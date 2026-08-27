# 软件需求文档（SRD）— 合同审批审查 Agent

| 项 | 内容 |
|----|------|
| 版本 | v1.2（生产级 Harness 升级） |
| 需求基线 | 《大模型项目实战》§2.4 合同审批审查系统 |
| 关联仓库 | github.com/nobodycare-no/contract-review-agent |

> v1.1 变更：修复 FR-D 表格错乱；N05 扩展为含云端部署要求。
> v1.2 变更：新增 FR-E5 运行预算、FR-G 生产化运行时需求组；N04 升级为结构化可观测体系。

## 1. 项目概述

面向企业合同审批场景的自动审查系统：Agent 通过 **Function-Calling 自主调用七个工具**（拉取待办 / 审批详情 / 附件下载 / 文档解析 / 规则审查 / 结果保存 / 评论回写），完成完整闭环；**不代替人工审批**，产出风险审查意见供法务审核人参考。

**与项目 A 的本质差异**：无 RAG / 无向量库 / 无检索——核心是 **工具调用 Agent + 规则引擎 + 状态机**。

## 2. 用户角色（规范 §2.4.2）

| 角色 | 定位 | 交互方式 |
|------|------|---------|
| 审批系统 | 外部系统（本项目以 mock 容器仿真） | REST：待办/详情/附件/评论接收 |
| 法务审核人 | 查看审查结果、证据定位、关注点 | 简易 Web 工作台 |
| 系统管理员 | 维护规则、查看日志、重试阻塞任务 | Admin API（Token 保护）+ CLI |
| Agent（AI） | Function-calling 驱动七工具完成闭环 | 七大工具接口 |

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
| B2 | 基本信息 8 字段提取（标题/编号/甲乙方/金额/币种/生效/到期），含 value+pos+status | P0 |
| B3 | 八类条款定位（付款/交付/验收/违约/保密/数据/知识产权/争议），含片段+位置+present/absent | P0 |
| B4 | LLM 增强提取（结构化 JSON）；失败回落正则结果 | P1 |
| B5 | 解析失败必须记录原因，禁止空结果 | P0 |

### FR-C 规则审查
| 编号 | 需求 | 优先级 |
|------|------|--------|
| C1 | 规则库 ≥11 类：预付款比例/付款周期/自动续约/违约责任缺失/管辖地/主体缺失/金额缺失/保密缺失/数据处理/知识产权缺失/验收标准缺失 | P0 |
| C2 | 三种 match_mode：keyword / regex / absence（缺失探测） | P0 |
| C3 | 命中输出：规则名/风险等级/命中证据原文/位置/建议说明 | P0 |
| C4 | 总风险等级 = 命中规则最高级聚合；审批关注点列表 | P0 |
| C5 | 规则可启停/编辑（管理员） | P1 |
| C6 | LLM 自由裁量层（ADR-B10）：规则审查后对全文做语义级增量风险分析，每条输出必须引用原文证据，来源标记 AI_DISCRETIONARY 与规则命中区分；总评等级只升不降；LLM 不可用时静默降级为纯规则结果 | P1 |

### FR-D 输出与回写
| 编号 | 需求 | 优先级 |
|------|------|--------|
| D1 | 结果保存：总风险等级+摘要+关注点 JSON+评论全文 | P0 |
| D2 | 评论生成：结构化模板 + LLM 摘要润色（降级时纯模板） | P0 |
| D3 | 跨服务 HTTP 回写至 mock 审批评论区 | P0 |
| D4 | 回写状态机 not_written→writing→success/failed | P0 |
| D5 | 回写日志落库 comment_logs | P0 |
| D6 | blocked 触发面：附件缺失 / 图片无法识别 / 文档内容为空 / 接口调用失败，四类统一进入 blocked 并记录原因；retry 按 block_stage 回溯 parsing 或 reviewing | P0 |

### FR-E Agent 循环
| 编号 | 需求 | 优先级 |
|------|------|--------|
| E1 | Function-calling 循环驱动七工具，步数上限 12 | P0 |
| E2 | 七工具 JSON Schema 暴露给模型（规范 §2.4.10 签名逐一对应） | P0 |
| E3 | 步数耗尽或模型未回写时**强制兜底执行回写工具** | P0 |
| E4 | 全链路日志（fetch/download/parse/rule/write/agent 六类） | P0 |
| E5 | 运行预算三维化：步数 × token 上限 × 墙钟时限，任一触顶进入优雅终结（强制 save+write），任务终态必为 done 或 blocked | P0 |

### FR-F 管理与可用性
| 编号 | 需求 | 优先级 |
|------|------|--------|
| F1 | blocked 任务人工重试（回到 parsing/reviewing） | P0 |
| F2 | 规则启停/编辑 API（Admin Token 保护） | P1 |
| F3 | 运行日志查询 API（按 task） | P1 |
| F4 | mock 数据重置端点（演示复现） | P1 |
| F5 | 简易 Web 工作台：待审列表/详情/命中/回写状态/运行轨迹 | P1 |

### FR-G 生产化运行时（v1.2 新增，简历核心差异化）
| 编号 | 需求 | 优先级 |
|------|------|--------|
| G1 | 断点恢复：每步持久化消息快照至 agent_runs；进程崩溃后 POST /agent/runs/{id}/resume 从断点继续 | P0 |
| G2 | dry-run 模式：全链路真实执行但跳过评论外呼，用于安全演练 | P0 |
| G3 | 熔断器：LLM 连续失败 ≥3 次开路 60s，开路期直接走确定性通道并记录降级事件 | P0 |
| G4 | 指标面：GET /metrics 以 Prometheus 文本格式暴露 runs/llm_calls/tool_calls/fallback/blocked/latency 计数 | P1 |
| G5 | 提示词版本注册表：prompts.yaml 版本化，prompt_version/model/channel 写入运行记录，结果可复现 | P1 |
| G6 | 轨迹录制回放：GPU 会话录制为 fixtures，测试以 FakeTransport 回放——CI 无 GPU | P1 |
| G7 | 输出护栏与幂等守卫：回写文本长度上限/格式校验/控制符净化；write_status=success 拒绝重复外呼 | P0 |

## 4. 非功能需求（NFR）

| 编号 | 类别 | 要求 |
|------|------|------|
| N01 | 性能 | 单合同全闭环 ≤60s（GPU 在线，OCR 页除外） |
| N02 | 可靠性 | LLM 不可用时降级为纯规则引擎模板意见，闭环不中断 |
| N03 | 安全 | 管理面 Admin Token；附件路径穿越防护；mock 与工具面端口隔离 |
| N04 | 可观测 | 结构化 JSON 日志（run_id/task_id 全链关联）+ task_logs 六类分级 + GET /metrics(Prometheus) + 组件级 /health(mysql/mock/llm) |
| N05 | 可部署 | 双容器 compose 一键起；MySQL 数据卷持久化；**云端部署**：独立新购云服务器，prod override（restart 策略/收敛端口/持久卷），部署手册 docs/部署手册.md |
| N06 | 测试 | pytest 覆盖去重/规则矩阵/状态机/mock 全链路（LLM mock） |
| N07 | 环境隔离 | 与项目 A 完全独立：独立仓库/独立 compose 项目名/独立网络与数据卷；GPU 推理复用 AutoDL 但仅按需开机 |

## 5. 验收标准（规范 §2.4.12 七条逐条映射）

| AC | 规范原文 | 验证方式 |
|----|---------|---------|
| AC-1 | 工具服务拉取待办并去重 | 探针：两次拉取任务数不变 + pytest |
| AC-2 | 下载附件并保存记录 | 探针：attachment 表 download_status=done 且文件存在 |
| AC-3 | 解析文档和图片扫描件提取字段 | docx 结构化断言 + png OCR 冒烟 |
| AC-4 | 规则审查返回命中证据和风险等级 | 高风险合同 → 预付款/违约缺失等命中断言 |
| AC-5 | 保存结果并回写评论区 | review_results 落库 + mock comments 收到 POST |
| AC-6 | 异常进入阻塞并支持重试 | 缺附件单 → blocked → retry → done |
| AC-7 | 完整闭环演示 | demo 脚本五阶段彩色输出全程取证 |
