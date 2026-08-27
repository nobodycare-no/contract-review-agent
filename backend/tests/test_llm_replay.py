"""ADR-B9 真机轨迹回放回归：GPU 录制的实时响应序列在 CI 中离线重放必须同收敛。

fixtures 不在仓库时自动跳过（本地开发未联调 GPU 的场景）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from tests.factory import make_form, post_spy

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "trajectories"
CASES = sorted(p.stem for p in FIXTURE_DIR.glob("*.jsonl")) if FIXTURE_DIR.exists() else []


@pytest.fixture()
def fake_mock(monkeypatch):
    """真实本地审批域 + 回写捕获（外部模拟系统已物理移除）。"""
    from tests.factory import make_form, post_spy

    return {"posted": post_spy(monkeypatch)}


@pytest.mark.skipif(not CASES, reason="尚无录制轨迹——GPU 联调后生成")
class TestReplayRealTrajectories:
    def test_recorded_native_trajectory_replays_green(
            self, client, db_session: Session, fake_mock, monkeypatch) -> None:
        """取任一已录轨迹：LLM 全部替换为 ReplayTransport，RunController 必须同样 succeeded。"""
        from app.services.agent_loop import RunController
        from app.services.llm_client import ReplayTransport
        from app.tools.bootstrap import seed_rules

        seed_rules(db_session)
        case = CASES[0]
        transport = ReplayTransport(case)

        task = make_form(db_session, code=f"RP-{case[:6]}", title="回放回归",
                         applicant="CI", files=1)
        db_session.add(task)
        db_session.commit()

        controller = RunController(db_session, task, transport=transport)
        # 回放通道直接锁定 native：录制时即为原生 tool_calls 序列
        monkeypatch.setattr(
            __import__("app.services.llm_client", fromlist=["probe_native_tools"]),
            "probe_native_tools", lambda t=None: True)
        run = controller.start()

        assert run.status == "succeeded"
        assert run.channel == "native"
        assert any(t["tool"] == "write_approval_comment"
                   for t in controller.ctx.trace), "回放轨迹必须走完回写步"
