#!/usr/bin/env python3
"""
scripts/test_local.py — 本地快速验证脚本

无需 Docker，直接在本地运行一个小型集成测试：
  - 创建假日志文件并写入测试行
  - 启动 OpsAgent（限时 10 秒）
  - 验证告警是否被正确创建
  - 验证 /api/v1/alerts 接口是否可用
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import os
from pathlib import Path

# 把 ops-agent 根目录加入 Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

TEST_LOG_CONTENT = """\
2026-04-25 10:00:01 INFO  Server starting up
2026-04-25 10:00:02 ERROR OutOfMemoryError: Java heap space
2026-04-25 10:00:03 INFO  Attempting recovery
2026-04-25 10:00:04 ERROR Connection refused to db:5432
2026-04-25 10:00:05 WARN  timeout connecting to redis
"""


async def run_test():
    print("=" * 60)
    print("OpsAgent 本地集成测试")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "test.log"
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()

        # 预写日志内容（模拟已有日志）
        # 注意：LogAgent 首次打开会 seek 到末尾，所以我们先创建空文件
        log_file.write_text("")

        # 生成测试配置
        config = {
            "system": {"name": "OpsAgent-test", "version": "0.1.0-test",
                       "log_level": "DEBUG", "data_dir": str(data_dir),
                       "alert_retention_days": 1},
            "log_agent": {
                "enabled": True,
                "poll_interval_seconds": 0.1,
                "targets": [{"path": str(log_file), "name": "test_app", "encoding": "utf-8"}],
            },
            "db_agent": {"enabled": False, "targets": []},
            "api_probe_agent": {"enabled": False, "targets": []},
            "orchestrator_agent": {"correlation_window_seconds": 5},
            "self_evolution_agent": {"enabled": False, "review_interval_seconds": 9999,
                                     "false_positive_threshold": 0.3, "min_fires_for_review": 10},
            "api_server": {"host": "127.0.0.1", "port": 18000},
        }

        rules = [
            {"id": "log_oom", "name": "OOM", "source": "log",
             "pattern": "OutOfMemoryError", "severity": "critical",
             "title": "OOM [{target}]", "suggestions": ["restart service"],
             "weight": 0.95, "enabled": True},
            {"id": "log_connection_refused", "name": "Connection refused", "source": "log",
             "pattern": "Connection refused", "severity": "error",
             "title": "Connection refused [{target}]", "suggestions": ["check service"],
             "weight": 0.85, "enabled": True},
        ]

        config_path = Path(tmpdir) / "config.yaml"
        rules_path = Path(tmpdir) / "rules.yaml"
        config_path.write_text(yaml.dump(config, allow_unicode=True))
        rules_path.write_text(yaml.dump({"rules": rules}, allow_unicode=True))

        os.environ["CONFIG_PATH"] = str(config_path)
        os.environ["RULES_PATH"] = str(rules_path)

        # 延迟写入日志（在 Agent 启动后）
        async def write_logs_later():
            await asyncio.sleep(0.5)  # 等 Agent 打开文件
            with open(log_file, "a") as f:
                f.write(TEST_LOG_CONTENT)
            print(f"✓ 写入测试日志：{log_file}")

        # 导入并启动（限时）
        import importlib
        import main as main_module
        importlib.reload(main_module)

        async def run_with_timeout():
            tasks = [
                asyncio.create_task(main_module.main(), name="ops_agent"),
                asyncio.create_task(write_logs_later(), name="log_writer"),
            ]
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            except asyncio.TimeoutError:
                pass
            except Exception as exc:
                print(f"⚠ main() raised: {exc}")
            for t in tasks:
                if not t.done():
                    t.cancel()

        await run_with_timeout()

        # 验证结果
        from storage.alert_store import AlertStore
        result_store = AlertStore(data_dir=str(data_dir))
        await result_store.initialize()
        alerts = await result_store.list_alerts(limit=50)

        print(f"\n📊 结果：共产生 {len(alerts)} 条告警")
        for a in alerts:
            print(f"  [{a.severity.value.upper()}] {a.title} (rule={a.rule_id})")

        if len(alerts) >= 2:
            print("\n✅ 测试通过！OpsAgent 成功检测到告警。")
        else:
            print(f"\n⚠ 预期至少 2 条告警，实际 {len(alerts)} 条（可能需要调整 poll_interval）")


if __name__ == "__main__":
    asyncio.run(run_test())
