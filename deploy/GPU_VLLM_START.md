# GPU 实例操作卡：vLLM 工具调用模式启动 · 验收 · 观测

> **本分支（feat/cloud-glm53flash）默认走智谱 BigModel 云端 GLM，此卡为本地 GPU 备选路径**——
> 想切回本地推理时：按本卡启动 vLLM，再把 `.env` 的 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`
> 三项改回 vLLM 端点即可，代码零改动。
> 系统侧对 LLM **零降级零静默**：环境不对就显式报错，不存在「看似成功」的假象。

## 一、为什么必须带这两个启动参数

LangChain Agent 走 OpenAI 原生 function calling，请求携带 `tool_choice:"auto"`。
vLLM 缺省不开这条通路，报错原文：

```
400 - 'auto' tool choice requires --enable-auto-tool-choice
      and --tool-call-parser to be set
```

事故记录：2026-08-27 17:20 首次真机闭环成功（7 次 200 / 28.1s / 12 条消息）；
随后实例重启丢失参数，同一套代码立即 400 掀桌——反证系统从未做假。

## 二、启动命令（AutoDL 终端，按实际模型路径调整）

```bash
vllm serve /root/autodl-tmp/Qwen3-8B \
  --served-model-name qwen3-8b \
  --host 0.0.0.0 --port 6006 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.90
```

Qwen3 系列用 `hermes` 解析器；`--served-model-name` 必须与 `.env` 的
`LLM_MODEL=qwen3-8b` 一致。

## 三、30 秒自检（服务起完后先跑这个再喊继续）

```bash
curl -s http://127.0.0.1:6006/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"用weather工具查北京天气"}],
       "tools":[{"type":"function","function":{"name":"weather",
                "parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]}' \
  | head -c 500
```

合格标志：返回 JSON 里出现 `"tool_calls"` 且 `"finish_reason":"tool_calls"`。
若仍是 400 提示缺参数，说明 flags 没带上，回到第二节检查命令行。

## 四、GPU 使用率双终端观测协议（防「曲线永远为 0」疑云）

- **终端 A**（压测源）：连发自检，持续制造真实推理负载：

  ```bash
  PAYLOAD='{"model":"qwen3-8b","messages":[{"role":"user","content":"用weather工具查上海天气"}],
            "tools":[{"type":"function","function":{"name":"weather",
                     "parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]}'
  for i in $(seq 1 300); do
    curl -s http://127.0.0.1:6006/v1/chat/completions -H "Content-Type: application/json" \
         -d "$PAYLOAD" -o /dev/null && echo "call $i ok"
  done
  ```

- **终端 B**（观测量表）：

  ```bash
  nvidia-smi dmon -s u -c 150
  ```

  `sm` 列连续高于 70%~90% 即为 GPU 实打实在推理；
  或者另一条路：AutoDL 控制台「算力监控」曲线看同窗口突起。

自检通过后对本机说一声「继续」，我立即重放容器端到端验收并把耗时证据贴给你。

## 五、模型局限与升级预案（2026-08-28 起生效）

当前 8B 本地模型（qwen3-8b）的现实约束与系统侧对策：

| 约束 | 对策 |
|------|------|
| 上下文 8k（建议 `--max-model-len 12288`） | 引擎提供 `search_contract_text` 检索工具——模型按关键词取条款原文，不通读全文 |
| 推理质量有限 | 系统提示词明确「不确定就如实说明，禁止编造条款」；规则库仅作参考线索，意见必须引用原文佐证 |
| 工具调用可靠性依赖 parser | Qwen3 必须配 `--tool-call-parser hermes`（第二节）；换模型时同步换 parser |

**升级预案**：换更大模型只需改 `.env` 的 `LLM_MODEL` + vLLM 启动参数（含对应
tool parser），代码零改动——引擎、九工具、提示词全部模型无关。
