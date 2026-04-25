"""
agents/api_probe_agent.py — HTTP 接口探针 Agent

定期 HTTP 请求目标接口，检查可用性和响应时间。
失败或超时时发布 Event 到事件总线。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from core.event import Event, EventSource, EventSeverity
from core.event_bus import EventBus

logger = logging.getLogger(__name__)

SLOW_RESPONSE_THRESHOLD_SECONDS = 3.0


@dataclass
class ProbeTarget:
    name: str
    url: str
    method: str = "GET"
    expected_status: int = 200
    severity_on_failure: str = "error"
    headers: dict = field(default_factory=dict)
    body: Optional[str] = None
    poll_interval_seconds: int = 60
    timeout_seconds: int = 10


class APIProbeAgent:
    def __init__(
        self,
        bus: EventBus,
        targets: list[dict],
        default_poll_interval: int = 60,
        default_timeout: int = 10,
    ) -> None:
        self._bus = bus
        self._targets = [
            ProbeTarget(
                name=t["name"],
                url=t["url"],
                method=t.get("method", "GET").upper(),
                expected_status=t.get("expected_status", 200),
                severity_on_failure=t.get("severity_on_failure", "error"),
                headers=t.get("headers", {}),
                body=t.get("body"),
                poll_interval_seconds=t.get("poll_interval_seconds", default_poll_interval),
                timeout_seconds=t.get("timeout_seconds", default_timeout),
            )
            for t in targets
        ]

    async def run(self) -> None:
        if not self._targets:
            logger.info("APIProbeAgent: no targets configured, idle")
            while True:
                await asyncio.sleep(3600)

        logger.info("APIProbeAgent: starting with %d targets", len(self._targets))

        try:
            import aiohttp  # type: ignore
        except ImportError:
            logger.error("APIProbeAgent: aiohttp not installed, agent disabled")
            return

        async with aiohttp.ClientSession() as session:
            tasks = [
                asyncio.create_task(self._probe_loop(session, t))
                for t in self._targets
            ]
            await asyncio.gather(*tasks)

    async def _probe_loop(self, session: "aiohttp.ClientSession", target: ProbeTarget) -> None:
        while True:
            try:
                await self._probe_once(session, target)
            except Exception as exc:
                logger.error("APIProbeAgent: unexpected error probing %s: %s", target.name, exc)
            await asyncio.sleep(target.poll_interval_seconds)

    async def _probe_once(self, session: "aiohttp.ClientSession", target: ProbeTarget) -> None:
        import aiohttp  # type: ignore

        start = time.monotonic()
        try:
            async with session.request(
                target.method,
                target.url,
                headers=target.headers,
                data=target.body,
                timeout=aiohttp.ClientTimeout(total=target.timeout_seconds),
            ) as resp:
                elapsed = time.monotonic() - start
                status = resp.status

                if status != target.expected_status:
                    severity = EventSeverity(target.severity_on_failure)
                    event = Event(
                        source=EventSource.API_PROBE,
                        severity=severity,
                        message=f"服务接口不可达 [{target.name}]",
                        raw=f"HTTP {status} (expected {target.expected_status})",
                        target=target.url,
                        tags={"rule_id": "probe_endpoint_down", "probe_name": target.name},
                        extra={
                            "rule_id": "probe_endpoint_down",
                            "http_status": status,
                            "elapsed_ms": round(elapsed * 1000),
                        },
                    )
                    await self._bus.publish(event)
                    logger.warning(
                        "APIProbeAgent: %s returned %d (expected %d), %.0fms",
                        target.name, status, target.expected_status, elapsed * 1000,
                    )
                elif elapsed > SLOW_RESPONSE_THRESHOLD_SECONDS:
                    event = Event(
                        source=EventSource.API_PROBE,
                        severity=EventSeverity.WARNING,
                        message=f"接口响应缓慢 [{target.name}]",
                        raw=f"elapsed {elapsed:.2f}s (threshold {SLOW_RESPONSE_THRESHOLD_SECONDS}s)",
                        target=target.url,
                        tags={"rule_id": "probe_slow_response", "probe_name": target.name},
                        extra={
                            "rule_id": "probe_slow_response",
                            "elapsed_ms": round(elapsed * 1000),
                            "threshold_ms": round(SLOW_RESPONSE_THRESHOLD_SECONDS * 1000),
                        },
                    )
                    await self._bus.publish(event)
                    logger.warning(
                        "APIProbeAgent: %s slow response %.0fms", target.name, elapsed * 1000
                    )
                else:
                    logger.debug(
                        "APIProbeAgent: %s OK %d %.0fms", target.name, status, elapsed * 1000
                    )

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            event = Event(
                source=EventSource.API_PROBE,
                severity=EventSeverity(target.severity_on_failure),
                message=f"服务接口不可达 [{target.name}]",
                raw=f"timeout after {target.timeout_seconds}s",
                target=target.url,
                tags={"rule_id": "probe_endpoint_down", "probe_name": target.name},
                extra={
                    "rule_id": "probe_endpoint_down",
                    "error": "timeout",
                    "elapsed_ms": round(elapsed * 1000),
                },
            )
            await self._bus.publish(event)
            logger.warning("APIProbeAgent: %s timed out after %ds", target.name, target.timeout_seconds)

        except Exception as exc:
            event = Event(
                source=EventSource.API_PROBE,
                severity=EventSeverity(target.severity_on_failure),
                message=f"服务接口不可达 [{target.name}]",
                raw=str(exc),
                target=target.url,
                tags={"rule_id": "probe_endpoint_down", "probe_name": target.name},
                extra={"rule_id": "probe_endpoint_down", "error": str(exc)},
            )
            await self._bus.publish(event)
            logger.warning("APIProbeAgent: %s error: %s", target.name, exc)
