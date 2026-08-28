"""AC 真实栈验收探针（V2 语义）：对运行中的 compose 栈逐条取证，任一失败 exit 1。

容器内运行：python -m app.acceptance.probe
V2 行为：LangChain ReAct 引擎、零降级（LLM 失败=502+blocked）、闭环闸门、诚实计时。
注意：本探针会 reset-demo 重种演示单（清空现有业务数据）。
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
    print(f"[{'PASS' if cond else 'FAIL'}] {ac:<10} {evidence}")


def post(path, payload=None, token=None):
    headers = {"X-Admin-Token": TOKEN} if token else {}
    r = httpx.post(f"{BASE}{path}", json=payload or {}, headers=headers, timeout=900)
    return r.json()


def get(path):
    return httpx.get(f"{BASE}{path}", timeout=60).json()


def task_by_code(code: str) -> dict:
    return next(t for t in get("/agent/tasks")["tasks"] if t["approval_code"] == code)


def main() -> int:
    # 健康面（mysql/forms/llm 三组件）
    health = get("/health")
    check("HEALTH", set(health["components"]) == {"mysql", "forms", "llm"},
          f"status={health['status']} mysql={health['components']['mysql']['ok']} "
          f"forms={health['components'].get('forms',{}).get('ok')} llm={health['components']['llm']['ok']}")

    # AC-1 队列幂等与唯一编号（统一系统语义：创建即落库，拉取为只读视图）
    reset = post("/admin/reset-demo", token=TOKEN)
    seeded = len(reset.get("seeded") or [])
    codes = [t["approval_code"] for t in get("/agent/tasks")["tasks"]]
    dup_free = len(codes) == len(set(codes))
    q1 = post("/tools/list_pending", {"limit": 10})["data"]["sync"]
    q2 = post("/tools/list_pending", {"limit": 10})["data"]["sync"]
    check("AC-1", seeded == 5 and dup_free and q1["total"] == q2["total"],
          f"重种={seeded} 单号无重复={dup_free} 队列两次视图均={q1['total']}")

    # AC-2 附件下载与记录（inst-001）
    buy = task_by_code("LOCAL-AP-2026-001")
    dl = post("/tools/download_attachment", {"instance_id": buy["instance_id"]})
    atts = dl["data"]["attachments"]
    check("AC-2", atts and all(a["download_status"] == "done" for a in atts),
          f"{atts}")

    # AC-3 解析（结构化字段：金额归一化数值元）
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

    # AC-4 规则审查（确定性初筛 + AI 裁量增量层）
    ruled = post("/tools/run_rules", {"case_id": buy["id"]})["data"]
    hit_codes = {h["rule_code"] for h in ruled["hits"]}
    expected = {"PAY_ADVANCE_HIGH", "NO_BREACH", "ACCEPTANCE_MISSING"}
    check("AC-4", expected <= hit_codes and ruled["overall_risk_level"] == "high",
          f"overall={ruled['overall_risk_level']} hits={sorted(hit_codes)}")

    # AC-5 保存+回写（中文枚举归一 + 幂等去重闭环状态）
    saved = post("/tools/save_result", {
        "case_id": buy["id"], "overall_risk_level": "high",
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

    # AC-6 创建闸门：无附件提交被拒（V1 起 mock 已物理删除，运行期缺失路径由 pytest 覆盖）
    empty_try = httpx.post(f"{BASE}/app/forms", data={"applicant": "探针", "bundle": "false"},
                           timeout=30).json()
    check("AC-6", empty_try["ok"] is False and len(empty_try["errors"]) == 1,
          f"空单拒绝={empty_try['ok'] is False}")

    # AGENT-RUN V2 真机闭环：LangChain ReAct 多轮推理，无 channel/fallback 概念
    lease = task_by_code("LOCAL-AP-2026-003")
    run = post("/agent/run", {"instance_id": lease["instance_id"]})
    lease_after = task_by_code("LOCAL-AP-2026-003")
    check("AGENT-RUN",
          run.get("status") == "succeeded"
          and lease_after["task_status"] == "done"
          and lease_after["write_status"] == "success"
          and run.get("elapsed_ms", 0) > 10000
          and len(run.get("trace") or []) >= 5,
          f"elapsed_ms={run.get('elapsed_ms')} steps={run.get('steps')} "
          f"trace={len(run.get('trace') or [])}轮工具 status={run.get('status')}")

    # CLOSED-LOOP 闭环闸门反证：trace 必含写回工具且 outcome=ok
    write_ok = any(t["tool"] == "write_approval_comment" and t["outcome"] == "ok"
                   for t in (run.get("trace") or []))
    check("CLOSED-LOOP", write_ok,
          f"write_approval_comment(ok) in trace={write_ok}")

    # METRICS 暴露（Prometheus 家族）
    text = httpx.get(f"{BASE}/metrics", timeout=15).text
    check("METRICS", "cra_runs_total" in text and "cra_tool_calls_total" in text,
          "Prometheus 家族已暴露")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n==== 探针汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS ====")
    for ac, _, ev in failed:
        print(f"FAILED {ac}: {ev}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
