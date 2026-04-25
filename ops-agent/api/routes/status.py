"""
api/routes/status.py — 系统状态 API
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/status", tags=["status"])

_start_time = datetime.now(timezone.utc)


@router.get("")
async def get_status():
    from api.server import alert_store, event_bus, system_meta
    open_count = await alert_store.count()
    bus_stats = event_bus.stats
    uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()
    return {
        "status": "ok",
        "version": system_meta.get("version", "0.1.0"),
        "uptime_seconds": round(uptime),
        "alerts": {
            "open": await alert_store.count(status=None),
        },
        "event_bus": bus_stats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health")
async def health():
    return {"status": "ok"}
