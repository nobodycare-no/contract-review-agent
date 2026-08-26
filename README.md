# contract-review-agent · 合同审批审查 Agent

> 自研生产级 Agent Harness × 七大工具 × 双通道 Function-Calling × 规则引擎
> —— 合同风险自动审查并回写审批评论区（《大模型项目实战》§2.4）

无 RAG、无向量库——本项目的主战场是 **Agent 运行时工程**：

| Harness 能力 | 说明 |
|--------------|------|
| 双通道 Function-Calling | vLLM 原生 tools API 为主，能力探测失败自动降级提示词 JSON 协议（ADR-B7） |
| RunController 事件溯源 | 每步消息快照落库 `agent_runs`，进程崩溃后 **POST /runs/{id}/resume 断点恢复**（ADR-B8） |
| 三维预算优雅终结 | 步数≤12（规范）× token × 墙钟，任一触顶走强制 save+write 收敛，绝不悬挂 |
| 熔断器 | LLM 连续失败自动开路 60s，期间直接确定性通道，故障期演示不挂死 |
| dry-run 模式 | `?dry_run=true` 全链路演练、不外发评论——写操作必备的安全阀 |
| 幂等回写守卫 | success 状态拒绝重复外呼，防重复评论污染审批单 |
| 可观测 | 结构化 JSON 日志(run_id/task_id 关联) + `GET /metrics`(Prometheus) + 组件级 `/health` |
| 提示词版本注册表 | prompt_version/model/channel 入运行记录，结果可复现可审计 |
| 轨迹录制回放 | 真实 GPU 会话录成 fixtures，CI 无 GPU 回放回归（ADR-B9，VCR 式） |

## 快速启动

```bash
conda activate demo_env
pip install -r backend/requirements.txt
cp deploy/.env.example .env       # GPU 开机时填 LLM_BASE_URL
cd deploy && docker compose up -d --build
docker compose exec app python -m app.tools.bootstrap   # 灌规则库+注册mock审批单
```

## 一键闭环演示

```bash
docker compose exec app python -m app.tools.demo              # 彩色五阶段全程
docker compose exec app python -m app.tools.demo --dry-run    # 演练模式：不外发评论
```

## 文档

| 文档 | 路径 |
|------|------|
| 需求文档 SRD | [docs/srd/SRD.md](docs/srd/SRD.md) |
| 架构文档 SAD（9 条 ADR） | [docs/sad/SAD.md](docs/sad/SAD.md) |
| 详细设计 SDD（Harness 规格 §7） | [docs/sdd/SDD.md](docs/sdd/SDD.md) |
| 开发手册（切片计划） | [docs/开发手册.md](docs/开发手册.md) |
| 部署手册（云端生产） | [docs/部署手册.md](docs/部署手册.md) |
| 测试评估报告 | [docs/测试评估报告.md](docs/测试评估报告.md) |
| 需求覆盖验证报告 | [docs/需求覆盖验证报告.md](docs/需求覆盖验证报告.md) |
