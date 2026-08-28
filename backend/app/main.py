"""FastAPI 应用装配——T2 骨架：健康(组件级) + 指标 + 静态挂载；业务路由由后续切片注册。"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.obs import setup_logging
from app.db import SessionLocal

_LLM_PROBE_TTL_S = 30.0


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()
    app = FastAPI(title="contract-review-agent", version="1.3.0")
    app.state.llm_probe_cache: dict = {"ts": 0.0, "ok": None}  # type: ignore[attr-defined]

    # 启动自愈：清理曾因维护/重启而卡死在中间态的任务（用户可见为"需人工处理"）；
    # 从未开跑的排队单诚实回待处理（heal_queued——它们没被工人碰过）
    try:
        from app.services.state_machine import recover_interrupted

        with SessionLocal() as _db0:
            healed = recover_interrupted(_db0, heal_queued=True)
    except Exception:  # noqa: BLE001 —— 表未建好等测试场景静默
        healed = 0

    from app.api.agent import router as agent_router
    from app.api.admin import router as admin_router
    from app.api.portal import router as portal_router
    from app.api.tools import router as tools_router

    app.include_router(tools_router)
    app.include_router(agent_router)
    app.include_router(portal_router)
    app.include_router(admin_router)

    @app.get("/health")
    def health() -> dict:
        """组件级健康探测（N04）：mysql/审批域/llm；任一失败 status=degraded 但仍 200。"""
        components: dict[str, dict] = {}

        t0 = time.perf_counter()
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
                pending = db.execute(
                    text("SELECT COUNT(*) FROM approval_tasks WHERE task_status='pending'")
                ).scalar()
            components["mysql"] = {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
            components["forms"] = {"ok": True, "pending": int(pending or 0)}
        except Exception as exc:  # noqa: BLE001 —— 健康面必须吞错降级
            components["mysql"] = {"ok": False, "error": str(exc)[:200]}
            components.setdefault("forms", {"ok": False})

        if not settings.llm_base_url:
            components["llm"] = {"ok": None, "note": "not_configured"}
        else:
            cache = app.state.llm_probe_cache
            now = time.monotonic()
            if now - cache["ts"] > _LLM_PROBE_TTL_S:   # 到期即重探，不做负缓存永锁
                try:
                    resp = httpx.get(f"{settings.llm_base_url.rstrip('/')}/models",
                                     headers={"Authorization":
                                              f"Bearer {settings.llm_api_key}"},
                                     timeout=8.0)
                    ok = resp.status_code == 200
                except Exception:  # noqa: BLE001
                    ok = False
                cache.update(ts=now, ok=ok)
            components["llm"] = {"ok": cache["ok"], "cached": True}

        degraded = any(c.get("ok") is False for c in components.values())
        return {"status": "degraded" if degraded else "ok", "components": components}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # Web 静态托管：优先 Vue 构建产物(dist)，回退轻量占位页(static)。API 路由不受遮蔽。
    _candidates = [Path(__file__).resolve().parents[1] / "web" / "dist",   # 容器内 /srv/web/dist
                   Path(__file__).resolve().parents[2] / "web" / "dist",   # 本地仓库布局
                   Path(__file__).resolve().parent / "static"]             # 兜底
    web_root = next((c for c in _candidates if c.exists()), None)
    if web_root is not None:
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(web_root), html=True), name="web")

    return app


app = create_app()
