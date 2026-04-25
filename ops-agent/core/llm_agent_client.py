"""
core/llm_agent_client.py — 外部 LLM Agent REST 客户端

OpsAgent 本身不直接调用任何 LLM。所有 AI 能力（告警诊断、规则进化建议、
新规则发现）均通过 RESTful HTTP 请求发送给外部平台上编排运行的"LLM Agent"。

## 外部 LLM Agent 需要实现的接口契约

### POST /analyze-alert
用于对单条告警进行 AI 诊断，返回自然语言分析和增强的操作建议。

请求体：
    {
        "alert_id": "string",
        "rule_id": "string",
        "title": "string",
        "severity": "critical|error|warning|info",
        "summary": "string (原始日志/错误内容，不超过 2000 字符)",
        "source": "log|db|api_probe",
        "target": "string (日志文件路径/接口URL等)",
        "event_count": 1,
        "labels": {"key": "value"}
    }

响应体：
    {
        "analysis": "string (自然语言诊断，说明根因和影响)",
        "suggestions": ["操作建议1", "操作建议2"],
        "confidence": 0.9,
        "root_cause": "string (可选，根因摘要)"
    }

### POST /evolve-rules
用于让 LLM Agent 基于统计数据和未匹配的异常样本，提供规则进化建议。

请求体：
    {
        "current_rules": [
            {"id": "...", "pattern": "...", "weight": 0.9, ...}
        ],
        "stats": {
            "rule_id": {
                "total": 50, "false_positive": 5, "resolved": 40, "open": 5
            }
        },
        "unmatched_samples": [
            {"log_snippet": "string", "frequency": 10, "target": "app.log"}
        ]
    }

响应体：
    {
        "weight_adjustments": [
            {"rule_id": "log_exception", "new_weight": 0.6, "reason": "..."}
        ],
        "new_rules": [
            {
                "id": "suggested_gc_overhead",
                "pattern": "GC overhead limit exceeded",
                "severity": "warning",
                "title": "GC 开销过大 [{target}]",
                "suggestions": ["检查内存使用"],
                "weight": 0.75,
                "enabled": true,
                "tags": ["memory", "jvm"],
                "source": "log"
            }
        ],
        "rules_to_disable": ["rule_id_1"]
    }
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 调用 LLM Agent 超时后的最大重试次数
DEFAULT_RETRY = 2
# 两次重试之间的等待（秒）
RETRY_BACKOFF = 2.0


class LLMAgentClient:
    """
    向外部 LLM Agent 发送 RESTful 请求的异步客户端。

    - 如果 `base_url` 为空或 `enabled=False`，所有调用直接返回 None，
      调用方应将 None 视为"LLM Agent 不可用，使用本地降级逻辑"。
    - 网络错误和超时均会被捕获并记录，不会向上层抛出异常，保证主流程稳定。
    """

    def __init__(
        self,
        base_url: str = "",
        enabled: bool = False,
        timeout_seconds: float = 30.0,
        api_key: str = "",
        api_key_header: str = "X-API-Key",
        retry_attempts: int = DEFAULT_RETRY,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._enabled = enabled and bool(base_url)
        self._timeout = timeout_seconds
        self._api_key = api_key
        self._api_key_header = api_key_header
        self._retries = retry_attempts
        self._session: Any = None  # aiohttp.ClientSession（懒加载）

        if self._enabled:
            logger.info("LLMAgentClient: enabled, base_url=%s", self._base_url)
        else:
            logger.info(
                "LLMAgentClient: disabled (no base_url or enabled=false), "
                "agents will use local fallback logic"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _get_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp  # type: ignore
            headers = {}
            if self._api_key and self._api_key_header:
                headers[self._api_key_header] = self._api_key
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, path: str, payload: dict) -> Optional[dict]:
        """通用 POST 请求，带重试，失败返回 None。"""
        if not self._enabled:
            return None

        url = f"{self._base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(self._retries + 1):
            try:
                import aiohttp  # type: ignore
                session = await self._get_session()
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    body = await resp.text()
                    logger.warning(
                        "LLMAgentClient: %s returned HTTP %d: %s",
                        path, resp.status, body[:200],
                    )
                    return None
            except asyncio.TimeoutError:
                last_error = asyncio.TimeoutError(f"timeout after {self._timeout}s")
                logger.warning(
                    "LLMAgentClient: %s timeout (attempt %d/%d)",
                    path, attempt + 1, self._retries + 1,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "LLMAgentClient: %s error (attempt %d/%d): %s",
                    path, attempt + 1, self._retries + 1, exc,
                )

            if attempt < self._retries:
                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))

        logger.error(
            "LLMAgentClient: %s failed after %d attempts: %s",
            path, self._retries + 1, last_error,
        )
        return None

    # ─────────────────────────────────────────────────────────
    # Public API methods
    # ─────────────────────────────────────────────────────────

    async def analyze_alert(self, alert_context: dict) -> Optional[dict]:
        """
        调用 LLM Agent 对告警进行 AI 诊断。

        返回格式（成功时）：
            {
                "analysis": "自然语言根因分析",
                "suggestions": ["建议1", "建议2"],
                "confidence": 0.9,
                "root_cause": "可选摘要"
            }
        返回 None 时，调用方应保留原有的 suggestions 不变。
        """
        result = await self._post("/analyze-alert", alert_context)
        if result:
            logger.debug(
                "LLMAgentClient: analyze-alert for alert %s → confidence=%.2f",
                alert_context.get("alert_id", "?"),
                result.get("confidence", 0),
            )
        return result

    async def evolve_rules(self, payload: dict) -> Optional[dict]:
        """
        调用 LLM Agent 的规则进化接口。

        返回格式（成功时）：
            {
                "weight_adjustments": [{"rule_id": "...", "new_weight": 0.6, "reason": "..."}],
                "new_rules": [{完整规则定义}],
                "rules_to_disable": ["rule_id_1"]
            }
        返回 None 时，SelfEvolutionAgent 使用纯统计方式更新权重。
        """
        result = await self._post("/evolve-rules", payload)
        if result:
            adj = len(result.get("weight_adjustments", []))
            new = len(result.get("new_rules", []))
            dis = len(result.get("rules_to_disable", []))
            logger.info(
                "LLMAgentClient: evolve-rules → %d adjustments, %d new rules, %d to disable",
                adj, new, dis,
            )
        return result
