"""AC-1~7 真实栈验收探针：对运行中的 compose 栈逐条取证，任一失败 exit 1。

容器内运行：python -m app.acceptance.probe
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("APP_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("ADMIN_TOKEN", "change-me-admin")
RESULTS: list[tuple[str, bool, str]] = []


def check(ac: str, cond: bool, evidence: str) -> None:
    RESULTS.append((ac, bool(cond), evidence))
    print(f"[{'PASS' if cond else 'FAIL'}] {ac:<8} {evidence}")


def post(path, payload=None, token=None):
    headers = {"X-Admin-Token": TOKEN} if token else {}
    r = httpx.post(f"{BASE}{path}", json=payload or {}, headers=headers, timeout=300)
    return r.json()


def get(path):
    return httpx.get(f"{BASE}{path}", timeout=60).json()


def task_by_code(code: str) -> dict:
    return next(t for t in get("/agent/tasks")["tasks"] if t["approval_code"] == code)


def main() -> int:
    # 健康面
    health = get("/health")
    check("HEALTH", set(health["components"]) == {"mysql", "forms", "llm"},
          f"status={health['status']} mysql={health['components']['mysql']['ok']} "
          f"forms={health['components'].get('forms',{}).get('ok')} llm={health['components']['llm'].get('ok')}")

    # AC-1 队列幂等与唯一编号（统一系统语义：任务创建即落库，拉取为只读视图）
    reset = post("/admin/reset-demo", token=TOKEN)
    seeded = len(reset.get("seeded") or [])
    codes = [t["approval_code"] for t in get("/agent/tasks")["tasks"]]
    dup_free = len(codes) == len(set(codes))
    q1 = post("/tools/list_pending", {"limit": 10})["data"]["sync"]
    q2 = post("/tools/list_pending", {"limit": 10})["data"]["sync"]
    check("AC-1", seeded == 6 and dup_free and q1["total"] == q2["total"],
          f"重种={seeded} 单号无重复={dup_free} 队列两次视图均={q1['total']}")

    # AC-2 下载与记录（inst-001）
    buy = task_by_code("LOCAL-AP-2026-001")
    dl = post("/tools/download_attachment", {"instance_id": buy["instance_id"]})
    atts = dl["data"]["attachments"]
    check("AC-2", atts and all(a["download_status"] == "done" for a in atts),
          f"{atts}")

    # AC-3 解析（结构化字段）
    parsed = post("/tools/parse_document", {"document_id": buy["id"]})
    amount = parsed["data"]["basic_info"]["amount"]["value"]
    check("AC-3a", parsed["data"]["task_status"] == "reviewing" and amount == 1860000.0,
          f"amount_value={amount}")

    # AC-3b OCR 扫描件（inst-005 PNG → tesseract chi_sim）
    scan = task_by_code("LOCAL-AP-2026-005")
    post("/tools/download_attachment", {"instance_id": scan["instance_id"]})
    ocr = post("/tools/parse_document", {"document_id": scan["id"]})
    no = ocr["data"]["basic_info"]["contract_no"]["value"]
    check("AC-3b", ocr["ok"] and no == "HT-2026-0305",
          f"OCR 提取 contract_no={no}")

    # AC-4 规则审查
    ruled = post("/tools/run_rules", {"case_id": buy["id"]})["data"]
    codes = {h["rule_code"] for h in ruled["hits"]}
    expected = {"PAY_ADVANCE_HIGH", "NO_BREACH", "ACCEPTANCE_MISSING"}
    check("AC-4", expected <= codes and ruled["overall_risk_level"] == "high",
          f"overall={ruled['overall_risk_level']} hits={sorted(codes)}")

    # AC-5 保存+回写
    saved = post("/tools/save_result", {
        "case_id": buy["id"], "overall_risk_level": ruled["overall_risk_level"],
        "summary_text": "probe", "focus_points_json": ruled["focus_points"],
        "comment_text": "【AI合同审查】总风险等级：高\n二、中文摘要\nprobe"})
    written = post("/tools/write_comment",
                   {"instance_id": buy["instance_id"],
                    "review_id": saved["data"]["review_id"]})
    dup = post("/tools/write_comment",
               {"instance_id": buy["instance_id"],
                "review_id": saved["data"]["review_id"]})
    done_task = task_by_code("LOCAL-AP-2026-001")
    check("AC-5", written["data"]["write_status"] == "success"
          and done_task["task_status"] == "done"
          and dup["data"].get("deduped") is True,
          f"write={written['data']['write_status']} 幂等重放 deduped={dup['data'].get('deduped')}")

    # AC-6 阻塞与重试（inst-006 缺附件）
    broken = task_by_code("LOCAL-AP-2026-006")
    blocked = post("/tools/parse_document", {"document_id": broken["id"]})
    after_block = task_by_code("LOCAL-AP-2026-006")
    retried = httpx.post(f"{BASE}/agent/tasks/{broken['id']}/retry", timeout=30)
    check("AC-6", blocked["error"]["code"] == "ATTACHMENT_MISSING"
          and after_block["task_status"] == "blocked"
          and retried.status_code == 200
          and retried.json()["resumed_stage"] == "parsing",
          f"reason={str(after_block['block_reason'])[:48]} → retry(parsing) OK")

    # Agent 真机闭环（LLM 在线→native/json；离线→deterministic；皆须收敛至 done）
    lease = task_by_code("LOCAL-AP-2026-003")
    run = post("/agent/run", {"instance_id": lease["instance_id"]})
    lease_after = task_by_code("LOCAL-AP-2026-003")
    check("AGENT-RUN", run["channel"] in ("native", "json", "deterministic")
          and run["status"] == "succeeded"
          and lease_after["task_status"] == "done",
          f"channel={run['channel']} fallback={run['fallback_kind']} "
          f"steps={run['steps_used']} trace={len(run['trace'])}步")

    # metrics 暴露
    text = httpx.get(f"{BASE}/metrics", timeout=15).text
    check("METRICS", "cra_runs_total" in text and "cra_circuit_state" in text,
          "Prometheus 家族已暴露")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n==== 探针汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS ====")
    for ac, _, ev in failed:
        print(f"FAILED {ac}: {ev}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
