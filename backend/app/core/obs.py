"""结构化可观测：JSON 日志 + Prometheus 指标 + LLM 熔断器（N04/G3/G4）。"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time

from prometheus_client import Counter, Gauge, Histogram

# ---------- Prometheus 指标（模块级单例，进程内复用） ----------
RUNS_TOTAL = Counter("cra_runs_total", "Agent runs by channel and final status", ["channel", "status"])
LLM_CALLS = Counter("cra_llm_calls_total", "LLM calls by channel and outcome", ["channel", "outcome"])
TOOL_CALLS = Counter("cra_tool_calls_total", "Tool executions", ["tool", "outcome"])
FALLBACKS = Counter("cra_fallback_total", "Fallback activations", ["kind"])
BLOCKED = Counter("cra_blocked_total", "Tasks entered blocked state", ["reason"])
RUN_LATENCY = Histogram(
    "cra_run_latency_seconds", "Run wall latency",
    buckets=(1, 5, 10, 30, 60, 120, 180, 300))
CIRCUIT_STATE = Gauge("cra_circuit_state", "LLM circuit breaker: 0 closed / 1 half_open / 2 open")

# ---------- JSON 日志 ----------
_EXTRA_FIELDS = ("run_id", "task_id", "tool", "ms", "err", "channel", "kind")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    logger.log(level, event, extra={k: v for k, v in fields.items() if v is not None})


# ---------- LLM 熔断器（ADR-B8） ----------
class CircuitBreaker:
    """连续失败开路 → 半开放行一次探测 → 成功闭合。线程安全。"""

    def __init__(self, fail_threshold: int, open_seconds: int) -> None:
        self._lock = threading.Lock()
        self._fails = 0
        self._opened_at = 0.0
        self.fail_threshold = fail_threshold
        self.open_seconds = open_seconds
        CIRCUIT_STATE.set(0)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at == 0.0:
            return "closed"
        if time.monotonic() - self._opened_at >= self.open_seconds:
            return "half_open"
        return "open"

    def allow(self) -> tuple[bool, str]:
        """返回 (是否放行真实调用, 当前状态)。open 期返回 False。"""
        with self._lock:
            state = self._state_locked()
            CIRCUIT_STATE.set({"closed": 0, "half_open": 1, "open": 2}[state])
            if state == "open":
                return False, state
            return True, state  # closed 常规放行；half_open 放行探测

    def record_success(self) -> None:
        with self._lock:
            self._fails = 0
            self._opened_at = 0.0
            CIRCUIT_STATE.set(0)

    def record_failure(self) -> None:
        with self._lock:
            self._fails += 1
            if self._state_locked() == "half_open" or self._fails >= self.fail_threshold:
                self._opened_at = time.monotonic()
                self._fails = 0
                CIRCUIT_STATE.set(2)
