# OpsAgent — 运维超级智能体

> **目标**：一个能实时感知系统状态、主动诊断故障、协助日常运维，并接入现有监控界面的 AI 智能体集群。

---

## 架构概览

```
                         ┌─────────────────────────────────────────────────┐
                         │              OpsAgent（本项目）                    │
                         │                                                   │
  日志文件 ──► LogAgent ──┐│                                                   │
  数据库表 ──► DBAgent ───┼┤── EventBus ──► OrchestratorAgent ──► AlertStore  │
  HTTP接口 ──► ProbeAgent ┘│                      │                   │        │
                         │                      │                   ▼        │
                         │                      │        SelfEvolutionAgent  │
                         │                      │                   │        │
                         └──────────────────────┼───────────────────┼────────┘
                                                │ REST POST          │ REST POST
                                                │ /analyze-alert     │ /evolve-rules
                                                ▼                   ▼
                                    ┌─────────────────────────────────────┐
                                    │   外部 LLM Agent（另一平台编排生成）    │
                                    │                                      │
                                    │  ● 接入 LLM（GPT/Claude/本地模型等） │
                                    │  ● 对告警做 AI 根因分析               │
                                    │  ● 发现未匹配的新错误模式，建议新规则   │
                                    └─────────────────────────────────────┘
                                                │
                                      FastAPI Server（本项目对外暴露）
                                      /api/v1/alerts   告警查询
                                      /api/v1/status   系统状态
                                      /metrics         Prometheus 指标
```

### 关键架构原则

> **本项目所有 Agent 均不直接调用 LLM。**
> 
> AI 能力（告警根因分析、新规则建议）通过 RESTful HTTP 请求发给外部"LLM Agent"。
> LLM Agent 由另一个平台编排生成，与本项目完全解耦。
> 若 LLM Agent 不可用，本项目自动降级为纯统计/规则逻辑，正常运行不受影响。

### 各组件职责

| 组件 | 类型 | 职责 |
|---|---|---|
| **LogAgent** | 本地 Agent | 实时 tail 日志文件，正则匹配异常行，发布事件；记录未匹配可疑行 |
| **DBAgent** | 本地 Agent | 定期执行 SQL，将异常记录转化为事件 |
| **APIProbeAgent** | 本地 Agent | 定期 HTTP 探测接口，检测可用性和响应时间 |
| **OrchestratorAgent** | 本地 Agent | 事件去重聚合、生成告警；调用 LLM Agent 补充 AI 诊断 |
| **SelfEvolutionAgent** | 本地 Agent | 统计驱动的规则权重调整 + 调用 LLM Agent 发现新规则 |
| **LLMAgentClient** | REST 客户端 | 封装所有对外部 LLM Agent 的调用（非 Agent 本体） |
| **外部 LLM Agent** | 外部服务 | 由另一平台编排，直接调用 LLM，通过 REST 提供 AI 能力 |

---

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
cd ops-agent

# 1. 按需编辑配置（可配置外部 LLM Agent 地址）
vim config/config.yaml

# 2. 一键启动
docker compose up -d

# 3. 查看告警
curl http://localhost:8000/api/v1/alerts | python3 -m json.tool

# 4. 查看 Prometheus 指标
curl http://localhost:8000/metrics
```

### 方式二：本地直接运行

```bash
cd ops-agent
pip install -r requirements.txt
python main.py
```

---

## API 参考

### OpsAgent 对外暴露的 API

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/v1/alerts` | 告警列表（支持 status/severity 过滤） |
| GET | `/api/v1/alerts/{id}` | 告警详情（含原始事件、AI 分析结果） |
| PATCH | `/api/v1/alerts/{id}/status` | 更新告警状态（标记误报/已解决，影响自进化） |
| GET | `/api/v1/status` | 系统整体状态 |
| GET | `/api/v1/status/health` | 健康检查（供 Docker/k8s 使用） |
| GET | `/metrics` | Prometheus 指标（供 Grafana 接入） |

### 外部 LLM Agent 需实现的 API 契约

OpsAgent 会调用以下两个接口（详见 `core/llm_agent_client.py`）：

#### `POST /analyze-alert` — 告警 AI 诊断

**请求体**：
```json
{
  "alert_id": "uuid",
  "rule_id": "log_oom",
  "title": "检测到内存溢出 [app.log]",
  "severity": "critical",
  "summary": "OutOfMemoryError: Java heap space ...",
  "source": "log",
  "target": "/logs/app.log",
  "event_count": 3,
  "labels": {"source": "log", "target": "/logs/app.log"}
}
```

**响应体**：
```json
{
  "analysis": "检测到 Java 堆内存溢出。根据栈跟踪推断，可能是大量对象未被 GC 回收...",
  "suggestions": ["增加 JVM 内存参数 -Xmx4g", "检查是否存在内存泄漏"],
  "confidence": 0.9,
  "root_cause": "JVM 堆内存耗尽"
}
```

#### `POST /evolve-rules` — 规则进化建议

**请求体**：
```json
{
  "current_rules": [...],
  "stats": {
    "log_oom": {"total": 50, "false_positive": 2, "resolved": 40, "open": 8}
  },
  "unmatched_samples": [
    {"log_snippet": "WARN GC overhead limit exceeded", "frequency": 15, "target": "app.log"}
  ]
}
```

**响应体**：
```json
{
  "weight_adjustments": [
    {"rule_id": "log_exception", "new_weight": 0.6, "reason": "触发频繁但处理率低"}
  ],
  "new_rules": [
    {
      "id": "log_gc_overhead",
      "pattern": "GC overhead limit exceeded",
      "severity": "warning",
      "source": "log",
      "title": "GC 开销过大 [{target}]",
      "suggestions": ["检查内存使用", "考虑增加堆大小"],
      "weight": 0.8,
      "enabled": true,
      "tags": ["memory", "jvm"]
    }
  ],
  "rules_to_disable": []
}
```

---

## 配置说明

### 接入外部 LLM Agent

编辑 `config/config.yaml`：

```yaml
llm_agent:
  enabled: true
  base_url: http://your-llm-agent:9000   # 外部 LLM Agent 地址
  timeout_seconds: 30
  api_key: "your-api-key"                # 可选，鉴权 Key
  api_key_header: "X-API-Key"            # 可选，鉴权 Header 名
  retry_attempts: 2
```

> LLM Agent 为空或 `enabled: false` 时，系统自动降级为纯统计逻辑，正常运行不受影响。

### 添加日志监控目标

```yaml
log_agent:
  targets:
    - path: /logs/myapp/app.log
      name: myapp
```

### 添加数据库监控

```yaml
db_agent:
  enabled: true
  targets:
    - name: job_monitor
      dsn: ******host:3306/dbname
      query: |
        SELECT id, status, error_msg, updated_at 
        FROM job_status 
        WHERE status='failed' 
          AND updated_at > NOW() - INTERVAL 5 MINUTE
      message_template: "Job {id} failed: {error_msg}"
```

### 添加 HTTP 接口探针

```yaml
api_probe_agent:
  targets:
    - name: payment_service
      url: http://payment-service:8080/health
      expected_status: 200
      severity_on_failure: critical
      poll_interval_seconds: 30
```

### 与 Grafana 集成

在 Grafana 中添加 Prometheus 数据源 URL：`http://ops-agent:8000`

可用指标：
- `ops_agent_alerts_total{status="open"}` — 当前未处理告警数
- `ops_agent_events_published_total` — 总事件数
- `ops_agent_uptime_seconds` — 运行时长

---

## 自我进化机制

`SelfEvolutionAgent` 每小时（可配置）运行一次进化周期，双通道并行：

### 通道 A：统计驱动（本地，始终运行）
1. 读取各规则的统计数据（触发次数、误报次数、解决次数）
2. 计算误报率 → 调整 weight
3. 将新 weight 写回 `config/rules.yaml`
4. 热重载到所有 Agent（无需重启）

### 通道 B：LLM Agent 驱动（REST，可选）
1. 收集 LogAgent 未命中规则的可疑日志行（UnmatchedTracker）
2. 将统计数据 + 未匹配样本发送给外部 LLM Agent (`POST /evolve-rules`)
3. LLM Agent 返回：权重调整建议 + 新规则建议 + 需禁用的规则
4. 与通道 A 结果合并，写回 `rules.yaml`
5. 进化记录写入 `/data/learning/evolution_log.jsonl`

**如何反馈告警质量**：通过 API 标记告警状态

```bash
# 标记误报（降低对应规则的权重）
curl -X PATCH http://localhost:8000/api/v1/alerts/{id}/status \
  -H "Content-Type: application/json" \
  -d '{"status": "false_positive"}'

# 标记已解决（提升规则置信度）
curl -X PATCH http://localhost:8000/api/v1/alerts/{id}/status \
  -H "Content-Type: application/json" \
  -d '{"status": "resolved"}'
```

---

## 路线图

- [x] **Milestone 1**：数据采集层（LogAgent + DBAgent + APIProbeAgent + OrchestratorAgent + SelfEvolutionAgent + API Server）
- [x] **架构改造**：解耦 LLM 调用，所有 AI 能力通过 RESTful API 对接外部 LLM Agent
- [ ] **Milestone 3**：ActionAgent（受控执行运维操作：重启服务、清理磁盘等，支持人工审批）
- [ ] **Milestone 4**：Webhook 推送（主动推送告警到钉钉/Slack/PagerDuty）

