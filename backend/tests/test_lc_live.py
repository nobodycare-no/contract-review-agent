"""真机 GPU 用例（默认跳过）：LC_LIVE=1 时走 LangGraph ReAct 真·大模型闭环。

断言的是「耗时与轮次数」这类**不可伪造**的证据：
任何确定性降级路径都不可能产生分钟级的真实推理耗时与多轮工具调用。
"""
from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LC_LIVE"),
    reason="真机验证专用：设 LC_LIVE=1 且 GPU/vLLM 可达才运行")


def test_react_full_loop_on_real_llm(db_session, monkeypatch):
    from tests.factory import make_form

    from app.services import engine

    # conftest 为单测隔离默认 legacy；真机用例显式上 LangChain 主线
    monkeypatch.setenv("AGENT_ENGINE", "langchain")

    task = make_form(db_session)
    started = time.monotonic()
    result = engine.run_full_cycle(db_session, task, dry_run=True)
    elapsed = time.monotonic() - started

    assert result["status"] == "succeeded", result
    assert result["steps"] >= 6, f"ReAct 消息轮数过少，疑似未跑图：{result}"
    assert elapsed > 10.0, f"仅 {elapsed:.1f}s——不像真实推理，疑似降级！"

    print(f"\n[REAL-GPU] elapsed={elapsed:.1f}s "
          f"steps={result['steps']} tail={result['raw_output'][:200]!r}")
