# memory/log.md — 会话日志

> 每次对话结束时追加一条记录。格式统一，不删除历史。

---

## 2026-04-25

- 初始化仓库为个人 AI 工作空间：创建 AGENTS.md（角色定义与工作规则）、MEMORY.md（长期记忆）、memory/log.md（本文件）
- 更新 README.md 说明仓库用途
- 遗留问题：无，等待仓库主人的下一个任务

---

## 2026-04-25（第二次会话）

- 接收到目标：构建"运维超级智能体"OpsAgent 系统
- 制定了完整的四阶段路线图（感知 → 理解 → 动手 → 接入监控界面）
- 更新 MEMORY.md，写入 OpsAgent 目标与技术选型
- 遗留问题：等待下次会话启动 Milestone 1 MVP（LogAgent + APIServer + Docker Compose）

---

## 2026-04-25（第三次会话）

- 新增需求：立即动手实现，并且系统要能自我进化
- 实现了完整的 Milestone 1：ops-agent/ 项目（Python 3.11 + FastAPI + asyncio）
- 实现组件：LogAgent / DBAgent / APIProbeAgent / OrchestratorAgent / SelfEvolutionAgent / AlertStore / REST API + Prometheus /metrics
- SelfEvolutionAgent：每小时分析规则误报率，自动更新 rules.yaml 中的 weight，热重载到所有 Agent
- 集成测试通过：日志写入 → 事件总线 → 告警生成 全流程验证 OK
- Docker Compose 一键部署配置完成
- 遗留问题：下次会话可选择推进 Milestone 2（LLM 诊断）或 Milestone 3（ActionAgent 受控执行）

---

## 2026-04-25（第四次会话）

- 新需求：本项目 Agent 不能直接调用 LLM，所有 AI 能力通过 REST 调用外部 LLM Agent
- 新增 core/llm_agent_client.py：封装对外部 LLM Agent 的所有 HTTP 调用，含契约文档
- 新增 core/unmatched_tracker.py：记录未命中规则的可疑日志行，供 LLM Agent 发现新规则
- 改造 OrchestratorAgent：告警生成后异步调用外部 LLM Agent /analyze-alert 做 AI 诊断
- 改造 SelfEvolutionAgent：双通道进化（统计+LLM Agent），从外部获取新规则建议
- 改造 LogAgent：接入 UnmatchedTracker，记录未匹配可疑行
- 更新 config.yaml：新增 llm_agent 配置段
- 更新 README.md：重写架构图，清晰说明 LLM 解耦设计
- 集成测试通过（含降级路径验证）
- 遗留问题：下次可推进 Milestone 3（ActionAgent）或 Milestone 4（Webhook）
