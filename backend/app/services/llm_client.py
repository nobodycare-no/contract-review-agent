"""LLM 传输层：真实 HTTP / 轨迹回放 双实现 + 能力探测（ADR-B7/B9）。

- RealHTTPTransport: httpx 直连 vLLM /v1/chat/completions（OpenAI 兼容）
- ReplayTransport:   按 fixtures jsonl 顺序吐响应——CI 无 GPU 回放录制轨迹
- probe_native_tools(): 最小 tools 请求探测服务端 tool-call-parser 能力，进程内缓存
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.obs import LLM_CALLS, get_logger

logger = get_logger("llm_client")


class LLMUnavailable(Exception):
    pass


class RealHTTPTransport:
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             *, channel: str = "native") -> dict:
        settings = get_settings()
        # 裸 httpx 直连：vLLM 扩展参数必须位于 JSON 顶层
        # （OpenAI SDK 的 extra_body 包装对线上协议无效——此前思考因此未被关闭）
        payload: dict[str, Any] = {
            "model": settings.llm_model, "messages": messages,
            "temperature": 0.2, "max_tokens": 1500,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools and channel == "native":
            payload["tools"] = tools
        try:
            resp = httpx.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=payload, timeout=settings.mock_timeout_s * 8)
            resp.raise_for_status()
            body = resp.json()
            message = body["choices"][0]["message"]
            message["_usage"] = body.get("usage", {})
            return message
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(str(exc)[:300]) from exc


class ReplayTransport:
    """按行读取 trajectories jsonl 的 resp 字段顺序回放。"""

    def __init__(self, case: str) -> None:
        path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / \
            "trajectories" / f"{case}.jsonl"
        self._items = [json.loads(line)["resp"]
                       for line in path.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
        # 防御：剔除历史污染的探测行(echo)
        self._items = [i for i in self._items
                       if not any(c.get("function", {}).get("name") == "echo"
                                  for c in (i.get("tool_calls") or []))]
        self._cursor = 0

    def chat(self, messages, tools=None, *, channel="native") -> dict:
        if self._cursor >= len(self._items):
            raise LLMUnavailable("轨迹已耗尽")
        item = self._items[self._cursor]
        self._cursor += 1
        return item


class RecordingTransport:
    """包裹真实传输，把逐轮响应落盘为可回放轨迹。inner 暴露底层裸传输供探测旁路。"""

    def __init__(self, inner, case: str) -> None:
        self._inner = inner
        self.inner = inner
        self._path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / \
            "trajectories" / f"{case}.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

    def chat(self, messages, tools=None, *, channel="native") -> dict:
        message = self._inner.chat(messages, tools, channel=channel)
        slim = {k: message.get(k) for k in ("role", "content", "tool_calls", "reasoning_content")}
        self._fh.write(json.dumps({"ts": time.time(), "channel": channel,
                                   "resp": slim}, ensure_ascii=False) + "\n")
        self._fh.flush()
        return message


_probe_cache: dict[str, bool | None] = {"supported": None}


def probe_native_tools(transport=None) -> bool:
    """返回服务端是否支持原生 tools；异常一律判不支持并缓存。"""
    if _probe_cache["supported"] is not None:
        return _probe_cache["supported"]
    settings = get_settings()
    if not settings.llm_base_url:
        _probe_cache["supported"] = False
        return False
    transport = transport or RealHTTPTransport()
    tiny_tool = [{"type": "function", "function": {
        "name": "echo", "description": "probe",
        "parameters": {"type": "object", "properties": {}}}}]
    try:
        message = transport.chat(
            [{"role": "user", "content": "调用 echo 工具确认链路"}],
            tiny_tool, channel="native")
        supported = bool(message.get("tool_calls"))
    except Exception:  # noqa: BLE001 —— 探测失败≠服务不可用，仅决定通道
        supported = False
    _probe_cache["supported"] = supported
    logger.info("llm channel probed", extra={"kind": f"native={supported}"})
    return supported


def reset_probe_cache() -> None:
    _probe_cache["supported"] = None


def counted_chat(transport, messages, tools, channel) -> dict:
    try:
        message = transport.chat(messages, tools, channel=channel)
        LLM_CALLS.labels(channel=channel, outcome="ok").inc()
        return message
    except Exception:
        LLM_CALLS.labels(channel=channel, outcome="fail").inc()
        raise
