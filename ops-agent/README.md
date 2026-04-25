# OpsAgent — 运维超级智能体

> **目标**：一个能实时感知系统状态、主动诊断故障、协助日常运维，并接入现有监控界面的 AI 智能体集群。

---

## 架构概览

```
日志文件 ──► LogAgent ──────┐
数据库表 ──► DBAgent ────────┼──► EventBus ──► OrchestratorAgent ──► AlertStore
HTTP接口 ──► APIProbeAgent ──┘                                           │
                                                                         ▼
                                                              SelfEvolutionAgent
                                                              （分析规则效果，
                                                               自动更新 rules.yaml）
                                                                         │
                                                                         ▼
                                                                   FastAPI Server
                                                              /api/v1/alerts  （告警列表）
                                                              /api/v1/status  （系统状态）
                                                              /metrics        （Prometheus）
```

### 各 Agent 职责

| Agent | 职责 |
|---|---|
| **LogAgent** | 实时 tail 日志文件，正则匹配异常行，发布事件 |
| **DBAgent** | 定期执行 SQL，将异常记录转化为事件 |
| **APIProbeAgent** | 定期 HTTP 探测接口，检测可用性和响应时间 |
| **OrchestratorAgent** | 事件去重聚合、关联诊断、生成告警（含建议操作） |
| **SelfEvolutionAgent** | **自我进化**：分析规则误报率，自动调整 `rules.yaml` 中的 weight |

---

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
cd ops-agent

# 1. 按需编辑配置
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

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/v1/alerts` | 告警列表（支持 status/severity 过滤） |
| GET | `/api/v1/alerts/{id}` | 告警详情（含原始事件） |
| PATCH | `/api/v1/alerts/{id}/status` | 更新告警状态（标记误报/已解决） |
| GET | `/api/v1/status` | 系统整体状态 |
| GET | `/api/v1/status/health` | 健康检查（供 Docker/k8s 使用） |
| GET | `/metrics` | Prometheus 指标（供 Grafana 接入） |

### 与 Grafana 集成

在 Grafana 中添加 Prometheus 数据源：
- URL：`http://ops-agent:8000`（Docker 网络内）或 `http://localhost:8000`

可用指标：
- `ops_agent_alerts_total{status="open"}` — 当前未处理告警数
- `ops_agent_events_published_total` — 总事件数
- `ops_agent_uptime_seconds` — 运行时长

---

## 配置说明

### 添加日志监控目标

编辑 `config/config.yaml`：

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
      dsn: mysql+aiomysql://user:password@host:3306/dbname
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

---

## 自我进化机制

`SelfEvolutionAgent` 每小时（可配置）运行一次进化周期：

1. 读取各规则的统计数据（触发次数、误报次数、解决次数）
2. 计算误报率：
   - 误报率 ≥ 30%：降低规则 weight（× 0.85）
   - 误报率 < 5% 且解决率 > 70%：提升 weight（× 1.05）
3. 将新 weight 写回 `config/rules.yaml`
4. 热重载到所有 Agent（无需重启）
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
- [ ] **Milestone 2**：LLM 诊断增强（接入 OpenAI/本地模型，对异常日志给出自然语言解读）
- [ ] **Milestone 3**：ActionAgent（受控执行运维操作：重启服务、清理磁盘等，支持人工审批）
- [ ] **Milestone 4**：Webhook 推送（主动推送告警到钉钉/Slack/PagerDuty）
