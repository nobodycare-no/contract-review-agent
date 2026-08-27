"""轨迹录制运行器：容器内进程级 RunController 跑指定审批单，真实 LLM 响应落 fixtures。

用法：RECORD_TRAJECTORY=<case名> python -m app.tools.record_run <instance_id>
产物：/srv/tests/fixtures/trajectories/<case名>.jsonl（随后 docker cp 回仓库）
"""
from __future__ import annotations

import sys

from app.core.config import get_settings
from app.core.obs import get_logger, setup_logging
from app.db import SessionLocal
from app.models import ApprovalTask
from app.services import fetcher
from app.services.agent_loop import RunController
from app.tools.bootstrap import seed_rules

logger = get_logger("record_run")


def main() -> int:
    setup_logging()
    instance_id = sys.argv[1] if len(sys.argv) > 1 else "inst-001"
    case = get_settings().record_trajectory or "unnamed_case"

    with SessionLocal() as db:
        fetcher.sync_pending_approvals(db)
        task = db.query(ApprovalTask).filter_by(instance_id=instance_id).one_or_none()
        if task is None:
            logger.error("task not found", extra={"err": instance_id})
            return 2
        controller = RunController(db, task)          # 默认走 RecordingTransport(因为 env 已设)
        run = controller.start()
        logger.info("recorded run finished",
                    extra={"kind": f"case={case} status={run.status} "
                                   f"channel={run.channel} steps={run.steps_used}"})
        print(f"[record_run] case={case} instance={instance_id} -> status={run.status} "
              f"channel={run.channel} steps={run.steps_used} trace={len(controller.ctx.trace)}")
    return 0 if run.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
