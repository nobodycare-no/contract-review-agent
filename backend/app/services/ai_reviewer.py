"""LLM 自由裁量审查层（ADR-B10）：规则库之外的语义级增量风险。

设计契约：
- 输入=解析全文+已命中规则码；输出=points[{severity,point,evidence_quote,reason}]，强制引用原文；
- 与规则命中合并时来源标记为 AI_DISCRETIONARY，总评等级重算取最高；
- **任何失败静默降级**——纯规则结果原样返回，绝不影响闭环（ADR-B5 哲学一致）。
"""
from __future__ import annotations

import json
import re

from app.core.config import get_settings
from app.core.obs import get_logger, log_event
from app.services.llm_client import LLMUnavailable, RealHTTPTransport

logger = get_logger("ai_reviewer")

AI_CODE = "AI_DISCRETIONARY"
_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}
_LABEL = {"high": "高", "medium": "中", "low": "低"}
_MAX_POINTS = 5


def _transport() -> RealHTTPTransport | None:
    s = get_settings()
    if not getattr(s, "ai_review_enabled", True) or not s.llm_base_url:
        return None
    return RealHTTPTransport()


def chat_points(text: str, existing_codes: set[str], transport=None) -> list[dict]:
    from app.core.prompts import render

    target = transport or _transport()
    if target is None:
        raise LLMUnavailable("ai_review disabled or llm not configured")
    prompt = render("ai_review",
                    existing="、".join(sorted(existing_codes)) or "无",
                    text=text[:6000])   # 防御服务端 max-model-len 上限（中文 token 密度高）
    message = target.chat(
        [{"role": "system", "content": "你是企业资深法务审查专家，输出严格遵循约定 JSON 格式。"},
         {"role": "user", "content": prompt}], None, channel="json")
    return parse_points(message.get("content") or "")


def parse_points(content: str) -> list[dict]:
    """宽松提取首个平衡 JSON 对象并校验点结构；无结构/坏字段一律抛错由上层降级。"""
    match = None
    depth = 0
    start = content.find("{")
    if start < 0:
        raise ValueError("model output has no JSON object")
    for idx in range(start, len(content)):
        if content[idx] == "{":
            depth += 1
        elif content[idx] == "}":
            depth -= 1
            if depth == 0:
                match = content[start:idx + 1]
                break
    data = json.loads(match)
    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, list):
        raise ValueError("missing points array")
    cleaned: list[dict] = []
    for item in points[:_MAX_POINTS]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "low")).lower()
        if severity not in _LEVEL_ORDER:
            severity = "low"
        quote = str(item.get("evidence_quote") or "").strip()
        point = str(item.get("point") or "").strip()
        if not point or not quote:
            continue
        cleaned.append({"severity": severity,
                        "point": point[:200],
                        "evidence_quote": quote[:300],
                        "reason": str(item.get("reason") or "").strip()[:400]})
    return cleaned


def augment(summary: dict, text: str, *, task=None, db=None,
            transport=None) -> dict:
    """把 AI 增量风险并入规则汇总；可选持久化到 rule_hits；失败静默降级返回原 summary。"""
    if not text or len(text.strip()) < 50:
        return summary
    try:
        codes = {h["rule_code"] for h in summary.get("hits", [])}
        points = chat_points(text, codes, transport)
    except Exception as exc:  # noqa: BLE001 —— 降级即约定行为
        log_event(logger, 30, "ai_review skipped", err=str(exc)[:160])
        return summary
    if not points:
        # 空结果同样要清理旧 AI 行——重跑必须反映最新状态
        if task is not None and db is not None:
            try:
                from app.models import ReviewRule, RuleHit

                anchor = db.query(ReviewRule).filter_by(rule_code=AI_CODE).one_or_none()
                if anchor is not None:
                    db.query(RuleHit).filter_by(task_id=task.id,
                                                rule_id=anchor.id).delete()
                db.commit()
            except Exception as exc:  # noqa: BLE001
                log_event(logger, 30, "ai_review clear failed", err=str(exc)[:160])
        log_event(logger, 20, "ai_review empty")
        return summary

    merged = list(summary["hits"])
    overall = summary["overall_risk_level"]
    for p in points:
        merged.append({"rule_code": AI_CODE,
                       "rule_name": f"[AI] {p['point']}",
                       "risk_level": p["severity"],
                       "evidence_text": f"『{p['evidence_quote']}』——{p['reason']}"[:1000],
                       "evidence_position": "",
                       "suggestion_text": p["reason"]})
        if _LEVEL_ORDER[p["severity"]] > _LEVEL_ORDER[overall]:
            overall = p["severity"]

    augmented = dict(summary)
    augmented.update(hits=merged,
                     overall_risk_level=overall,
                     overall_risk_label=_LABEL[overall],
                     focus_points=[h["suggestion_text"] for h in merged])

    if task is not None and db is not None:
        try:
            _persist_points(db, task.id, merged)
        except Exception as exc:  # noqa: BLE001 —— 持久化失败不影响闭环
            log_event(logger, 30, "ai_review persist failed", err=str(exc)[:160])

    log_event(logger, 20, "ai_review merged",
              kind=f"points={len(points)},overall={overall}")
    return augmented


def _persist_points(db, task_id: int, hits: list) -> None:
    """把 AI 点写为 rule_hits（挂 AI_DISCRETIONARY 合成规则 id）；重跑先清旧行。"""
    from app.models import ReviewRule, RuleHit

    anchor = db.query(ReviewRule).filter_by(rule_code=AI_CODE).one_or_none()
    if anchor is None:
        from app.services.rule_seed import SEED_RULES

        spec = next(r for r in SEED_RULES if r["rule_code"] == AI_CODE)
        anchor = ReviewRule(**spec)
        db.add(anchor)
        db.flush()
    db.query(RuleHit).filter_by(task_id=task_id, rule_id=anchor.id).delete()
    for h in hits:
        if h.get("rule_code") != AI_CODE:
            continue
        db.add(RuleHit(task_id=task_id, rule_id=anchor.id,
                       evidence_text=(f"{h['rule_name']}｜{h['evidence_text']}")[:1000],
                       evidence_position="ai",
                       hit_status="hit"))
    db.commit()
