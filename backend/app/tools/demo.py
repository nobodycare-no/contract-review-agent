"""闭环演示 CLI：彩色五阶段输出。用法（容器内）：python -m app.tools.demo [--dry-run]"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("APP_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("ADMIN_TOKEN", "change-me-admin")
C = {"g": "\033[92m", "y": "\033[93m", "b": "\033[94m", "r": "\033[91m", "0": "\033[0m"}


def _post(path: str, payload: dict, token: str | None = None) -> dict:
    headers = {"X-Admin-Token": TOKEN} if token else {}
    r = httpx.post(f"{BASE}{path}", json=payload, headers=headers, timeout=300)
    r.raise_for_status()
    return r.json()


def stage(no: int, title: str) -> None:
    print(f"\n{C['b']}═══ 阶段 {no} · {title} ═══{C['0']}")


def main() -> int:
    dry = "--dry-run" in sys.argv
    print(f"{C['y']}合同审批审查 Agent · 闭环演示{'（dry-run 演练模式）' if dry else ''}{C['0']}")

    stage(1, "重置 mock 审批系统")
    print(_post("/admin/reset-demo", {}, token=TOKEN))

    stage(2, "拉取待办并去重")
    pulled = _post("/tools/list_pending", {"limit": 10})["data"]
    print(f"待办 {pulled['sync']['total']} 条：新建 {pulled['sync']['created']}，"
          f"更新 {pulled['sync']['updated']}")

    stage(3, "Agent 闭环执行 inst-001（高风险采购合同）")
    run = _post("/agent/run", {"instance_id": "inst-001", "dry_run": dry})
    print(f"run #{run['run_id']} 通道={run['channel']} 状态={run['status']} "
          f"步数={run['steps_used']} tokens≈{run['tokens']} "
          f"降级={run['fallback_kind']} 耗时={run['wall_ms']}ms")
    for i, t in enumerate(run.get("trace", []), 1):
        color = C["g"] if t["outcome"] == "ok" else C["r"]
        print(f"  {i:>2}. {t['tool']:<28} {color}{t['outcome']}{C['0']}")

    stage(4, "任务详情与审查结论")
    detail = httpx.get(f"{BASE}/agent/tasks/{run['task_id']}").json()
    hits = [h for h in detail["hits"] if h["hit_status"] == "hit"]
    print(f"命中 {len(hits)} 条规则：")
    for h in hits:
        print(f"  [{h['risk_level']}] {h['rule_name']}")
    review = detail["review"] or {}
    print(f"\n总风险等级：{review.get('overall_risk_level', '—').upper()}")
    comment = review.get("comment_text", "")
    print(comment.split("\n")[0] if comment else "（无评论）")

    stage(5, "回写状态")
    print(f"task_status={detail['task']['task_status']} "
          f"write_status={detail['task']['write_status']}")
    ok = run["status"] == "succeeded" and (
        dry or detail["task"]["write_status"] == "success")
    print(f"\n{C['g'] if ok else C['r']}演示{'通过 ✓' if ok else '失败 ✗'}{C['0']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
