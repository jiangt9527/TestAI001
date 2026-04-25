"""
agents/self_evolution_agent.py — 自我进化智能体

这是 OpsAgent 的"进化引擎"。它定期分析自身的运行效果，
根据数据自动调整规则权重、禁用低效规则、并将改进写回 rules.yaml。

进化逻辑：
  1. 从 AlertStore 读取各规则的统计数据（触发次数、误报率、解决率）
  2. 根据误报率和未解决率调整规则 weight
  3. 将新的 weight 写回 rules.yaml（持久化）
  4. 通知 LogAgent 和 OrchestratorAgent 热重载规则
  5. 将进化决策写入 learning/evolution_log.jsonl（可追溯）

未来扩展：
  - 通过 LLM 分析日志中的新错误模式，自动提议新规则
  - A/B 测试不同规则阈值的效果
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable, Optional

import yaml  # type: ignore

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
    ) -> None:
        self._store = store
        self._rules_path = Path(rules_path)
        self._review_interval = review_interval_seconds
        self._fp_threshold = false_positive_threshold
        self._min_fires = min_fires
        self._on_rules_updated = on_rules_updated
        self._learning_dir = Path(learning_dir)
        self._evolution_count = 0

    async def run(self) -> None:
        self._learning_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "SelfEvolutionAgent: starting, review every %ds", self._review_interval
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

        changes: list[dict] = []

        for rule in rules:
            rid = rule.get("id")
            if not rid or not rule.get("enabled", True):
                continue

            s = stats.get(rid)
            if not s or s["total"] < self._min_fires:
                continue  # 样本太少，不评估

            total = s["total"]
            fp_rate = s["false_positive"] / total
            resolve_rate = s["resolved"] / total
            current_weight = float(rule.get("weight", 1.0))

            new_weight = current_weight
            reason = ""

            if fp_rate >= self._fp_threshold:
                # 误报率高：降低权重
                new_weight = max(0.1, current_weight * 0.85)
                reason = f"高误报率 {fp_rate:.1%}，权重 {current_weight:.2f} → {new_weight:.2f}"
            elif fp_rate < 0.05 and resolve_rate > 0.7:
                # 误报率极低且解决率高：提升权重
                new_weight = min(1.0, current_weight * 1.05)
                reason = f"低误报率 {fp_rate:.1%} 高解决率 {resolve_rate:.1%}，权重 {current_weight:.2f} → {new_weight:.2f}"
            elif total > 100 and s["open"] / total > 0.9:
                # 几乎所有告警都未处理：可能是噪音规则，轻微降权
                new_weight = max(0.1, current_weight * 0.95)
                reason = f"告警处理率过低 {(1 - s['open']/total):.1%}，权重轻微下调"

            if abs(new_weight - current_weight) > 0.001:
                rule["weight"] = round(new_weight, 3)
                changes.append(
                    {
                        "rule_id": rid,
                        "old_weight": round(current_weight, 3),
                        "new_weight": round(new_weight, 3),
                        "reason": reason,
                        "stats": s,
                    }
                )

        if not changes:
            logger.info("SelfEvolutionAgent: no rule changes needed this cycle")
            return

        # 写回 rules.yaml
        self._save_rules(rules)
        self._evolution_count += 1

        # 记录进化日志
        await self._log_evolution(changes)

        logger.info(
            "SelfEvolutionAgent: evolution #%d — %d rules updated: %s",
            self._evolution_count,
            len(changes),
            [c["rule_id"] for c in changes],
        )

        # 通知其他 Agent 热重载
        if self._on_rules_updated:
            await self._on_rules_updated(rules)

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

    async def _log_evolution(self, changes: list[dict]) -> None:
        log_file = self._learning_dir / "evolution_log.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "evolution_number": self._evolution_count,
            "changes": changes,
        }
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.error("SelfEvolutionAgent: cannot write evolution log: %s", exc)

    @property
    def evolution_count(self) -> int:
        return self._evolution_count
