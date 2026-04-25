"""
core/alert.py — 告警模型

OrchestratorAgent 将事件聚合、关联后生成 Alert。
Alert 是面向运维人员的诊断结论，包含建议操作。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from core.event import Event, EventSeverity


class AlertStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


@dataclass
class Alert:
    title: str
    severity: EventSeverity
    summary: str
    events: list[Event] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    rule_id: str = ""
    status: AlertStatus = AlertStatus.OPEN
    labels: dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve(self) -> None:
        self.status = AlertStatus.RESOLVED
        self.resolved_at = datetime.now(timezone.utc)
        self.updated_at = self.resolved_at

    def mark_false_positive(self) -> None:
        self.status = AlertStatus.FALSE_POSITIVE
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity.value,
            "summary": self.summary,
            "status": self.status.value,
            "rule_id": self.rule_id,
            "labels": self.labels,
            "suggestions": self.suggestions,
            "event_count": len(self.events),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "extra": self.extra,
        }
