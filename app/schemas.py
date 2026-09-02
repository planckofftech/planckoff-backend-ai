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
    item_count: int = Field(
        0, description="Horizontal text spans found. Near zero means a scan."
    )


class TableBox(BaseModel):
    """Where a table sits on its page, as fractions of the page (0-1).

    Fractions rather than points, because the only consumer is the preview,
    which draws onto an image rendered at whatever dpi it likes.

    Only the AI tier fills this in. The deterministic tier does not need it:
    the preview re-measures the grid itself and gets the exact rulings.
    """

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    source: str = Field(
        description="'text' when the extracted values were found in the page's "
        "own text layer, 'model' when the model estimated it from the image"
    )


class ScheduleTable(BaseModel):
    """One schedule. A sheet often carries several stacked down the page --
    a main door schedule, then residential units, then guestrooms."""

    title: str = Field("", description="Caption as printed above the table")
    page: int
    headers: list[str] = Field(default_factory=list)
    field_map: list[str | None] = Field(
        default_factory=list,
        description="Canonical field per column, aligned to `headers`; null "
        "where the column had no equivalent and went to `extra`.",
    )
    row_count: int = 0
    rows: list[DoorRow] = Field(default_factory=list)


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
    tables: list[ScheduleTable] = Field(
        default_factory=list,
        description="Every schedule found. `rows` above is the largest of them.",
    )
    page_scores: list[PageScore] = Field(
        default_factory=list, description="Diagnostics; only when ?debug=true"
    )
    box: TableBox | None = Field(
        None,
        description="Where the table was found, when the AI tier read it. The "
        "preview draws this; without it an AI-read page shows no outline.",
    )


class SheetRef(BaseModel):
    """One sheet of the set, named the way the title block names it."""

    page: int
    number: str = Field("", description="Sheet number, e.g. 'A2.10'")
    title: str = Field("", description="Sheet title, e.g. 'GROUND LEVEL - FLOOR PLAN'")
    width: float = Field(0.0, description="Sheet width in PDF points")
    height: float = Field(0.0, description="Sheet height in PDF points")
    level: str = Field(
        "",
        description="Which storey this sheet draws, e.g. 'LEVEL 1', 'LEVEL B1', "
        "'MEZZANINE'. Empty on a single-floor job, which is not a failure -- it "
        "means the building.",
    )
    leads: bool = Field(
        False,
        description="Deprecated -- identical to `scanned`, kept so existing "
        "callers keep working. It used to name one 'lead' sheet per storey, "
        "elected by comparing each sheet against its own size; two sheets "
        "contributing the same eight doors could get opposite answers. Read "
        "`scanned` instead.",
    )
    scanned: bool = Field(
        False,
        description="Did the audit actually read this sheet? A sheet can be a "
        "real floor plan and still be skipped: an overall plan at 1/16\" "
        "redraws doors the partial plans already show at 1/8\", so scanning it "
        "again finds nothing new. Without this a deliberate skip and a genuine "
        "miss both read as zero doors found.",
    )
    is_enlargement: bool = Field(
        False,
        description="Does this sheet blow up one part of the building rather "
        "than draw the whole floor? Enlargements and reduced-scale keys are "
        "worth showing -- they are how a person finds one door on a plan too "
        "big to read -- but they are not where the count comes from.",
    )


class DoorSwing(BaseModel):
    """The door's swing, measured off the drawing, in PDF points.

    A rectangle says a door is somewhere near here. This says what the door
    actually is: struck from `hinge`, `radius` long, opening from `start_deg`
    to `end_deg`. It is what the drawing itself contains, recovered by fitting
    a circle to the arc's own ink -- so it can be drawn back over the plan as
    the door rather than as a box around it.

    Points, not page fractions, because a page is wider than it is tall and a
    circle expressed in fractions of each is an ellipse.
    """

    hinge_x: float
    hinge_y: float
    radius: float = Field(description="Leaf length in points")
    start_deg: float
    end_deg: float
    residual: float = Field(
        0.0,
        description="How far the ink sat off the fitted circle, in points. "
        "A real door arc fits to about 0.04; the curves that impersonate one "
        "-- basin bowls, chair backs -- to about 0.17.",
    )


class DoorLocation(BaseModel):
    """Where one door was found on a drawing.

    Coordinates are fractions of the page, matching TableBox, so the preview
    can outline it at whatever dpi it renders.
    """

    page: int
    sheet: str
    x0: float
    y0: float
    x1: float
    y1: float


class DoorSightingOut(BaseModel):
    tag: str
    confidence: str = Field(
        description="'unique' (one place on each sheet), 'resolved' (several "
        "candidates, one clearly best), 'ambiguous' (a tie -- check it), or "
        "'not_found'"
    )
    locations: list[DoorLocation] = Field(default_factory=list)


class UnscheduledDoorOut(BaseModel):
    label: str
    location: DoorLocation
    reasons: list[str] = Field(default_factory=list)


class DetectedDoorOut(BaseModel):
    """A door found as a *shape* on the drawing, not as a text label.

    This is the thing the tag-based pass could never give: the door itself, and
    what kind of door it is. `tag` is filled in when one of the schedule's doors
    was found beside it -- when it is empty, nothing in the schedule accounts
    for this door.
    """

    location: DoorLocation
    type: str = Field(description="single_swing, double_swing, sliding, "
                                  "pocket, opening_no_door, ...")
    swing: str = ""
    tag: str = Field("", description="Matched schedule door, or empty")
    confidence: str = ""
    schedule: dict[str, str] = Field(
        default_factory=dict,
        description="What the schedule says about this door, so a detail panel "
        "needs no second request. Empty when no schedule door matched.",
    )
    wall_type: str = Field(
        "",
        description="The wall this door sits in, as the drawing's own tag "
        "names it -- '2C', 'A3', '1'. Empty when the drawing did not settle "
        "it; see `wall_type_options`.",
    )
    wall_type_options: list[str] = Field(
        default_factory=list,
        description="The shortlist when two tags sit at much the same distance "
        "and the drawing does not say which governs this door. A person picks. "
        "Never more than three.",
    )
    wall_type_source: str = Field(
        "",
        description="'tag' when read off the plan's own tags, 'ai' when the "
        "legend's layout defeated the deterministic reader and the vision tier "
        "read it instead.",
    )
    source: str = Field(
        "model",
        description="'geometry' when the box is the measured extent of the "
        "door's own swing arc, 'model' when it is the detector's estimate -- "
        "true for sliding doors and cased openings, which have no arc to "
        "measure.",
    )
    measured_width: str = Field(
        "",
        description="Leaf width read off the drawing, e.g. \"3' - 0\\\"\". "
        "Independent of the schedule, so the two can be compared.",
    )
    arc: DoorSwing | None = Field(
        None,
        description="The swing as measured, so a viewer can draw the door "
        "itself rather than a box around it. Absent where there was no arc to "
        "measure -- a sliding door, a pocket door, a cased opening.",
    )
    other_leaf: DoorSwing | None = Field(
        None,
        description="A pair's second leaf. Both are measured; keeping only "
        "one drew a six-foot opening as a three-foot door.",
    )
    primary: bool = Field(
        True,
        description="Is this the drawing of this door to price from? A set "
        "draws the same door several times -- overall plan, partial plan, "
        "enlargement -- and one door must produce one line on a takeoff. The "
        "others are kept so the viewer can still draw the door on the sheet "
        "you are looking at, but they are not counted.",
    )
    also_on: list[str] = Field(
        default_factory=list,
        description="Every sheet this same door is drawn on, smallest scale "
        "first -- overall plan, then the partial, then the enlargement. This "
        "is the order a person reads the set in.",
    )
    sheet_scale: float = Field(
        0.0,
        description="How large a door is drawn on this sheet, in points per "
        "leaf. Measured off the drawing, so it compares two sheets without "
        "either of them having to state a scale.",
    )


class ScanCost(BaseModel):
    """What the detection pass spent, reported per run rather than discovered
    on an invoice."""

    model: str = ""
    sheets: int = 0
    tiles_planned: int = 0
    tiles_sent: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_usd: float = Field(
        0.0, description="What was actually billed, at the called model's rate")
    predicted_usd: float = Field(
        0.0, description="What the run was expected to cost before it started")
    dry_run: bool = False


class PlanAudit(BaseModel):
    """The schedule set against the drawings."""

    status: str = "ok"
    pages_scanned: int = 0
    duration_ms: int = 0
    schedule_page: int = 0
    floor_plans: list[SheetRef] = Field(default_factory=list)
    door_count: int = 0
    located: list[DoorSightingOut] = Field(default_factory=list)
    not_on_plans: list[DoorSightingOut] = Field(default_factory=list)
    unscheduled: list[UnscheduledDoorOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    coverage_note: str = Field(
        "",
        description="What this audit can and cannot see, in plain words -- so "
        "'0 unscheduled doors' is never read as more than it means.",
    )
    detected: list[DetectedDoorOut] = Field(
        default_factory=list,
        description="Doors found as shapes on the plan. Only when ?detect=true; "
        "this is the pass that costs money.",
    )
    scan_cost: ScanCost | None = Field(
        None, description="What the detection pass spent, if it ran"
    )


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    ai_enabled: bool


class ErrorResponse(BaseModel):
    status: str = "error"
    detail: str
    pages_scanned: int | None = None
