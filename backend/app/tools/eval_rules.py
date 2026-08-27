"""规则审查评估集（T8）：四份画像 × 标注命中矩阵 → precision/recall/F1 + 总评准确率。

容器内运行：python -m app.tools.eval_rules   （对真实栈打 HTTP，不直连 DB）
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("APP_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("ADMIN_TOKEN", "change-me-admin")

ALL_RULES = {"PAY_ADVANCE_HIGH", "PAY_CYCLE_LONG", "AUTO_RENEW", "NO_BREACH",
             "JURISDICTION_RISK", "PARTY_MISSING", "AMOUNT_MISSING", "NDA_MISSING",
             "DATA_COMPLIANCE", "IP_MISSING", "ACCEPTANCE_MISSING"}

# 标注矩阵：人工判定每份合同的预期命中/禁止误报/总评
EXPECTED: dict[str, dict] = {
    "LOCAL-AP-2026-001": {  # 高风险采购：预付50%/无违约/管辖甲方/缺验收/缺保密/缺知产
        "expected": {"PAY_ADVANCE_HIGH", "PAY_CYCLE_LONG", "NO_BREACH",
                     "JURISDICTION_RISK", "NDA_MISSING", "IP_MISSING",
                     "ACCEPTANCE_MISSING"},
        "overall": "high"},
    "LOCAL-AP-2026-002": {  # 中风险外包：自动续约/缺知产；其余条款齐全
        "expected": {"AUTO_RENEW", "IP_MISSING"},
        "overall": "medium"},
    "LOCAL-AP-2026-003": {  # 低风险租赁：条款完备零命中
        "expected": set(),
        "overall": "low"},
    "LOCAL-AP-2026-004": {  # 中风险数据协议：数据处理提示/缺保密/缺知产
        "expected": {"DATA_COMPLIANCE", "NDA_MISSING", "IP_MISSING"},
        "overall": "medium"},
}


def post(path, payload=None, token=None):
    headers = {"X-Admin-Token": TOKEN} if token else {}
    return httpx.post(f"{BASE}{path}", json=payload or {}, headers=headers,
                      timeout=300).json()


def main() -> int:
    post("/admin/reset-demo", token=TOKEN)
    post("/tools/list_pending", {"limit": 10})
    tasks = {t["approval_code"]: t for t in
             httpx.get(f"{BASE}/agent/tasks", timeout=30).json()["tasks"]}

    tp = fp = fn = 0
    overall_ok = 0
    rows: list[str] = ["sample,rule_code,verdict"]
    overall_rows: list[str] = ["sample,predicted,expected,match"]

    for code, spec in EXPECTED.items():
        task = tasks[code]
        post("/tools/download_attachment", {"instance_id": task["instance_id"]})
        parsed = post("/tools/parse_document", {"document_id": task["id"]})
        assert parsed.get("ok"), f"{code} parse failed: {parsed}"
        ruled = post("/tools/run_rules", {"case_id": task["id"]})
        assert ruled.get("ok"), f"{code} rules failed: {ruled}"

        predicted = {h["rule_code"] for h in ruled["data"]["hits"]}
        expected = spec["expected"]

        for r in sorted(expected):
            verdict = "TP" if r in predicted else "FN"
            tp += verdict == "TP"
            fn += verdict == "FN"
            rows.append(f"{code},{r},{verdict}")
        for r in sorted(predicted - expected):
            fp += 1
            rows.append(f"{code},{r},FP")

        match = ruled["data"]["overall_risk_level"] == spec["overall"]
        overall_ok += match
        overall_rows.append(f"{code},{ruled['data']['overall_risk_level']},"
                            f"{spec['overall']},{'Y' if match else 'N'}")

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    print("\n".join(rows))
    print("\n".join(overall_rows))
    print("\n==== 规则评估汇总 ====")
    print(f"样本数: {len(EXPECTED)}")
    print(f"TP={tp} FP={fp} FN={fn}")
    print(f"micro_precision={precision:.4f}")
    print(f"micro_recall={recall:.4f}")
    print(f"micro_f1={f1:.4f}")
    print(f"总评等级准确率={overall_ok}/{len(EXPECTED)}")
    ok = fp == 0 and fn == 0 and overall_ok == len(EXPECTED)
    print("EVAL " + ("PASS ✓" if ok else "FAIL ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
