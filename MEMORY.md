# MEMORY.md — 长期记忆

> 本文件是 AI 助理 Coda 的长期记忆库。  
> 每次新对话开始时读取，保持跨会话的上下文连续性。  
> 只写稳定的、长期有效的信息；临时过程记录放 `memory/log.md`。

---

## 仓库背景

- **仓库名**：TestAI001
- **仓库主人**：jiangt9527
- **用途**：个人 AI 助理的长期工作空间，探索 GitHub Copilot 作为持续驻留助手的可能性
- **创建时间**：2026-04-25

---

## 偏好与约定

- 工作语言：中文优先，技术内容用英文
- 提交风格：简洁中文提交信息
- 文件风格：Markdown，保持简洁，不过度组织

---

## 进行中的任务

### 🎯 OpsAgent — 运维超级智能体

**目标**：构建一个能实时感知系统状态、主动诊断故障、协助日常运维、并接入现有监控界面的 AI 智能体集群。

**四个里程碑**：
- Milestone 1：数据采集层（LogAgent + DBAgent + APIProbeAgent + 事件总线）
- Milestone 2：大脑层（OrchestratorAgent：事件关联、规则诊断、AI 解读）
- Milestone 3：执行层（ActionAgent：受控运维操作 + 审计日志）
- Milestone 4：接口层（REST API + Webhook + Prometheus Metrics，供 Grafana/Zabbix 接入）

**当前进度**：✅ Milestone 1 + 架构改造完成。

**核心架构约束**：
- 本项目所有 Agent 不直接调用 LLM
- AI 能力（告警诊断、规则进化）通过 REST 调用外部 LLM Agent
- LLM Agent 由另一平台编排，本项目只调用其 RESTful 接口
- LLM Agent 不可用时自动降级为纯统计逻辑

**LLM Agent REST 接口契约**（外部 LLM Agent 需实现）：
- `POST /analyze-alert` → 告警 AI 诊断（分析 + 建议）
- `POST /evolve-rules`  → 规则进化建议（权重调整 + 新规则发现）

**项目目录**：`ops-agent/`

**技术选型**：Python 3.11+、FastAPI、asyncio、aiohttp、PyYAML、Docker Compose

**下一步**：Milestone 3（ActionAgent 受控执行）或 Milestone 4（Webhook 推送）

---

## 重要决策记录

| 日期 | 决策 | 原因 |
|---|---|---|
| 2026-04-25 | 初始化仓库为个人 AI 空间，创建 AGENTS.md / MEMORY.md / memory/log.md | 实现跨会话记忆与角色持久化 |

---

## 技能与工具

> 见 `memory/skills.md`（暂未创建，按需添加）
