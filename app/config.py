from functools import lru_cache
from typing import Annotated

from pydantic import AfterValidator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A credential, with surrounding whitespace removed.
#
# A secret almost never arrives clean. Written with `echo`, pasted into a file,
# or edited in nano, it carries a trailing newline -- and a newline cannot go
# into an HTTP header, so the request is never built. There is then no status
# code to report and the failure reads as "could not reach the provider at
# all": the account is fine, the network is fine, and the message sends you
# looking at both. One such newline in OPENROUTER_API_KEY cost an afternoon.
#
# Stripping is safe for all four of these: none of them has meaningful leading
# or trailing whitespace, and a key that genuinely needed it could not be
# transported through an environment variable anyway.
Credential = Annotated[str, AfterValidator(str.strip)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Left as the obvious placeholder on purpose, and checked at startup rather
    # than trusted -- see `main`. A default that happens to work is how an API
    # ships open: nothing fails, nothing warns, and the key protecting it is in
    # a public repository.
    api_key: Credential = "dev-key"
    # Set this in any deployment. It turns the placeholder from a convenience
    # into a refusal to start.
    require_real_api_key: bool = False
    max_upload_mb: float = 500.0
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # AI fallback (Phase 4). Absent key => the AI tier is skipped, not an error.
    openrouter_api_key: Credential = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Reading a scanned door schedule. Measured 18 November 2026 on the four
    # scanned schedules of the tester sheet (DISD, Christian Brothers, Kraus,
    # Cachet): gemini-3.8-flash read every door of all four. gemini-2.5-flash,
    # the model before it, read none of Kraus's 11 on one run and DISD's B125B
    # as B119B; gpt-6-luna listed ten hardware lines on Cachet as doors.
    ai_model: str = "google/gemini-3.8-flash"
    # Detecting doors on a plan is a different job from transcribing a table,
    # and the best model for it is an open question -- kept separate so it can
    # be changed on the evidence of scripts/door_bakeoff.py without disturbing
    # the schedule tier. Empty means "use ai_model".
    #
    # Measured 18 November 2026 (door_bakeoff, Austin OBGYN A2.1, 36 known
    # doors, 160 px a door): gpt-6.1-terra 36/36, 2 unexplained, boxes 1 pt off,
    # $0.08 -- against gemini-2.5-pro 34/36, 23 unexplained, 23 pt off, $0.38.
    # On Caliber's scanned floor plan it numbered 16 of 26 doors for $0.25,
    # where gemini-2.5-pro numbered 14 for $0.88.
    ai_detect_model: str = "openai/gpt-6.1-terra"
    # The second model a plan sheet is read with when the first finds none of
    # the sheet's scheduled doors on it, or under half of those printed there
    # as text. The answer naming more scheduled doors is kept. Dearer per tile
    # than the first, so it runs only on the sheets the first could not do.
    # Empty turns it off.
    ai_detect_fallback_model: str = "anthropic/claude-opus-5.5"
    # Reading prices from the manufacturers' books, and naming a maker from a
    # schedule's code. Its own setting so models can be tried on price pages
    # (AI-Model-Choice-and-Costs) without moving the other tiers. Empty means
    # "use ai_model".
    #
    # Held at the model it was chosen on when the schedule reader moved: the
    # price engine was measured on gemini-2.5-flash, and changing it is its
    # own decision, made on price pages.
    ai_price_model: str = "google/gemini-2.5-flash"
    # The second model every price is read with. Two models have to read the
    # same price before it shows as agreed (app/price_book/ai_check.py). Must
    # differ from the first to be worth anything; empty turns the second read
    # off and nothing is ever agreed.
    ai_price_check_model: str = "anthropic/claude-haiku-5.5"
    # How POST /price-books/ai-price prices when a request does not say:
    # "pipeline" (step A finds pages, step B reads them) or "agent" (agents
    # search the whole book; app/price_book/agent_pricing.py). Stays "pipeline"
    # until the trial of the agents is measured.
    ai_price_engine: str = "pipeline"
    ai_render_dpi: int = 200
    # How wide a door should be in the picture the detector sees. Measured:
    # at 80 px recall was 78%% and boxes landed in open rooms; at 160 px it
    # was 94%% and they landed on the doors. The cost is more tiles.
    ai_door_pixels: int = 160
    # The vision pass's spending limit is worked out per request, from the
    # work it was asked to do: every chosen sheet is measured and priced before
    # a tile is sent, and the pass may spend that price times
    # `detect_headroom`. A fixed $1.50 used to stop passes part-way -- one
    # scanned floor plan alone needs about $1.60 -- and the doors on the part
    # left over were simply missing, with no way to read just that part.
    detect_headroom: float = 1.5
    # The most one request may spend however much it was asked to do -- a
    # backstop against a loop bug, not a cost control. A pass that reaches it
    # records the parts it did not read, sheet and region, so they can be read
    # on their own (POST /documents/{id}/reprocess with `regions`).
    detect_max_usd: float = 20.0
    # Set to fix the limit instead of working it out -- the old behaviour.
    # Unset by default.
    detect_budget_usd: float | None = None
    # Most tiles one sheet may be cut into. A backstop against a loop bug, not
    # a cost control: the largest real sheet measured needs about 90 (a 1/16"
    # warehouse plan), and at the old 40 every bigger sheet was read in part.
    # Tiles past it are recorded as unread, never dropped quietly.
    max_tiles_per_sheet: int = 250

    # Supabase. Absent settings mean the service runs exactly as it did before
    # -- nothing is stored and nothing fails. A takeoff is still returned to the
    # caller; it just is not remembered.
    supabase_url: str = ""
    # The service key, not the anon key. This process is trusted and bypasses
    # row-level security; the anon key belongs in a browser, never here.
    supabase_service_key: Credential = ""
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
    r2_access_key: Credential = ""
    r2_secret_key: Credential = ""
    # How long a browser has to use an upload link before it expires.
    upload_url_ttl: int = 900

    # How many drawing sets may be read at once. One peaked at 434 MB, so two
    # together is an out-of-memory kill on a 1 GB host -- and the kernel takes
    # the whole process, losing both jobs and every request in flight. Raise it
    # with the memory, not with the core count.
    max_concurrent_extractions: int = 1

    # Memory one sheet's vector geometry may take when measuring door swings.
    # One plan sheet carries 2.9 million paths -- a hatch drawn as lines -- and
    # loading them took 4.4 GB, past the 4 GiB instance, which killed the plan
    # pass with no trace. A sheet estimated over this is not measured; its doors
    # are still placed by their printed numbers.
    geometry_budget_mb: int = 2000

    # Tell a door's number from a room's on the plans before measuring
    # (app/core/tag_evidence.py). On: where the plan read's pick of a door
    # number is a room label, it is moved to the door's own tag, or left for a
    # person to place. Where the pick is already a door tag, nothing changes.
    # ROOM_TAG_JUDGE=false turns it off without a redeploy.
    room_tag_judge: bool = True
    # ...for at most this long per plan read. A set dense enough to take
    # longer keeps the plan read's own answer, untouched.
    # Harmony's seven large sheets take about two minutes, the usual set
    # seconds.
    room_tag_judge_seconds: float = 180.0

    # Page-finder thresholds. Fitted to one document -- see PLAN.md section 8.
    min_header_hits: int = 5
    min_tag_run: int = 8

    # Watching the makers' websites for new price books (app/price_book/watch.py).
    # The name the watcher gives itself: generic on purpose, nothing about the
    # company or anyone in it.
    price_watch_user_agent: str = "PriceBookWatch/1.0"
    # Seconds between two calls to one site. Price books change a few times a
    # year; there is no hurry worth being rude for.
    price_watch_gap_s: float = 3.0
    # A PDF over this is not downloaded. The largest book loaded is far under it.
    price_watch_max_mb: float = 300.0

    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ai_detect(self) -> str:
        return self.ai_detect_model or self.ai_model

    @property
    def ai_price(self) -> str:
        return self.ai_price_model or self.ai_model

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

