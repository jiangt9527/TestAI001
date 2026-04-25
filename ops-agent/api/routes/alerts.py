"""
api/routes/alerts.py — 告警相关 REST API
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.alert import AlertStatus
from core.event import EventSeverity

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


class AlertStatusUpdate(BaseModel):
    status: str


def _get_store():
    from api.server import alert_store
    return alert_store


@router.get("")
async def list_alerts(
    status: Optional[str] = Query(None, description="open/acknowledged/resolved/false_positive"),
    severity: Optional[str] = Query(None, description="debug/info/warning/error/critical"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    store = _get_store()
    status_filter = AlertStatus(status) if status else None
    severity_filter = EventSeverity(severity) if severity else None
    alerts = await store.list_alerts(
        status=status_filter, severity=severity_filter, limit=limit, offset=offset
    )
    total = await store.count(status=status_filter)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "alerts": [a.to_dict() for a in alerts],
    }


@router.get("/{alert_id}")
async def get_alert(alert_id: str):
    store = _get_store()
    alert = await store.get(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    d = alert.to_dict()
    d["events"] = [e.to_dict() for e in alert.events[-20:]]  # 最近 20 条事件
    return d


@router.patch("/{alert_id}/status")
async def update_alert_status(alert_id: str, body: AlertStatusUpdate):
    store = _get_store()
    alert = await store.get(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    try:
        new_status = AlertStatus(body.status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {body.status}")

    if new_status == AlertStatus.RESOLVED:
        alert.resolve()
    elif new_status == AlertStatus.FALSE_POSITIVE:
        alert.mark_false_positive()
    else:
        from datetime import datetime, timezone
        alert.status = new_status
        alert.updated_at = datetime.now(timezone.utc)

    await store.update(alert)
    return {"id": alert_id, "status": alert.status.value}
