"""The preview must outline the table, not the page, and stay upright."""

import io

import pytest
from PIL import Image

from app.core import page_finder
from app.core.pdf_doc import PdfDoc
from app.core.preview import render_preview


def _preview(data: bytes, dpi: int = 110):
    with PdfDoc(data) as doc:
        candidate = page_finder.passing(page_finder.find_schedule_pages(doc))[0]
        png = render_preview(doc, candidate, dpi=dpi)
        width, height = doc.page_size(candidate.page - 1)
    return png, width, height


def test_preview_is_a_png_matching_the_displayed_page(ellis_p21_bytes):
    png, width, height = _preview(ellis_p21_bytes)
    image = Image.open(io.BytesIO(png))

    assert image.format == "PNG"
    # Rendered at 110 dpi from a 72 dpi user space, and in *displayed*
    # orientation -- a sideways preview means rotation was mishandled.
    scale = 110 / 72
    assert image.width == pytest.approx(width * scale, rel=0.02)
    assert image.height == pytest.approx(height * scale, rel=0.02)


def test_preview_draws_the_outline(ellis_p21_bytes):
    """A red box must actually appear; a preview that renders the bare page
    would look fine to a passing glance and prove nothing."""
    plain_png, _, _ = _preview(ellis_p21_bytes)
    with PdfDoc(ellis_p21_bytes) as doc:
        bare = doc.render_png(0, dpi=110)

    marked = Image.open(io.BytesIO(plain_png)).convert("RGB")
    original = Image.open(io.BytesIO(bare)).convert("RGB")

    def strong_red(img):
        return sum(
            1 for r, g, b in img.getdata()
            if r > 150 and g < 90 and b < 90
        )

    assert strong_red(marked) > strong_red(original) + 500, "no outline drawn"


def test_preview_box_excludes_the_neighbouring_tables(ellis_p21_bytes):
    """Sheet A560 carries a hardware schedule at x<1100 and a title block at
    x>2330. The outline must sit between them."""
    from app.core.table_locator import locate_table

    with PdfDoc(ellis_p21_bytes) as doc:
        candidate = page_finder.passing(page_finder.find_schedule_pages(doc))[0]
        items = doc.text_items(0)
        grid, _ = locate_table(items, doc.rulings(0),
                               candidate.header_y, candidate.tag_x)

    assert grid.left > 1100, "outline reaches into the hardware schedule"
    assert grid.right < 2340, "outline reaches into the title block"


def test_preview_endpoint_returns_png(ellis_p21_bytes):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/door-schedule/preview",
            headers={"X-API-Key": get_settings().api_key},
            files={"file": ("p21.pdf", ellis_p21_bytes, "application/pdf")},
        )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["X-Source-Page"] == "1"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_unlocated_page_never_consults_the_locator(schedule_shaped_pdf, monkeypatch):
    """When no page passed the gates the locator must not be asked at all.

    Left to run, it latches onto whatever ruled block it can find: on a real
    sheet it boxed the MATERIAL KEY legend and captioned it DOOR SCHEDULE,
    asserting a result the finder never reached. The page is still rendered --
    it is what the vision tier read -- just without the claim.
    """
    from app.core import preview as preview_module
    from app.core.page_finder import score_page

    def explode(*_args, **_kwargs):
        raise AssertionError("locate_table called for an unlocated page")

    monkeypatch.setattr(preview_module, "locate_table", explode)

    with PdfDoc(schedule_shaped_pdf) as doc:
        candidate = score_page(doc.text_items(0), 1)
        assert not candidate.passed, "fixture should not pass the gates"
        png = render_preview(doc, candidate, located=False)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    image = Image.open(io.BytesIO(png))
    assert image.width > 0 and image.height > 0


def test_preview_endpoint_422s_when_no_schedule(no_schedule_pdf):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/door-schedule/preview",
            headers={"X-API-Key": get_settings().api_key},
            files={"file": ("blank.pdf", no_schedule_pdf, "application/pdf")},
        )
    assert r.status_code == 422
    assert "No door schedule found" in r.json()["detail"]
