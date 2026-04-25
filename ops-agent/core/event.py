"""
core/event.py — 统一事件模型

所有 Agent 产生的原始观测都转化为 Event 对象，放入事件总线。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventSource(str, Enum):
    LOG = "log"
    DB = "db"
    API_PROBE = "api_probe"
    SELF_EVOLUTION = "self_evolution"
    SYSTEM = "system"


class EventSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Event:
    source: EventSource
    severity: EventSeverity
    message: str
    raw: str = ""
    target: str = ""          # 日志文件路径 / 数据库表名 / API URL
    tags: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source.value,
            "severity": self.severity.value,
            "message": self.message,
            "target": self.target,
            "tags": self.tags,
            "extra": self.extra,
        }
