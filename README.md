# contract-review-agent · 合同审批审查 Agent

> **分支 feat/langchain-react-gpu-only（V2）**：LangChain 官方 Agent（LangGraph 引擎）× 十工具 × **零降级**
> —— 合同风险自动审查并回写审批评论区（《大模型项目实战》§2.4）。

## V2 核心事实（与 main v1.x 的差异见 [docs/V2分支现状.md](docs/V2分支现状.md)）

| 维度 | V2 现状 |
|------|---------|
| 引擎 | `langchain.agents.create_agent`（底层 LangGraph），vLLM OpenAI 兼容原生 tools |
| 工具 | **10 个**：九业务工具 + 规则初筛；签名以 ctx 驱动，schema 只声明真实消费的参数 |
| 降级 | **不存在**。LLM 失败/闭环未完成 → 异常上抛 → 任务显式 blocked（人话原因 + 轨迹尾部） |
| 闭环闸门 | 图跑完≠闭环：未写回审批评论 = 任务失败，绝不返回假成功 |
| 同单互斥 | 双击/批量并发第二请求 409 拒绝；成功后释放 |
| 诚实计时 | 响应携带 `elapsed_ms`（agent.invoke 真实墙钟），前端直显 |
| 状态机 | `done→parsing`（再次审查）、`blocked→parsing`（重试即真跑引擎）；自愈原因只陈述事实 |
| 批量 | `batch_id` 进度账本（done/skipped），前端轮询 `/app/batch/{id}` |
| 时区 | compose `TZ=Asia/Shanghai`，新落库时间为中国时间 |

## 快速启动

```bash
cp deploy/.env.example .env       # 填 GPU 的 LLM_BASE_URL / LLM_MODEL
cd deploy && docker compose up -d --build
# GPU 侧 vLLM 启动参数与 30 秒自检：deploy/GPU_VLLM_START.md（必须含
# --enable-auto-tool-choice --tool-call-parser hermes，否则一切工具调用 400）
```

## 验证

```bash
cd backend && python -m pytest tests -q        # 87 passed, 2 skipped(真机用例 LC_LIVE=1)
# 真机闭环：前台 :18000 上传合同 → AI 审查 → 详情页看留痕时间线与服务端耗时
```

## 文档

| 文档 | 路径 |
|------|------|
| **V2 分支现状（行为权威）** | [docs/V2分支现状.md](docs/V2分支现状.md) |
| 需求文档 SRD（main 基线） | [docs/srd/SRD.md](docs/srd/SRD.md) |
| 架构文档 SAD（main 基线 + 9 ADR） | [docs/sad/SAD.md](docs/sad/SAD.md) |
| 详细设计 SDD（main 基线） | [docs/sdd/SDD.md](docs/sdd/SDD.md) |
| 开发手册 | [docs/开发手册.md](docs/开发手册.md) |
| 部署手册（含 GPU 卡） | [docs/部署手册.md](docs/部署手册.md) · [deploy/GPU_VLLM_START.md](deploy/GPU_VLLM_START.md) |
| 测试评估报告 | [docs/测试评估报告.md](docs/测试评估报告.md) |
| 需求覆盖验证报告（含 V2 复验） | [docs/需求覆盖验证报告.md](docs/需求覆盖验证报告.md) |

> SRD/SAD/SDD/两手册描述 main v1.x 基线，文档顶部均有指向 V2 现状文档的准星注记。
