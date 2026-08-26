"""规则引擎：keyword / regex / absence 三模式匹配 + 阈值语义 + 命中持久化（FR-C，SDD §4）。

- keyword：任一关键词出现即命中；
- regex：正则命中即候选；若规则在 NUMERIC_THRESHOLDS 注册了阈值，捕获数字 ≥ 阈值才成立；
- absence：match_text 逗号分隔关键词组**全部**未出现即命中（缺失即风险）。
评估结果全量落 rule_hits（hit/miss/error），重跑先清除该任务旧行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.obs import TOOL_CALLS, get_logger, log_event

logger = get_logger("rule_engine")

# 数值型规则的阈值注册（捕获组数字 ≥ 阈值才算风险）
NUMERIC_THRESHOLDS: dict[str, int] = {
    "PAY_ADVANCE_HIGH": 30,   # 预付款比例 %
    "PAY_CYCLE_LONG": 60,     # 付款周期 工作日
}

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
_LEVEL_NAME = {"high": "高", "medium": "中", "low": "低"}

_COMPILE_CACHE: dict[str, re.Pattern | None] = {}


@dataclass
class RuleEvaluation:
    rule_id: int
    rule_code: str
    rule_name: str
    risk_level: str
    hit_status: str            # hit | miss | error
    evidence_text: str
    evidence_position: str
    suggestion_text: str


def _compile(pattern: str) -> re.Pattern | None:
    if pattern not in _COMPILE_CACHE:
        try:
            _COMPILE_CACHE[pattern] = re.compile(pattern)
        except re.error as exc:
            logger.warning("regex compile failed", extra={"err": str(exc)[:200]})
            _COMPILE_CACHE[pattern] = None
    return _COMPILE_CACHE[pattern]


def _snippet(text: str, start: int, width: int = 160) -> str:
    return text[start:start + width].replace("\n", " ").strip()


def evaluate_rule(rule, text: str) -> RuleEvaluation:
    """对单条规则求值；任何异常收敛为 error 行，绝不阻断整体。"""
    base = dict(rule_id=rule.id, rule_code=rule.rule_code, rule_name=rule.rule_name,
                risk_level=rule.risk_level, suggestion_text=rule.suggestion_text)
    try:
        if rule.match_mode == "keyword":
            for kw in rule.match_text.split(","):
                kw = kw.strip()
                idx = text.find(kw)
                if idx >= 0:
                    return RuleEvaluation(**base, hit_status="hit",
                                          evidence_text=_snippet(text, idx),
                                          evidence_position=str(idx))
            return RuleEvaluation(**base, hit_status="miss", evidence_text="", evidence_position="")

        if rule.match_mode == "absence":
            missing = [kw.strip() for kw in rule.match_text.split(",")
                       if kw.strip() and kw.strip() not in text]
            if len(missing) == len([k for k in rule.match_text.split(",") if k.strip()]):
                return RuleEvaluation(**base, hit_status="hit",
                                      evidence_text="全文未出现任一关键词：" + "/".join(missing),
                                      evidence_position="")
            return RuleEvaluation(**base, hit_status="miss", evidence_text="", evidence_position="")

        if rule.match_mode == "regex":
            compiled = _compile(rule.match_text)
            if compiled is None:
                return RuleEvaluation(**base, hit_status="error",
                                      evidence_text=f"正则编译失败: {rule.match_text[:120]}",
                                      evidence_position="")
            m = compiled.search(text)
            if not m:
                return RuleEvaluation(**base, hit_status="miss", evidence_text="", evidence_position="")
            threshold = NUMERIC_THRESHOLDS.get(rule.rule_code)
            if threshold is not None:
                groups = [g for g in m.groups() if g and g.strip().isdigit()]
                if not groups or int(groups[0]) < threshold:
                    return RuleEvaluation(**base, hit_status="miss",
                                          evidence_text="", evidence_position="")
            return RuleEvaluation(**base, hit_status="hit",
                                  evidence_text=_snippet(text, m.start()),
                                  evidence_position=str(m.start()))

        return RuleEvaluation(**base, hit_status="error",
                              evidence_text=f"未知 match_mode: {rule.match_mode}",
                              evidence_position="")
    except Exception as exc:  # noqa: BLE001 —— 单规则异常不阻断
        log_event(logger, logging_warning(), "rule error", err=str(exc)[:200], tool=rule.rule_code)
        return RuleEvaluation(**base, hit_status="error",
                              evidence_text=f"规则执行异常: {exc}"[:500], evidence_position="")


def logging_warning() -> int:
    import logging

    return logging.WARNING


def evaluate_rules(rules, text: str) -> list[RuleEvaluation]:
    return [evaluate_rule(r, text) for r in rules]


def summarize(evaluations: list[RuleEvaluation]) -> dict:
    """总风险等级 = 命中规则最高级；关注点 = 各命中建议。"""
    hits = [e for e in evaluations if e.hit_status == "hit"]
    overall = "low"
    for e in hits:
        if _RISK_ORDER[e.risk_level] > _RISK_ORDER[overall]:
            overall = e.risk_level
    hits.sort(key=lambda e: (-_RISK_ORDER[e.risk_level], e.rule_code))
    return {
        "overall_risk_level": overall,
        "overall_risk_label": _LEVEL_NAME[overall],
        "focus_points": [e.suggestion_text for e in hits],
        "hits": [{
            "rule_code": e.rule_code,
            "rule_name": getattr(e, "rule_name", e.rule_code),
            "risk_level": e.risk_level,
            "evidence_text": e.evidence_text[:1000],
            "evidence_position": e.evidence_position,
            "suggestion_text": e.suggestion_text,
        } for e in hits],
        "stats": {"evaluated": len(evaluations), "hit": len(hits),
                  "miss": sum(1 for e in evaluations if e.hit_status == "miss"),
                  "error": sum(1 for e in evaluations if e.hit_status == "error")},
    }


def run_task_rules(db, task_id: int, rules, text: str) -> dict:
    """评估 + 全量持久化（先清旧行），返回汇总。tool 指标计入 run_rules。"""
    from app.models import RuleHit

    evaluations = evaluate_rules(rules, text)
    summary = summarize(evaluations)

    db.query(RuleHit).filter_by(task_id=task_id).delete()
    for e in evaluations:
        db.add(RuleHit(
            task_id=task_id, rule_id=e.rule_id,
            evidence_text=(e.evidence_text or e.hit_status)[:1000],
            evidence_position=e.evidence_position[:60],
            hit_status=e.hit_status))
    db.commit()

    TOOL_CALLS.labels(tool="run_rules", outcome="done").inc()
    log_event(logger, 20, "rules evaluated", task_id=task_id,
              kind=f"hit={summary['stats']['hit']}/overall={summary['overall_risk_level']}")
    return summary
