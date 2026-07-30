"""Page rotation must be normalized before any geometry is trusted.

`get_text()` reports coordinates and direction vectors in *unrotated* PDF space.
A sheet authored sideways and displayed upright via /Rotate carries text whose
raw direction is (0, -1); a naive "abs(dx) > abs(dy)" test discards the whole
table.

Regression: a real 1-page door schedule with /Rotate 90 yielded 33 horizontal
spans out of 1849, scored 0, and was rejected by the finder outright.

The fixtures below mimic that authoring: text is drawn rotated by exactly the
amount /Rotate will undo, so every variant *displays* identically upright.
"""

import fitz
import pytest

ROTATIONS = [0, 90, 180, 270]


def _sheet(rotation: int) -> bytes:
    """A ruled table that displays upright at any /Rotate value.

    `insert_text(rotate=...)` cancels the page rotation, which is what real CAD
    exports do -- the page is landscape on screen but portrait in PDF space.
    """
    doc = fitz.open()
    page = doc.new_page(width=800, height=600)
    # Rotate first: derotation_matrix is identity until the page knows it is
    # rotated, and drawing methods take unrotated coordinates.
    page.set_rotation(rotation)
    text_rotate = (360 - rotation) % 360
    to_pdf = page.derotation_matrix

    page.draw_rect(fitz.Rect(30, 40, 590, 470) * to_pdf)

    rows = [["#", "FROM", "TO", "WIDTH", "HEIGHT", "MATERIAL", "HW"]]
    rows += [[str(n), "HALL", "OFFICE", "3'-0\"", "7'-0\"", "WOOD", "08"]
             for n in range(1, 13)]

    for r, cells in enumerate(rows):
        for c, cell in enumerate(cells):
            # Place by display-space intent, then map back into PDF space.
            x, y = 50 + c * 78, 70 + r * 30
            page.insert_text(fitz.Point(x, y) * to_pdf, cell,
                             fontsize=9, rotate=text_rotate)

    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_text_reads_as_horizontal_at_every_rotation(rotation):
    from app.core.pdf_doc import PdfDoc

    with PdfDoc(_sheet(rotation)) as doc:
        items = doc.text_items(0)
        width, height = doc.page_size(0)

    assert items, "no text extracted"
    vertical = [i for i in items if not i.horizontal]
    assert not vertical, (
        f"{len(vertical)} of {len(items)} spans read as vertical at /Rotate "
        f"{rotation}: {[i.text for i in vertical[:5]]}"
    )
    for item in items:
        assert -1 <= item.x0 <= width + 1, f"x outside page at /Rotate {rotation}"
        assert -1 <= item.y0 <= height + 1, f"y outside page at /Rotate {rotation}"


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_reading_order_survives_rotation(rotation):
    """Header above data, '#' left of 'HW', row 1 above row 12 -- at any
    /Rotate value, because all four render identically on screen."""
    from app.core.pdf_doc import PdfDoc

    with PdfDoc(_sheet(rotation)) as doc:
        items = doc.text_items(0)

    first = {}
    for item in items:
        first.setdefault(item.text, item)

    assert first["#"].y0 < first["1"].y0, "header not above data"
    assert first["#"].x0 < first["HW"].x0, "columns mirrored"
    assert first["1"].y0 < first["12"].y0, "rows upside down"


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_rulings_share_the_text_coordinate_space(rotation):
    """Rulings get the same transform as the text, or the grid would be
    mirrored relative to the cells it is meant to bound.

    Invariant: text drawn inside the box stays inside the box.
    """
    from app.core.pdf_doc import PdfDoc

    with PdfDoc(_sheet(rotation)) as doc:
        rulings = doc.rulings(0)
        items = doc.text_items(0)

    assert rulings.vertical and rulings.horizontal, f"rulings lost at {rotation}"
    left = min(s.pos for s in rulings.vertical)
    right = max(s.pos for s in rulings.vertical)
    top = min(s.pos for s in rulings.horizontal)
    bottom = max(s.pos for s in rulings.horizontal)

    for item in items:
        assert left - 1 <= item.cx <= right + 1, (
            f"{item.text!r} outside the ruled box horizontally at {rotation}"
        )
        assert top - 1 <= item.cy <= bottom + 1, (
            f"{item.text!r} outside the ruled box vertically at {rotation}"
        )


def test_finder_scores_the_same_regardless_of_rotation():
    """The end that matters: four sheets that render identically must score
    identically, and all four must pass the gates."""
    from app.core import page_finder
    from app.core.pdf_doc import PdfDoc

    scores = {}
    for rotation in ROTATIONS:
        with PdfDoc(_sheet(rotation)) as doc:
            scores[rotation] = page_finder.find_schedule_pages(doc)[0]

    for rotation, candidate in scores.items():
        assert candidate.passed, (
            f"/Rotate {rotation} rejected: hits={candidate.header_hits} "
            f"run={candidate.tag_run}"
        )
        assert candidate.tag_run == 12, f"tag column broken at /Rotate {rotation}"

    baseline = scores[0]
    for rotation, candidate in scores.items():
        assert candidate.header_hits == baseline.header_hits, (
            f"/Rotate {rotation} scores {candidate.header_hits} header hits, "
            f"unrotated scores {baseline.header_hits}"
        )
        assert candidate.score == baseline.score
