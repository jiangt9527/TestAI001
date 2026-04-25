"""
main.py — OpsAgent 主入口

启动所有 Agent 和 API 服务器，协调它们的生命周期。

启动顺序：
  1. 加载配置（config.yaml + rules.yaml）
  2. 初始化 AlertStore（加载持久化数据）
  3. 创建 EventBus
  4. 启动各 Agent（异步任务）
  5. 启动 FastAPI HTTP 服务器
  6. 等待所有任务完成（理论上永远运行）
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import uvicorn  # type: ignore
import yaml  # type: ignore

BASE_DIR = Path(__file__).parent
CONFIG_PATH = os.environ.get("CONFIG_PATH", str(BASE_DIR / "config/config.yaml"))
RULES_PATH = os.environ.get("RULES_PATH", str(BASE_DIR / "config/rules.yaml"))


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_rules() -> list[dict]:
    with open(RULES_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("rules", [])


async def main() -> None:
    config = load_config()
    setup_logging(config.get("system", {}).get("log_level", "INFO"))

    logger = logging.getLogger("main")
    logger.info("=" * 60)
    logger.info("OpsAgent v%s starting", config["system"].get("version", "0.1.0"))
    logger.info("=" * 60)

    rules = load_rules()
    data_dir = config["system"].get("data_dir", "/data")
    retention_days = config["system"].get("alert_retention_days", 30)

    # ── 初始化核心组件 ────────────────────────────────────
    from core.event_bus import EventBus
    from storage.alert_store import AlertStore

    bus = EventBus()
    store = AlertStore(data_dir=data_dir, retention_days=retention_days)
    await store.initialize()

    # ── 初始化 Agents ──────────────────────────────────────
    from agents.log_agent import LogAgent
    from agents.db_agent import DBAgent
    from agents.api_probe_agent import APIProbeAgent
    from agents.orchestrator_agent import OrchestratorAgent
    from agents.self_evolution_agent import SelfEvolutionAgent

    log_cfg = config.get("log_agent", {})
    db_cfg = config.get("db_agent", {})
    probe_cfg = config.get("api_probe_agent", {})
    orch_cfg = config.get("orchestrator_agent", {})
    evo_cfg = config.get("self_evolution_agent", {})
    api_cfg = config.get("api_server", {})

    orchestrator = OrchestratorAgent(
        bus=bus,
        store=store,
        rules=rules,
        correlation_window_seconds=orch_cfg.get("correlation_window_seconds", 300),
    )

    log_agent = LogAgent(
        bus=bus,
        targets=log_cfg.get("targets", []),
        rules=rules,
        poll_interval=log_cfg.get("poll_interval_seconds", 2),
    )

    db_agent = DBAgent(
        bus=bus,
        targets=db_cfg.get("targets", []) if db_cfg.get("enabled", False) else [],
        default_poll_interval=db_cfg.get("poll_interval_seconds", 30),
    )

    probe_agent = APIProbeAgent(
        bus=bus,
        targets=probe_cfg.get("targets", []) if probe_cfg.get("enabled", True) else [],
        default_poll_interval=probe_cfg.get("poll_interval_seconds", 60),
        default_timeout=probe_cfg.get("timeout_seconds", 10),
    )

    # 自进化回调：规则更新后热重载到各 Agent
    async def on_rules_updated(updated_rules: list[dict]) -> None:
        log_agent.reload_rules(updated_rules)
        orchestrator.reload_rules(updated_rules)
        logger.info("main: rules hot-reloaded into all agents")

    evolution_agent = SelfEvolutionAgent(
        store=store,
        rules_path=RULES_PATH,
        review_interval_seconds=evo_cfg.get("review_interval_seconds", 3600),
        false_positive_threshold=evo_cfg.get("false_positive_threshold", 0.3),
        min_fires=evo_cfg.get("min_fires_for_review", 10),
        on_rules_updated=on_rules_updated if evo_cfg.get("enabled", True) else None,
        learning_dir=str(Path(data_dir) / "learning"),
    )

    # ── 创建 FastAPI 应用 ────────────────────────────────
    from api.server import create_app

    app = create_app(
        store=store,
        bus=bus,
        meta={"version": config["system"].get("version", "0.1.0")},
        cors_origins=api_cfg.get("cors_origins", ["*"]),
    )

    # ── 启动所有任务 ────────────────────────────────────
    async def run_uvicorn():
        server_config = uvicorn.Config(
            app=app,
            host=api_cfg.get("host", "0.0.0.0"),
            port=api_cfg.get("port", 8000),
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(server_config)
        await server.serve()

    tasks = [
        asyncio.create_task(orchestrator.run(), name="orchestrator"),
        asyncio.create_task(log_agent.run(), name="log_agent"),
        asyncio.create_task(db_agent.run(), name="db_agent"),
        asyncio.create_task(probe_agent.run(), name="api_probe"),
        asyncio.create_task(evolution_agent.run(), name="self_evolution"),
        asyncio.create_task(run_uvicorn(), name="api_server"),
    ]

    logger.info(
        "OpsAgent: all agents started (log=%s, db=%s, probe=%s, evolution=%s)",
        log_cfg.get("enabled", True),
        db_cfg.get("enabled", False),
        probe_cfg.get("enabled", True),
        evo_cfg.get("enabled", True),
    )
    logger.info("OpsAgent: API server at http://%s:%d", api_cfg.get("host", "0.0.0.0"), api_cfg.get("port", 8000))
    logger.info("OpsAgent: Prometheus metrics at http://%s:%d/metrics", api_cfg.get("host", "0.0.0.0"), api_cfg.get("port", 8000))

    # 优雅退出
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(signum, frame):
        logger.info("OpsAgent: received signal %s, shutting down...", signum)
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _shutdown(s, None))
        except NotImplementedError:
            pass

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    for task in done:
        if task.exception():
            logger.error("OpsAgent: task %s raised: %s", task.get_name(), task.exception())

    for task in pending:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
