"""
agents/self_evolution_agent.py — 自我进化智能体

这是 OpsAgent 的"进化引擎"。它定期分析自身的运行效果，
根据数据自动调整规则权重、禁用低效规则、并将改进写回 rules.yaml。

进化逻辑（双通道）：
  通道 A — 统计驱动（本地，始终运行）：
    1. 从 AlertStore 读取各规则的统计数据（触发次数、误报率、解决率）
    2. 根据误报率和未解决率调整规则 weight

  通道 B — LLM Agent 驱动（通过 REST，可选）：
    1. 将统计数据 + 未匹配日志样本通过 REST API 发给外部 LLM Agent
    2. LLM Agent 返回：权重调整建议、新规则、需禁用的规则
    3. 将 LLM Agent 建议与统计结果合并（LLM 建议优先级更高）

  最终结果：
    3. 将新的 weight 写回 rules.yaml（持久化）
    4. 通知 LogAgent 和 OrchestratorAgent 热重载规则
    5. 将进化决策写入 learning/evolution_log.jsonl（可追溯）

注意：本 Agent 不直接调用任何 LLM。所有 AI 能力均来自外部 LLM Agent 的 RESTful 接口。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable, Optional

import yaml  # type: ignore

from core.llm_agent_client import LLMAgentClient
from core.unmatched_tracker import UnmatchedTracker
from storage.alert_store import AlertStore

logger = logging.getLogger(__name__)

MIN_FIRES_FOR_REVIEW = 10


class SelfEvolutionAgent:
    def __init__(
        self,
        store: AlertStore,
        rules_path: str,
        review_interval_seconds: int = 3600,
        false_positive_threshold: float = 0.3,
        min_fires: int = MIN_FIRES_FOR_REVIEW,
        on_rules_updated: Optional[Callable[[list[dict]], Awaitable[None]]] = None,
        learning_dir: str = "/data/learning",
        llm_client: Optional[LLMAgentClient] = None,
        unmatched_tracker: Optional[UnmatchedTracker] = None,
    ) -> None:
        self._store = store
        self._rules_path = Path(rules_path)
        self._review_interval = review_interval_seconds
        self._fp_threshold = false_positive_threshold
        self._min_fires = min_fires
        self._on_rules_updated = on_rules_updated
        self._learning_dir = Path(learning_dir)
        self._llm_client = llm_client
        self._unmatched_tracker = unmatched_tracker
        self._evolution_count = 0

    async def run(self) -> None:
        self._learning_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "SelfEvolutionAgent: starting, review every %ds, LLM Agent=%s",
            self._review_interval,
            "enabled" if (self._llm_client and self._llm_client.enabled) else "disabled",
        )
        while True:
            await asyncio.sleep(self._review_interval)
            try:
                await self._review_and_evolve()
            except Exception as exc:
                logger.error("SelfEvolutionAgent: evolution cycle failed: %s", exc)

    async def _review_and_evolve(self) -> None:
        stats = await self._store.rule_stats()
        rules = self._load_rules()
        if not rules:
            return

        # ── 通道 A：统计驱动（本地）────────────────────────
        stat_changes = self._compute_stat_adjustments(rules, stats)

        # ── 通道 B：LLM Agent 驱动（REST，可选）────────────
        llm_result: Optional[dict] = None
        if self._llm_client and self._llm_client.enabled:
            llm_result = await self._call_llm_evolve(rules, stats)

        # ── 合并两个通道的建议 ──────────────────────────────
        changes = self._merge_adjustments(rules, stat_changes, llm_result)

        # 清空未匹配样本（已发送给 LLM Agent 分析）
        if self._unmatched_tracker:
            await self._unmatched_tracker.clear()

        if not changes and not _has_new_rules(llm_result) and not _has_disable_rules(llm_result):
            logger.info("SelfEvolutionAgent: no rule changes needed this cycle")
            return

        # 写回 rules.yaml
        self._save_rules(rules)
        self._evolution_count += 1

        # 记录进化日志
        await self._log_evolution(changes, llm_result)

        logger.info(
            "SelfEvolutionAgent: evolution #%d — %d rules updated, LLM=%s",
            self._evolution_count,
            len(changes),
            "contributed" if llm_result else "not used",
        )

        # 通知其他 Agent 热重载
        if self._on_rules_updated:
            await self._on_rules_updated(rules)

    def _compute_stat_adjustments(
        self, rules: list[dict], stats: dict[str, dict]
    ) -> list[dict]:
        """通道 A：纯统计驱动的权重调整，返回变更列表（不修改 rules 本身）。"""
        changes = []
        for rule in rules:
            rid = rule.get("id")
            if not rid or not rule.get("enabled", True):
                continue

            s = stats.get(rid)
            if not s or s["total"] < self._min_fires:
                continue

            total = s["total"]
            fp_rate = s["false_positive"] / total
            resolve_rate = s["resolved"] / total
            current_weight = float(rule.get("weight", 1.0))
            new_weight = current_weight
            reason = ""

            if fp_rate >= self._fp_threshold:
                new_weight = max(0.1, current_weight * 0.85)
                reason = f"高误报率 {fp_rate:.1%}，权重 {current_weight:.2f} → {new_weight:.2f}"
            elif fp_rate < 0.05 and resolve_rate > 0.7:
                new_weight = min(1.0, current_weight * 1.05)
                reason = f"低误报率 {fp_rate:.1%} 高解决率 {resolve_rate:.1%}，权重 {current_weight:.2f} → {new_weight:.2f}"
            elif total > 100 and s["open"] / total > 0.9:
                new_weight = max(0.1, current_weight * 0.95)
                reason = f"告警处理率过低 {(1 - s['open']/total):.1%}，权重轻微下调"

            if abs(new_weight - current_weight) > 0.001:
                changes.append(
                    {
                        "rule_id": rid,
                        "old_weight": round(current_weight, 3),
                        "new_weight": round(new_weight, 3),
                        "reason": reason,
                        "source": "stats",
                        "stats": s,
                    }
                )
        return changes

    async def _call_llm_evolve(
        self, rules: list[dict], stats: dict[str, dict]
    ) -> Optional[dict]:
        """通道 B：调用外部 LLM Agent 的 /evolve-rules 接口。"""
        unmatched = []
        if self._unmatched_tracker:
            unmatched = await self._unmatched_tracker.get_top_samples(top_n=20)

        payload = {
            "current_rules": rules,
            "stats": stats,
            "unmatched_samples": unmatched,
        }
        return await self._llm_client.evolve_rules(payload)

    def _merge_adjustments(
        self,
        rules: list[dict],
        stat_changes: list[dict],
        llm_result: Optional[dict],
    ) -> list[dict]:
        """
        合并统计建议和 LLM Agent 建议，写入 rules 列表。
        LLM Agent 的 weight_adjustments 优先于统计结果。
        LLM Agent 建议的新规则直接追加到 rules。
        LLM Agent 建议禁用的规则设置 enabled=false。
        """
        # 先应用统计建议（作为基础）
        all_changes = list(stat_changes)
        weight_map = {c["rule_id"]: c["new_weight"] for c in stat_changes}

        if llm_result:
            # LLM 权重调整覆盖统计结果
            for adj in llm_result.get("weight_adjustments", []):
                rid = adj.get("rule_id")
                new_w = adj.get("new_weight")
                if rid and new_w is not None:
                    weight_map[rid] = round(float(new_w), 3)
                    all_changes.append(
                        {
                            "rule_id": rid,
                            "new_weight": round(float(new_w), 3),
                            "reason": adj.get("reason", "LLM Agent 建议"),
                            "source": "llm_agent",
                        }
                    )

            # 应用禁用列表
            disable_set = set(llm_result.get("rules_to_disable", []))
            for rule in rules:
                if rule.get("id") in disable_set:
                    rule["enabled"] = False
                    all_changes.append(
                        {
                            "rule_id": rule["id"],
                            "action": "disabled",
                            "reason": "LLM Agent 建议禁用",
                            "source": "llm_agent",
                        }
                    )

            # 添加 LLM Agent 建议的新规则
            existing_ids = {r.get("id") for r in rules}
            for new_rule in llm_result.get("new_rules", []):
                if new_rule.get("id") and new_rule["id"] not in existing_ids:
                    new_rule.setdefault("enabled", True)
                    new_rule.setdefault("weight", 0.75)
                    new_rule["_source"] = "llm_agent_suggested"
                    rules.append(new_rule)
                    existing_ids.add(new_rule["id"])
                    all_changes.append(
                        {
                            "rule_id": new_rule["id"],
                            "action": "new_rule",
                            "reason": "LLM Agent 发现的新规则",
                            "source": "llm_agent",
                        }
                    )
                    logger.info(
                        "SelfEvolutionAgent: LLM Agent suggested new rule '%s' (pattern=%s)",
                        new_rule["id"],
                        new_rule.get("pattern", ""),
                    )

        # 将最终 weight 写入 rules
        for rule in rules:
            rid = rule.get("id")
            if rid and rid in weight_map:
                rule["weight"] = weight_map[rid]

        return all_changes

    def _load_rules(self) -> list[dict]:
        try:
            with open(self._rules_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data.get("rules", [])
        except Exception as exc:
            logger.error("SelfEvolutionAgent: cannot load rules: %s", exc)
            return []

    def _save_rules(self, rules: list[dict]) -> None:
        try:
            tmp = self._rules_path.with_suffix(".tmp")
            with open(self._rules_path, encoding="utf-8") as f:
                full_data = yaml.safe_load(f) or {}
            full_data["rules"] = rules
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(full_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            tmp.replace(self._rules_path)
            logger.info("SelfEvolutionAgent: rules.yaml updated")
        except Exception as exc:
            logger.error("SelfEvolutionAgent: cannot save rules: %s", exc)

    async def _log_evolution(
        self, changes: list[dict], llm_result: Optional[dict]
    ) -> None:
        log_file = self._learning_dir / "evolution_log.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "evolution_number": self._evolution_count,
            "changes": changes,
            "llm_agent_used": llm_result is not None,
            "llm_new_rules": len((llm_result or {}).get("new_rules", [])),
            "llm_disabled_rules": len((llm_result or {}).get("rules_to_disable", [])),
        }
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error("SelfEvolutionAgent: cannot write evolution log: %s", exc)

    @property
    def evolution_count(self) -> int:
        return self._evolution_count


def _has_new_rules(llm_result: Optional[dict]) -> bool:
    return bool(llm_result and llm_result.get("new_rules"))


def _has_disable_rules(llm_result: Optional[dict]) -> bool:
    return bool(llm_result and llm_result.get("rules_to_disable"))

