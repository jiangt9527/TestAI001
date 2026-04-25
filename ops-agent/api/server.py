"""
api/server.py — FastAPI 应用入口

全局单例：alert_store、event_bus、system_meta
由 main.py 在启动时注入。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.event_bus import EventBus
from storage.alert_store import AlertStore

# 全局单例（由 main.py 注入）
alert_store: AlertStore = None  # type: ignore
event_bus: EventBus = None  # type: ignore
system_meta: dict = {}


def create_app(
    store: AlertStore,
    bus: EventBus,
    meta: dict,
    cors_origins: list[str] = None,
) -> FastAPI:
    global alert_store, event_bus, system_meta
    alert_store = store
    event_bus = bus
    system_meta = meta

    app = FastAPI(
        title="OpsAgent API",
        description="运维超级智能体 REST API — 告警查询、状态监控、Prometheus 指标",
        version=meta.get("version", "0.1.0"),
    )

    origins = cors_origins or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from api.routes.alerts import router as alerts_router
    from api.routes.status import router as status_router
    from api.routes.metrics import router as metrics_router

    app.include_router(alerts_router)
    app.include_router(status_router)
    app.include_router(metrics_router)

    return app
