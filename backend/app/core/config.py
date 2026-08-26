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


@lru_cache
def get_settings() -> Settings:
    return Settings()
