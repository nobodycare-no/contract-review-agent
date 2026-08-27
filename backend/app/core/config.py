"""集中配置——全部来自环境变量。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    mysql_url: str = "mysql+pymysql://cra:cra123456@mysql:3306/contract_review?charset=utf8mb4"

    # LLM（Qwen3-8B @ vLLM，AutoDL 6006 端口映射）
    llm_base_url: str = ""
    llm_api_key: str = "sk-atguigu"
    llm_model: str = "qwen3-8b"
    llm_timeout_s: int = 120

    # mock 审批系统（服务间）
    mock_base_url: str = "http://mock-approval:8100"

    # OCR
    tesseract_cmd: str = ""          # 空则用系统 PATH 中的 tesseract
    ocr_lang: str = "chi_sim"

    # 存储
    upload_dir: str = "/srv/storage/attachments"

    # 内部管理令牌（管理员接口）
    admin_token: str = "change-me-admin"

    # ===== RunController 三维预算（ADR-B8）=====
    agent_max_steps: int = 12          # 规范字面上限
    agent_token_budget: int = 24000    # prompt+completion 累计
    agent_wall_budget_s: int = 180     # 运行墙钟时限

    # ===== 熔断器 =====
    circuit_fail_threshold: int = 3
    circuit_open_seconds: int = 60

    # ===== 工具超时（秒）=====
    mock_timeout_s: int = 15
    download_timeout_s: int = 30
    parse_timeout_s: int = 90
    rules_timeout_s: int = 10

    # ===== 轨迹录制（ADR-B9）：非空则录制到 tests/fixtures/trajectories/<名>.jsonl =====
    record_trajectory: str = ""

    # ===== LLM 自由裁量审查层（ADR-B10）=====
    ai_review_enabled: bool = True

    # 后台运行模式轮询间隔提示（供 Web/CLI 使用）
    run_poll_interval_ms: int = 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()
