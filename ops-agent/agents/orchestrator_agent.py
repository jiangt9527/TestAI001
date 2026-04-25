"""
agents/orchestrator_agent.py — 总指挥 / 诊断引擎

从事件总线消费 Event，进行：
1. 事件去重与聚合（同一规则 + 同一目标，在时间窗口内只生成一条告警）
2. 告警生成，附带诊断建议
3. （可选）调用外部 LLM Agent 接口获取 AI 诊断分析，丰富告警内容
4. 告警写入 AlertStore

LLM Agent 通过 RESTful API 调用（见 core/llm_agent_client.py）。
若 LLM Agent 不可用，则降级为仅使用规则内预设的 suggestions。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.alert import Alert
from core.event import Event, EventSeverity
from core.event_bus import EventBus
from core.llm_agent_client import LLMAgentClient
from storage.alert_store import AlertStore

logger = logging.getLogger(__name__)


@dataclass
class RuleMeta:
    id: str
    title_template: str
    suggestions: list[str]
    severity: EventSeverity
    weight: float = 1.0


@dataclass
class DeduplicationKey:
    rule_id: str
    target: str

    def __hash__(self):
        return hash((self.rule_id, self.target))


class OrchestratorAgent:
    def __init__(
        self,
        bus: EventBus,
        store: AlertStore,
        rules: list[dict],
        correlation_window_seconds: int = 300,
        llm_client: Optional[LLMAgentClient] = None,
    ) -> None:
        self._queue = bus.subscribe()
        self._store = store
        self._correlation_window = timedelta(seconds=correlation_window_seconds)
        self._rule_meta: dict[str, RuleMeta] = {}
        self._dedup_table: dict[DeduplicationKey, str] = {}  # key → alert_id
        self._dedup_timestamps: dict[DeduplicationKey, datetime] = {}
        self._llm_client = llm_client
        self._load_rules(rules)

    def _load_rules(self, rules: list[dict]) -> None:
        self._rule_meta.clear()
        for r in rules:
            if not r.get("enabled", True):
                continue
            self._rule_meta[r["id"]] = RuleMeta(
                id=r["id"],
                title_template=r.get("title", r["id"]),
                suggestions=r.get("suggestions", []),
                severity=EventSeverity(r.get("severity", "error")),
                weight=float(r.get("weight", 1.0)),
            )
        logger.info("OrchestratorAgent: loaded %d rules", len(self._rule_meta))

    def reload_rules(self, rules: list[dict]) -> None:
        self._load_rules(rules)

    async def run(self) -> None:
        logger.info("OrchestratorAgent: starting")
        cleanup_task = asyncio.create_task(self._cleanup_loop())
        try:
            while True:
                event: Event = await self._queue.get()
                try:
                    await self._process(event)
                except Exception as exc:
                    logger.error("OrchestratorAgent: error processing event %s: %s", event.id, exc)
        finally:
            cleanup_task.cancel()

    async def _process(self, event: Event) -> None:
        rule_id = event.extra.get("rule_id") or event.tags.get("rule_id")
        if not rule_id:
            return

        meta = self._rule_meta.get(rule_id)
        if meta is None:
            return

        # 去重：同一规则 + 同一目标，在时间窗口内不重复告警
        key = DeduplicationKey(rule_id=rule_id, target=event.target)
        now = datetime.now(timezone.utc)
        last_time = self._dedup_timestamps.get(key)

        if last_time and (now - last_time) < self._correlation_window:
            # 时间窗口内：将事件追加到已有告警（不创建新告警）
            existing_alert_id = self._dedup_table.get(key)
            if existing_alert_id:
                existing = await self._store.get(existing_alert_id)
                if existing:
                    existing.events.append(event)
                    existing.updated_at = now
                    await self._store.update(existing)
                    logger.debug(
                        "OrchestratorAgent: aggregated event into alert %s", existing_alert_id
                    )
            return

        # 新告警
        title = meta.title_template.replace("{target}", event.target or event.tags.get("log_name", ""))
        summary = event.raw[:500] if event.raw else event.message

        alert = Alert(
            title=title,
            severity=meta.severity,
            summary=summary,
            events=[event],
            suggestions=list(meta.suggestions),  # 先用规则预设建议
            rule_id=rule_id,
            labels={
                "source": event.source.value,
                "target": event.target,
                "rule_id": rule_id,
            },
        )
        await self._store.add(alert)

        self._dedup_table[key] = alert.id
        self._dedup_timestamps[key] = now

        logger.info(
            "OrchestratorAgent: created alert [%s] %s (rule=%s)",
            alert.severity.value.upper(),
            alert.title,
            rule_id,
        )

        # 异步调用外部 LLM Agent 补充 AI 诊断（不阻塞告警生成）
        if self._llm_client and self._llm_client.enabled:
            asyncio.create_task(
                self._enrich_alert_with_llm(alert),
                name=f"llm_enrich_{alert.id[:8]}",
            )

    async def _enrich_alert_with_llm(self, alert: Alert) -> None:
        """
        调用外部 LLM Agent 的 /analyze-alert 接口，用 AI 诊断结果
        更新告警的 suggestions 和 extra.ai_analysis 字段。
        此方法异步运行，不阻塞主处理流程。
        """
        try:
            context = {
                "alert_id": alert.id,
                "rule_id": alert.rule_id,
                "title": alert.title,
                "severity": alert.severity.value,
                "summary": alert.summary,
                "source": alert.labels.get("source", ""),
                "target": alert.labels.get("target", ""),
                "event_count": len(alert.events),
                "labels": alert.labels,
            }
            result = await self._llm_client.analyze_alert(context)
            if not result:
                return

            # 用 LLM Agent 返回的建议追加（不替换）规则预设建议
            llm_suggestions = result.get("suggestions", [])
            if llm_suggestions:
                existing = set(alert.suggestions)
                for s in llm_suggestions:
                    if s not in existing:
                        alert.suggestions.append(s)

            # AI 分析文本写入 extra
            if result.get("analysis"):
                alert.extra["ai_analysis"] = result["analysis"]
            if result.get("root_cause"):
                alert.extra["ai_root_cause"] = result["root_cause"]
            if result.get("confidence") is not None:
                alert.extra["ai_confidence"] = result["confidence"]

            await self._store.update(alert)
            logger.info(
                "OrchestratorAgent: alert %s enriched by LLM Agent (confidence=%.2f)",
                alert.id[:8],
                result.get("confidence", 0),
            )
        except Exception as exc:
            logger.warning(
                "OrchestratorAgent: LLM enrichment failed for alert %s: %s",
                alert.id[:8],
                exc,
            )

    async def _cleanup_loop(self) -> None:
        """定期清理过期的去重记录，防止内存泄漏。"""
        while True:
            await asyncio.sleep(600)
            now = datetime.now(timezone.utc)
            expired = [
                k for k, t in self._dedup_timestamps.items()
                if (now - t) > self._correlation_window * 2
            ]
            for k in expired:
                self._dedup_table.pop(k, None)
                self._dedup_timestamps.pop(k, None)
            if expired:
                logger.debug("OrchestratorAgent: cleaned up %d expired dedup keys", len(expired))
