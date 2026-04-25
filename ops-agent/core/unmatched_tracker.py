"""
core/unmatched_tracker.py — 未匹配日志行追踪器

当 LogAgent 扫描日志时，未命中任何规则但看起来像异常（包含 ERROR/WARN 等关键词）
的日志行会被记录在此。SelfEvolutionAgent 在进化周期中将这些样本发送给 LLM Agent，
由 LLM Agent 判断是否需要新增规则来捕获这类模式。

设计原则：
  - 纯内存结构，进程重启后清空（样本是瞬时的，不需要持久化）
  - 线程安全（使用 asyncio.Lock）
  - 自动限制内存占用（每个 target 最多保留 MAX_SAMPLES 条）
  - 相同日志行按频率合并，避免重复
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

# 每个 target 最多保留的未匹配样本数
MAX_SAMPLES_PER_TARGET = 100
# 判断是否"看起来像异常"的关键词（大小写不敏感）
_ANOMALY_HINT = re.compile(
    r"(ERROR|WARN|CRITICAL|FATAL|Exception|Error|failed|timeout|refused|denied|overflow|crash)",
    re.IGNORECASE,
)


@dataclass
class UnmatchedSample:
    log_snippet: str
    target: str
    frequency: int = 1
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "log_snippet": self.log_snippet,
            "target": self.target,
            "frequency": self.frequency,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }


class UnmatchedTracker:
    def __init__(self) -> None:
        # target → {snippet_key → UnmatchedSample}
        self._samples: dict[str, dict[str, UnmatchedSample]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    async def record(self, line: str, target: str) -> None:
        """记录一条未匹配的日志行（仅记录看起来像异常的行）。"""
        if not _ANOMALY_HINT.search(line):
            return

        # 截断，避免占用过多内存
        snippet = line[:300].strip()
        if not snippet:
            return

        async with self._lock:
            bucket = self._samples[target]
            if snippet in bucket:
                bucket[snippet].frequency += 1
                bucket[snippet].last_seen = datetime.now(timezone.utc)
            else:
                if len(bucket) >= MAX_SAMPLES_PER_TARGET:
                    # 踢掉频率最低的样本（保留有价值的高频样本）
                    min_key = min(bucket, key=lambda k: bucket[k].frequency)
                    del bucket[min_key]
                bucket[snippet] = UnmatchedSample(log_snippet=snippet, target=target)

    async def get_top_samples(self, top_n: int = 20) -> list[dict]:
        """返回跨所有 target 的频率最高的 top_n 个未匹配样本。"""
        async with self._lock:
            all_samples: list[UnmatchedSample] = []
            for bucket in self._samples.values():
                all_samples.extend(bucket.values())
            all_samples.sort(key=lambda s: s.frequency, reverse=True)
            return [s.to_dict() for s in all_samples[:top_n]]

    async def clear(self) -> None:
        """进化周期后清空，避免重复分析旧样本。"""
        async with self._lock:
            self._samples.clear()

    async def total_count(self) -> int:
        async with self._lock:
            return sum(len(b) for b in self._samples.values())
