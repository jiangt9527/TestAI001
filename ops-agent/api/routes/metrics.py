"""
api/routes/metrics.py — Prometheus metrics 端点

暴露 /metrics 供 Prometheus 抓取，Grafana 等监控系统可直接接入。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from core.alert import AlertStatus

router = APIRouter(tags=["metrics"])

_start_time = datetime.now(timezone.utc)


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    from api.server import alert_store, event_bus, system_meta

    lines: list[str] = []

    def metric(name: str, value, labels: dict = None, help_text: str = "", mtype: str = "gauge"):
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")

    # 告警数量
    for status in AlertStatus:
        count = await alert_store.count(status=status)
        metric(
            "ops_agent_alerts_total",
            count,
            labels={"status": status.value},
            help_text="Total number of alerts by status",
            mtype="gauge",
        )

    # 事件总线统计
    bus_stats = event_bus.stats
    metric("ops_agent_events_published_total", bus_stats["published"],
           help_text="Total events published to the bus", mtype="counter")
    metric("ops_agent_events_dropped_total", bus_stats["dropped"],
           help_text="Total events dropped due to full queue", mtype="counter")

    # 运行时间
    uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()
    metric("ops_agent_uptime_seconds", round(uptime), help_text="Agent uptime in seconds")

    return "\n".join(lines) + "\n"
