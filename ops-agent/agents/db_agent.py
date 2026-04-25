"""
agents/db_agent.py — 数据库状态表监控 Agent

定期执行配置的 SQL 查询，将异常行转化为 Event 并发布到事件总线。
使用 SQLAlchemy async 驱动，支持 MySQL、PostgreSQL、SQLite。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from string import Template
from typing import Any, Optional

from core.event import Event, EventSource, EventSeverity
from core.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class DBTarget:
    name: str
    dsn: str
    query: str
    severity_field: Optional[str] = None
    message_template: str = "DB alert from {name}"
    poll_interval_seconds: int = 30


class DBAgent:
    def __init__(
        self,
        bus: EventBus,
        targets: list[dict],
        default_poll_interval: int = 30,
    ) -> None:
        self._bus = bus
        self._targets = [
            DBTarget(
                name=t["name"],
                dsn=t["dsn"],
                query=t["query"],
                severity_field=t.get("severity_field"),
                message_template=t.get("message_template", "DB alert: {name}"),
                poll_interval_seconds=t.get("poll_interval_seconds", default_poll_interval),
            )
            for t in targets
        ]
        self._engines: dict[str, Any] = {}

    async def _get_engine(self, dsn: str) -> Any:
        """懒加载数据库引擎，失败时返回 None。"""
        if dsn in self._engines:
            return self._engines[dsn]
        try:
            from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore
            engine = create_async_engine(dsn, pool_pre_ping=True)
            self._engines[dsn] = engine
            return engine
        except Exception as exc:
            logger.error("DBAgent: cannot create engine for DSN %s: %s", dsn[:30], exc)
            return None

    async def run(self) -> None:
        if not self._targets:
            logger.info("DBAgent: no targets configured, idle")
            while True:
                await asyncio.sleep(3600)

        logger.info("DBAgent: starting with %d targets", len(self._targets))
        tasks = [asyncio.create_task(self._poll_target(t)) for t in self._targets]
        await asyncio.gather(*tasks)

    async def _poll_target(self, target: DBTarget) -> None:
        while True:
            try:
                await self._query_and_publish(target)
            except Exception as exc:
                logger.error("DBAgent: error querying %s: %s", target.name, exc)
            await asyncio.sleep(target.poll_interval_seconds)

    async def _query_and_publish(self, target: DBTarget) -> None:
        engine = await self._get_engine(target.dsn)
        if engine is None:
            return

        from sqlalchemy import text  # type: ignore
        from sqlalchemy.ext.asyncio import AsyncConnection  # type: ignore

        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(target.query))
                rows = result.mappings().all()
        except Exception as exc:
            logger.error("DBAgent: query failed for %s: %s", target.name, exc)
            # 查询失败本身也是一个告警事件
            event = Event(
                source=EventSource.DB,
                severity=EventSeverity.ERROR,
                message=f"DB query failed: {target.name}",
                raw=str(exc),
                target=target.name,
                tags={"rule_id": "db_query_error"},
                extra={"rule_id": "db_query_error"},
            )
            await self._bus.publish(event)
            return

        for row in rows:
            row_dict = dict(row)
            # 从结果列读取严重度
            severity = EventSeverity.ERROR
            if target.severity_field and target.severity_field in row_dict:
                try:
                    severity = EventSeverity(str(row_dict[target.severity_field]).lower())
                except ValueError:
                    pass

            # 格式化消息
            try:
                msg = target.message_template.format(**row_dict, name=target.name)
            except (KeyError, ValueError):
                msg = f"DB alert from {target.name}: {row_dict}"

            event = Event(
                source=EventSource.DB,
                severity=severity,
                message=msg,
                raw=str(row_dict),
                target=target.name,
                tags={"db_target": target.name},
                extra={"row": {k: str(v) for k, v in row_dict.items()}},
            )
            await self._bus.publish(event)
            logger.debug("DBAgent: published event from %s: %s", target.name, msg[:80])
