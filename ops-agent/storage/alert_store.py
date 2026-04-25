"""
storage/alert_store.py — 告警存储

内存 + JSON 文件持久化。
重启后从文件恢复最近 N 天的告警。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from core.alert import Alert, AlertStatus
from core.event import EventSeverity

logger = logging.getLogger(__name__)

MAX_MEMORY_ALERTS = 10_000


class AlertStore:
    def __init__(self, data_dir: str = "/data", retention_days: int = 30) -> None:
        self._alerts: OrderedDict[str, Alert] = OrderedDict()
        self._lock = asyncio.Lock()
        self._data_dir = Path(data_dir)
        self._retention_days = retention_days
        self._persist_file = self._data_dir / "alerts.jsonl"

    async def initialize(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        await self._load_from_disk()

    async def add(self, alert: Alert) -> None:
        async with self._lock:
            self._alerts[alert.id] = alert
            if len(self._alerts) > MAX_MEMORY_ALERTS:
                self._alerts.popitem(last=False)
            await self._append_to_disk(alert)

    async def get(self, alert_id: str) -> Optional[Alert]:
        async with self._lock:
            return self._alerts.get(alert_id)

    async def update(self, alert: Alert) -> None:
        async with self._lock:
            self._alerts[alert.id] = alert
            await self._rewrite_disk()

    async def list_alerts(
        self,
        status: Optional[AlertStatus] = None,
        severity: Optional[EventSeverity] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Alert]:
        async with self._lock:
            alerts = list(reversed(list(self._alerts.values())))
            if status:
                alerts = [a for a in alerts if a.status == status]
            if severity:
                alerts = [a for a in alerts if a.severity == severity]
            return alerts[offset : offset + limit]

    async def count(self, status: Optional[AlertStatus] = None) -> int:
        async with self._lock:
            if status:
                return sum(1 for a in self._alerts.values() if a.status == status)
            return len(self._alerts)

    async def purge_old(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        async with self._lock:
            old_ids = [
                aid for aid, a in self._alerts.items() if a.created_at < cutoff
            ]
            for aid in old_ids:
                del self._alerts[aid]
            if old_ids:
                await self._rewrite_disk()
        return len(old_ids)

    # ── 统计（供 SelfEvolutionAgent 使用）────────────────
    async def rule_stats(self) -> dict[str, dict]:
        """统计每条规则的触发次数、误报次数等。"""
        async with self._lock:
            stats: dict[str, dict] = {}
            for alert in self._alerts.values():
                rid = alert.rule_id or "__unknown__"
                s = stats.setdefault(rid, {"total": 0, "false_positive": 0, "resolved": 0, "open": 0})
                s["total"] += 1
                if alert.status == AlertStatus.FALSE_POSITIVE:
                    s["false_positive"] += 1
                elif alert.status == AlertStatus.RESOLVED:
                    s["resolved"] += 1
                else:
                    s["open"] += 1
            return stats

    # ── 持久化 ────────────────────────────────────────────
    async def _append_to_disk(self, alert: Alert) -> None:
        try:
            line = json.dumps(alert.to_dict(), ensure_ascii=False) + "\n"
            async with asyncio.Lock():
                with open(self._persist_file, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as exc:
            logger.error("AlertStore: failed to persist alert %s: %s", alert.id, exc)

    async def _rewrite_disk(self) -> None:
        try:
            tmp = self._persist_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for alert in self._alerts.values():
                    f.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")
            tmp.replace(self._persist_file)
        except Exception as exc:
            logger.error("AlertStore: failed to rewrite disk: %s", exc)

    async def _load_from_disk(self) -> None:
        if not self._persist_file.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        loaded = 0
        try:
            with open(self._persist_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        created = datetime.fromisoformat(d["created_at"])
                        if created < cutoff:
                            continue
                        alert = Alert(
                            id=d["id"],
                            title=d["title"],
                            severity=EventSeverity(d["severity"]),
                            summary=d["summary"],
                            rule_id=d.get("rule_id", ""),
                            status=AlertStatus(d["status"]),
                            labels=d.get("labels", {}),
                            suggestions=d.get("suggestions", []),
                            created_at=created,
                            updated_at=datetime.fromisoformat(d["updated_at"]),
                            extra=d.get("extra", {}),
                        )
                        self._alerts[alert.id] = alert
                        loaded += 1
                    except Exception as exc:
                        logger.debug("AlertStore: skip malformed line: %s", exc)
        except Exception as exc:
            logger.error("AlertStore: failed to load from disk: %s", exc)
        logger.info("AlertStore: loaded %d alerts from disk", loaded)
