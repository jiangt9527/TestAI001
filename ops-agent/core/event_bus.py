"""
core/event_bus.py — 异步事件总线

所有 Agent 通过事件总线发布和订阅事件，实现松耦合。
支持多个消费者（每个消费者有独立的队列副本）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from core.event import Event

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self, maxsize: int = 1000) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._maxsize = maxsize
        self._published_count = 0
        self._dropped_count = 0

    def subscribe(self) -> asyncio.Queue[Event]:
        """创建一个新的订阅队列并返回，调用者从此队列读取事件。"""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(q)
        return q

    async def publish(self, event: Event) -> None:
        """向所有订阅者广播事件。队列满时丢弃并计数。"""
        self._published_count += 1
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped_count += 1
                logger.warning(
                    "EventBus: queue full, dropping event %s (total dropped: %d)",
                    event.id,
                    self._dropped_count,
                )

    @property
    def stats(self) -> dict:
        return {
            "published": self._published_count,
            "dropped": self._dropped_count,
            "subscribers": len(self._subscribers),
        }
