# GPU 实例操作卡：vLLM 工具调用模式启动 · 验收 · 观测

> 分支 feat/langchain-react-gpu-only 的真机前提。
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
