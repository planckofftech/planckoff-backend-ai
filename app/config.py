from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str = "dev-key"
    max_upload_mb: float = 500.0
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # AI fallback (Phase 4). Absent key => the AI tier is skipped, not an error.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    ai_model: str = "google/gemini-2.5-flash"
    ai_render_dpi: int = 200

    # Page-finder thresholds. Fitted to one document -- see PLAN.md section 8.
    min_header_hits: int = 5
    min_tag_run: int = 8

    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openrouter_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

