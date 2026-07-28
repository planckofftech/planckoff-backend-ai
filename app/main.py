import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import router
from app.config import get_settings

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
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
