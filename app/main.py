import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import router
from app.api.storage import router as storage_router
from app.config import get_settings

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# A placeholder key is fine on a laptop and is an open door anywhere else.
# Checked here, at import, so a misconfigured deployment fails to start instead
# of serving every takeoff to anyone who guesses the default.
if settings.require_real_api_key and settings.api_key == "dev-key":
    raise RuntimeError(
        "API_KEY is still the default 'dev-key' while REQUIRE_REAL_API_KEY is "
        "set. Refusing to start: set API_KEY to a real secret."
    )
if settings.api_key == "dev-key":
    logging.getLogger(__name__).warning(
        "API_KEY is the default 'dev-key' -- fine locally, never in a "
        "deployment. Set REQUIRE_REAL_API_KEY=true there to make this fatal."
    )

app = FastAPI(
    title="Planckoff Door Schedule Extraction API",
    version=__version__,
    description=(
        "Takes a construction PDF, returns the door schedule as structured JSON.\n\n"
        "Structure is recovered by code wherever it survives in the PDF; the "
        "vision model is the fallback for pages where it genuinely did not. "
        "The `method` field reports which tier produced the rows."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
# Kept separate from the takeoff endpoints: one produces answers, the other
# remembers them, and the second is useless without a database while the first
# has never needed one.
app.include_router(storage_router)

