"""云端 LLM 分支（feat/cloud-glm53flash）回归锁。

背景：本地 qwen3-8b 幻觉导致审查基本信息出错——本分支把推理端点切到
智谱 BigModel OpenAI 兼容端点（zai-custom · glm-5.3-flash）。
这里钉死四件事，防止切端点时闭环铁律被顺手丢掉：

1. 配置缺省=云端 GLM（不再默认 qwen3-8b / sk-atguigu；密钥不进代码）；
2. AI 审查层 httpx 载荷不再注入 Qwen 专属 /no_think 软开关；
3. 思考开关走可配置的 thinking 参数（默认不干预，禁用/启用显式传参）；
4. Agent 系统提示词不再宣称「8B 级本地模型」，
   但闭环铁律 / 意见格式 / 禁止编造条款三条诚实约束必须原样保留。
"""
from __future__ import annotations


# ---------- 1. 配置缺省 ----------

def test_default_config_points_to_cloud_glm(monkeypatch):
    from app.core.config import Settings

    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.llm_model == "glm-5.3-flash"
    assert s.llm_api_key == ""      # 密钥不写进代码：缺失时 API 401 显式失败
    assert s.llm_thinking == ""     # 默认不干预模型思考行为


# ---------- 2+3. AI 审查层传输载荷 ----------

class _Resp:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": "{}"}}],
                "usage": {}}


class _StubSettings:
    llm_model = "glm-5.3-flash"
    llm_api_key = "test-key"
    llm_base_url = "https://open.bigmodel.cn/api/paas/v4"
    mock_timeout_s = 15
    llm_thinking = ""


def _capture_post(monkeypatch, settings_cls=_StubSettings):
    import app.services.llm_client as llm_client

    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, payload=json, timeout=timeout)
        return _Resp()

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    monkeypatch.setattr(llm_client, "get_settings", lambda: settings_cls())
    return captured


def test_review_payload_no_qwen_soft_switch(monkeypatch):
    from app.services.llm_client import RealHTTPTransport

    captured = _capture_post(monkeypatch)
    RealHTTPTransport().chat(
        [{"role": "user", "content": "审查这份合同"}], None, channel="json")
    payload = captured["payload"]
    assert payload["model"] == "glm-5.3-flash"
    # /no_think 是 Qwen 专属软开关——对 GLM 是提示词污染，必须消失
    assert "/no_think" not in (payload["messages"][-1]["content"] or "")
    # 完成预算必须给思考型云端模型留足余量，JSON 不许被推理 token 截断
    assert payload["max_tokens"] >= 3000


def test_review_payload_thinking_passthrough(monkeypatch):
    """glm-5.3-flash 不支持关闭思考（API 400），档位=low/high/max——透传必须保形。"""
    from app.services.llm_client import RealHTTPTransport

    for level in ("low", "high", "max"):
        class _Level(_StubSettings):
            llm_thinking = level

        captured = _capture_post(monkeypatch, _Level)
        RealHTTPTransport().chat([{"role": "user", "content": "x"}], None)
        assert captured["payload"]["thinking"] == {"type": level}


def test_diag_llm_budget_survives_thinking_model(client, monkeypatch):
    """真机证据：思考型模型 reasoning token 计入 max_tokens——diag 上限 8 只会回出空串。"""
    import app.core.config as config_mod

    class _Live:
        llm_model = "glm-5.3-flash"
        llm_api_key = "test-key"
        llm_base_url = "https://open.bigmodel.cn/api/paas/v4"

    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "在线"}}], "usage": {}}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, payload=json)
        return _Resp()

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Live())
    import httpx as httpx_mod

    monkeypatch.setattr(httpx_mod, "post", fake_post)
    body = client.get("/app/diag_llm").json()
    assert captured["payload"]["max_tokens"] >= 512
    assert captured["payload"]["model"] == "glm-5.3-flash"
    assert body["reply"] == "在线"


# ---------- 4. Agent 系统提示词与模型缺省 ----------

def test_system_prompt_no_local_8b_claim_rules_intact():
    from app.services.lc_agent import SYSTEM_PROMPT

    assert "8B" not in SYSTEM_PROMPT            # 不再宣称本地小模型
    assert "write_approval_comment" in SYSTEM_PROMPT   # 闭环铁律
    assert "总风险等级" in SYSTEM_PROMPT                # 意见格式铁律
    assert "禁止编造" in SYSTEM_PROMPT                  # 诚实铁律
    assert "search_contract_text" in SYSTEM_PROMPT     # 检索定位能力仍在


def test_chat_model_default_model_is_glm(monkeypatch):
    for var in ("LLM_MODEL", "LLM_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    from app.services.lc_agent import _chat_model

    assert _chat_model().model_name == "glm-5.3-flash"
