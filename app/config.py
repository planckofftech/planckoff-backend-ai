from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Left as the obvious placeholder on purpose, and checked at startup rather
    # than trusted -- see `main`. A default that happens to work is how an API
    # ships open: nothing fails, nothing warns, and the key protecting it is in
    # a public repository.
    api_key: str = "dev-key"
    # Set this in any deployment. It turns the placeholder from a convenience
    # into a refusal to start.
    require_real_api_key: bool = False
    max_upload_mb: float = 500.0
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # AI fallback (Phase 4). Absent key => the AI tier is skipped, not an error.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    ai_model: str = "google/gemini-2.5-flash"
    # Detecting doors on a plan is a different job from transcribing a table,
    # and the best model for it is an open question -- kept separate so it can
    # be changed on the evidence of scripts/door_bakeoff.py without disturbing
    # the schedule tier. Empty means "use ai_model".
    ai_detect_model: str = "google/gemini-2.5-pro"
    ai_render_dpi: int = 200
    # How wide a door should be in the picture the detector sees. Measured:
    # at 80 px recall was 78%% and boxes landed in open rooms; at 160 px it
    # was 94%% and they landed on the doors. The cost is more tiles.
    ai_door_pixels: int = 160
    # Hard ceiling on one plan-audit request, in USD. Deliberately near the
    # cost of a single project rather than generous: a run that needs more
    # should say so and stop, not decide for itself. Two projects once cost $6
    # against a stated $0.70 because the only cap was per sheet.
    detect_budget_usd: float = 1.50
    # Most tiles one sheet may be cut into. A backstop against a loop bug, not
    # a cost control -- `detect_budget_usd` is the cost control. It matters on
    # a big building drawn small: a 1/16" warehouse plan needs about 90 tiles
    # to show every door at a readable size, so at 40 the sheet is read in
    # part and the rest is reported unread. Raise it and the budget together.
    max_tiles_per_sheet: int = 40

    # Supabase. Absent settings mean the service runs exactly as it did before
    # -- nothing is stored and nothing fails. A takeoff is still returned to the
    # caller; it just is not remembered.
    supabase_url: str = ""
    # The service key, not the anon key. This process is trusted and bypasses
    # row-level security; the anon key belongs in a browser, never here.
    supabase_service_key: str = ""
    # Stamped on every run_log row. Without it a comparison between two runs
    # says nothing, because you cannot tell what was different about them.
    app_version: str = "dev"

    # Where the drawing sets themselves live. Cloudflare R2 speaks the S3 API,
    # so this is boto3 pointed somewhere other than AWS.
    #
    # The files have to outlive the request that uploaded them: the plan viewer
    # re-renders sheets from the original PDF, so a temp file deleted when the
    # upload finishes leaves the viewer with nothing to draw.
    r2_endpoint: str = ""
    r2_bucket: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""
    # How long a browser has to use an upload link before it expires.
    upload_url_ttl: int = 900

    # How many drawing sets may be read at once. One peaked at 434 MB, so two
    # together is an out-of-memory kill on a 1 GB host -- and the kernel takes
    # the whole process, losing both jobs and every request in flight. Raise it
    # with the memory, not with the core count.
    max_concurrent_extractions: int = 1

    # Page-finder thresholds. Fitted to one document -- see PLAN.md section 8.
    min_header_hits: int = 5
    min_tag_run: int = 8

    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ai_detect(self) -> str:
        return self.ai_detect_model or self.ai_model

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def db_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)

    @property
    def files_enabled(self) -> bool:
        return bool(self.r2_endpoint and self.r2_bucket
                    and self.r2_access_key and self.r2_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

