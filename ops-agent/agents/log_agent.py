"""
agents/log_agent.py — 日志文件监控 Agent

持续 tail 日志文件，将每行与规则匹配，匹配成功则发布 Event 到事件总线。
支持文件轮转（logrotate）检测，自动重新打开文件。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.event import Event, EventSource, EventSeverity
from core.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class LogTarget:
    path: str
    name: str
    encoding: str = "utf-8"


@dataclass
class LogRule:
    id: str
    pattern: re.Pattern
    severity: EventSeverity
    title_template: str


class LogAgent:
    def __init__(
        self,
        bus: EventBus,
        targets: list[dict],
        rules: list[dict],
        poll_interval: float = 2.0,
    ) -> None:
        self._bus = bus
        self._targets = [LogTarget(**t) for t in targets]
        self._rules = self._compile_rules(rules)
        self._poll_interval = poll_interval
        self._file_positions: dict[str, int] = {}
        self._file_inodes: dict[str, int] = {}

    def _compile_rules(self, raw_rules: list[dict]) -> list[LogRule]:
        compiled = []
        for r in raw_rules:
            if r.get("source") not in ("log", "*"):
                continue
            if not r.get("enabled", True):
                continue
            pattern_str = r.get("pattern", "")
            if not pattern_str:
                continue
            try:
                compiled.append(
                    LogRule(
                        id=r["id"],
                        pattern=re.compile(pattern_str, re.IGNORECASE),
                        severity=EventSeverity(r.get("severity", "error")),
                        title_template=r.get("title", r["id"]),
                    )
                )
            except re.error as exc:
                logger.warning("LogAgent: invalid pattern for rule %s: %s", r["id"], exc)
        logger.info("LogAgent: compiled %d log rules", len(compiled))
        return compiled

    def reload_rules(self, rules: list[dict]) -> None:
        self._rules = self._compile_rules(rules)
        logger.info("LogAgent: rules reloaded (%d active)", len(self._rules))

    async def run(self) -> None:
        logger.info("LogAgent: starting, watching %d files", len(self._targets))
        while True:
            for target in self._targets:
                try:
                    await self._tail_file(target)
                except Exception as exc:
                    logger.error("LogAgent: error tailing %s: %s", target.path, exc)
            await asyncio.sleep(self._poll_interval)

    async def _tail_file(self, target: LogTarget) -> None:
        path = Path(target.path)
        if not path.exists():
            return

        current_inode = path.stat().st_ino
        prev_inode = self._file_inodes.get(target.path)

        # 文件轮转检测
        if prev_inode is not None and current_inode != prev_inode:
            logger.info("LogAgent: rotation detected for %s, resetting position", target.path)
            self._file_positions[target.path] = 0

        self._file_inodes[target.path] = current_inode
        seek_pos = self._file_positions.get(target.path, None)

        try:
            with open(path, encoding=target.encoding, errors="replace") as f:
                if seek_pos is None:
                    # 首次打开：跳到文件末尾，只读新内容
                    f.seek(0, 2)
                    self._file_positions[target.path] = f.tell()
                    return

                f.seek(seek_pos)
                lines = f.readlines()
                self._file_positions[target.path] = f.tell()

            for line in lines:
                line = line.rstrip("\n")
                if not line:
                    continue
                await self._match_and_publish(line, target)
        except OSError as exc:
            logger.warning("LogAgent: cannot read %s: %s", target.path, exc)

    async def _match_and_publish(self, line: str, target: LogTarget) -> None:
        for rule in self._rules:
            if rule.pattern.search(line):
                title = rule.title_template.replace("{target}", target.name)
                event = Event(
                    source=EventSource.LOG,
                    severity=rule.severity,
                    message=title,
                    raw=line,
                    target=target.path,
                    tags={"log_name": target.name, "rule_id": rule.id},
                    extra={"rule_id": rule.id},
                )
                await self._bus.publish(event)
                logger.debug(
                    "LogAgent: rule '%s' matched in %s: %.80s",
                    rule.id,
                    target.name,
                    line,
                )
