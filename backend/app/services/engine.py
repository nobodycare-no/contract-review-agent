"""运行引擎选择器（V2 分支）：默认 LangChain ReAct，无任何静默降级。"""
from __future__ import annotations

import os


def run_full_cycle(db, task, *, dry_run: bool = False) -> dict:
    engine = os.environ.get("AGENT_ENGINE", "langchain")
    if engine == "langchain":
        from app.services.lc_agent import run_lc

        return run_lc(db, task, dry_run=dry_run)
    # 兼容开关：legacy 仅存在于 main 分支的测试里，本分支仍可切换对比
    from app.services.agent_loop import RunController

    c = RunController(db, task, dry_run=dry_run)
    run = c.start()
    return {"status": run.status, "steps": run.steps_used,
            "channel": run.channel, "fallback_kind": run.fallback_kind}