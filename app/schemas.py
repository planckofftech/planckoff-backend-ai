from enum import Enum

from pydantic import BaseModel, Field


class ExtractionMethod(str, Enum):
    """Which tier produced the rows. The single most important field in the
    response -- it is the accuracy dashboard and the token bill in one string."""

    DETERMINISTIC_RULED = "deterministic_ruled"
    DETERMINISTIC_BANDED = "deterministic_banded"
    AI_VISION = "ai_vision"
    NONE = "none"


CANONICAL_FIELDS = (
    "door_tag",
    "from_space",
    "to_space",
    "door_width",
    "door_height",
    "door_type",
    "door_material",
    "door_finish",
    "frame_material",
    "frame_finish",
    "threshold",
    "fire_rating",
    "hw_set",
    "comments",
)


class DoorRow(BaseModel):
    door_tag: str = ""
    from_space: str = ""
    to_space: str = ""
    door_width: str = ""
    door_height: str = ""
    door_type: str = ""
    door_material: str = ""
    door_finish: str = ""
    frame_material: str = ""
    frame_finish: str = ""
    threshold: str = ""
    fire_rating: str = ""
    hw_set: str = ""
    comments: str = ""
    extra: dict[str, str] = Field(
        default_factory=dict,
        description="Columns whose header did not map to a canonical field. "
        "Never dropped.",
    )


class PageScore(BaseModel):
    """Per-page diagnostics from the page finder. Emitted for every page so the
    thresholds can be retuned from real data instead of guessed."""

    page: int = Field(description="1-indexed page number")
    header_hits: int
    header_y: float
    tag_run: int
    tag_x: float
    score: int
    passed: bool


class ExtractionResult(BaseModel):
    status: str = "ok"
    method: ExtractionMethod
    pages_scanned: int
    source_pages: list[int] = Field(default_factory=list)
    row_count: int = 0
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    headers: list[str] = Field(
        default_factory=list, description="Raw column headers as printed on the sheet"
    )
    rows: list[DoorRow] = Field(default_factory=list)
    page_scores: list[PageScore] = Field(
        default_factory=list, description="Diagnostics; only when ?debug=true"
    )


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    ai_enabled: bool


class ErrorResponse(BaseModel):
    status: str = "error"
    detail: str
    pages_scanned: int | None = None
