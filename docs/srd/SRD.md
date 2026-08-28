# 软件需求文档（SRD）— 合同审批审查 Agent

| 项 | 内容 |
|----|------|
| 版本 | v2.0（LangChain ReAct 引擎 · 零降级） |
| 需求基线 | 《大模型项目实战》§2.4 合同审批审查系统 |
| 关联仓库 | github.com/nobodycare-no/contract-review-agent |

> v2.0 变更：引擎迁移至 LangChain 官方 Agent（LangGraph）；废除全部降级通道（零降级）；
> 工具扩至十个；新增闭环闸门/纠偏轮/同单互斥/批量账本/诚实计时；AI 以原文修正解析初稿。

## 1. 项目概述

面向企业合同审批场景的自动审查系统：**LangChain ReAct Agent 通过原生 function-calling 自主调度十个工具**（拉取待办 / 审批详情 / 附件下载 / 文档解析 / 基本信息 AI 修正 / 原文检索 / 规则清单 / 规则初筛 / 结果保存 / 评论回写），完成完整闭环；**不代替人工审批**，产出由 AI 亲笔撰写的风险审查意见供法务审核人参考。

**与项目 A 的本质差异**：无 RAG / 无向量库——核心是 **Agent 运行时工程 + 规则引擎 + 状态机**。
**产品铁律：零降级**。LLM 不可用或闭环未完成 → 任务显式 `blocked`（人话原因 + 轨迹尾巴），绝不伪造成功。

## 2. 用户角色（规范 §2.4.2）

| 角色 | 定位 | 交互方式 |
|------|------|---------|
| 审批系统 | 本地审批域（V1 起为本系统自有业务表，mock 已物理删除） | approval_store 网关 |
| 法务审核人 | 查看审查结果、证据定位、留痕时间线、原件查看 | Vue3 Web 工作台 :18000 |
| 系统管理员 | 维护规则、查看日志、重试阻塞任务、重置演示数据 | Admin API（Token 保护） |
| Agent（AI） | 原生 function-calling 自主调度十工具完成闭环 | 十工具接口 |

## 3. 功能需求（FR-CRA-xx）

### FR-A 接入能力
| 编号 | 需求 | 优先级 |
|------|------|--------|
| A1 | 待办拉取：approval_code/title/applicant/time/附件数量 | P0 |
| A2 | 唯一业务标识去重：重复拉取只更新不重建 | P0 |
| A3 | 附件下载→本地存储→元数据入库（download_status） | P0 |
| A4 | 详情获取：审批信息+表单数据+附件清单+状态 | P0 |

### FR-B 解析能力
| 编号 | 需求 | 优先级 |
|------|------|--------|
| B1 | 四格式解析：docx/pdf/md·txt/png·jpg(OCR) | P0 |
| B2 | 基本信息 8 字段提取初稿（正则），含 value+pos+status | P0 |
| B3 | 八类条款定位（付款/交付/验收/违约/保密/数据/知识产权/争议） | P0 |
| B4 | **AI 修正**：模型用 search_contract_text 拉原文核对初稿，错漏经 submit_basic_info 修正（status=ai_verified）——解析器只是初稿 | P0 |
| B5 | 解析失败必须记录原因，禁止空结果；工具异常轨迹必须携带原因 | P0 |

### FR-C 规则审查
| 编号 | 需求 | 优先级 |
|------|------|--------|
| C1 | 规则库 ≥11 类（预付款/付款周期/自动续约/违约/管辖地/主体/金额/保密/数据/知产/验收） | P0 |
| C2 | 三种 match_mode：keyword / regex / absence | P0 |
| C3 | 命中输出：规则名/风险等级/命中证据原文/位置/建议说明 | P0 |
| C4 | 规则结果仅作参考线索；总风险等级由模型综合原文独立给出 | P0 |
| C5 | 规则可启停/编辑（管理员）；模型亦可经 list_review_rules 自主查阅 | P1 |
| C6 | AI 裁量增量层：语义级增量风险，强制原文证据；该层失败留痕不阻断 | P1 |

### FR-D 输出与回写
| 编号 | 需求 | 优先级 |
|------|------|--------|
| D1 | 结果保存：总风险等级（枚举归一 高/中/低→high/medium/low）+摘要+关注点+评论全文 | P0 |
| D2 | **评论完全由模型亲笔撰写**；comment_text 缺失即 VALIDATION_ERROR，禁止模板兜底 | P0 |
| D3 | 回写至本地审批域评论区（幂等守卫：success 短路时仍闭环任务状态） | P0 |
| D4 | 回写状态机 not_written→writing→success/failed | P0 |
| D5 | 回写日志落库 comment_logs | P0 |
| D6 | blocked 触发面：附件缺失/解析为空/运行失败（含 LLM 不可用、递归耗尽、闭环未完成），原因人话+处理指引；retry 即真跑引擎 | P0 |

### FR-E Agent 循环（LangChain ReAct）
| 编号 | 需求 | 优先级 |
|------|------|--------|
| E1 | LangChain create_agent（LangGraph）驱动十工具，原生 function-calling，**工具选择与顺序由模型逐轮决策**（提示词给推荐路径而非铁律） | P0 |
| E2 | 工具 schema 从 TOOLS_SCHEMA 动态生成强类型 args（只声明 dispatch 真实消费的参数） | P0 |
| E3 | **闭环闸门**：图跑完≠闭环——未写回评论（dry-run 未保存）即 RuntimeError；漏写回但已保存时自动纠偏一轮；递归上限耗尽可能纠偏一次 | P0 |
| E4 | 全链路留痕：工具调用（含 EXC 原因）/状态迁移/写回动作落 task_logs，详情页时间线呈现 | P0 |
| E5 | 步数预算 recursion_limit（agent_max_steps×2）；响应携带 elapsed_ms 真实墙钟 | P0 |

### FR-F 管理与可用性
| 编号 | 需求 | 优先级 |
|------|------|--------|
| F1 | blocked/done 单重试 = 复位后真跑引擎；重试崩溃显式落回 blocked | P0 |
| F2 | 规则启停/编辑 API（Admin Token） | P1 |
| F3 | 运行留痕查询 API（按 task） | P1 |
| F4 | reset-demo 演示数据重置端点 | P1 |
| F5 | Vue3 Web 工作台：列表/详情（原件查看·AI核对信息·留痕时间线·再次审查）/批量队列 | P0 |
| F6 | 批量送审 batch_id 进度账本（done/skipped），忙单跳过计数 | P0 |
| F7 | 同单互斥：并发双跑 409 人话拒绝 | P0 |

### FR-G 运行时（V2 形态）
| 编号 | 需求 | 优先级 |
|------|------|--------|
| G1 | dry-run：全链路真实执行、跳过评论外呼（闸门降级为校验已保存） | P0 |
| G2 | 指标面 /metrics（Prometheus）+ 组件级 /health | P1 |
| G3 | 输出护栏与幂等守卫（write_status=success 拒绝重复外呼，去重时仍闭环状态） | P0 |
| G4 | 时区：容器 TZ=Asia/Shanghai，新落库时间为中国时间 | P1 |
| G5 | 启动/批量自愈：孤儿任务显式 blocked，原因只陈述可观察事实 | P0 |

> v1.x 的断点恢复(agent_runs)/熔断器/双通道 JSON 协议/轨迹录制回放随降级哲学一并废除；
> RunController 代码保留于 agent_loop.py 仅供 legacy 开关对比，非交付面。

## 4. 非功能需求（NFR）

| 编号 | 类别 | 要求 |
|------|------|------|
| N01 | 性能 | 单合同全闭环 30~90s（GPU 在线；重审同合同经前缀缓存显著加速） |
| N02 | 可靠性 | **零降级**：LLM 不可用/闭环未完成 → blocked（人话原因），绝不产出伪造成功 |
| N03 | 安全 | Admin Token；附件路径防护；同单互斥防双跑 |
| N04 | 可观测 | 结构化 JSON 日志 + task_logs 工具留痕 + /metrics + /health + elapsed_ms |
| N05 | 可部署 | 双容器 compose（mysql/app）；TZ=Asia/Shanghai；GPU 卡 deploy/GPU_VLLM_START.md |
| N06 | 测试 | pytest 88+ 用例（含 LC_LIVE=1 门控真机用例）；V2 探针 11 项 |
| N07 | 环境隔离 | 独立仓库/网络/数据卷；GPU 按需开机 |

## 5. 验收标准（规范 §2.4.12 七条映射）

| AC | 规范原文 | 验证方式 |
|----|---------|---------|
| AC-1 | 拉取待办并去重 | V2 探针：重种5单+两次拉取视图一致 |
| AC-2 | 下载附件并保存记录 | download_status=done + 留痕 |
| AC-3 | 解析文档与扫描件 | 金额归一化断言 + OCR 编号命中 |
| AC-4 | 规则审查证据与等级 | 高风险合同 7 命中 overall=high |
| AC-5 | 保存并回写评论区 | success + 幂等 deduped + 中文枚举归一 |
| AC-6 | 异常阻塞并支持重试 | 空单拒绝 + blocked 重试即真跑 |
| AC-7 | 完整闭环演示 | V2 探针 AGENT-RUN（elapsed_ms/trace 取证）+ LC_LIVE 真机用例 |
