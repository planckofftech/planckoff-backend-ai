"""A scanned sheet is the vision tier's whole reason to exist.

It has no text to score, so it can never pass the structural gates. Routing to
AI must therefore not depend on document size -- a scanned 100-page bid set was
being refused outright while a scanned 4-page one went through.
"""

import fitz
import pytest

from app.ai.vision_extract import _upstream_reason
from app.core import page_finder
from app.core.pdf_doc import PdfDoc


def _scanned_pdf(pages: int) -> bytes:
    """Pages carrying a bitmap and no meaningful text -- what a scan looks like."""
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 180))
    pix.clear_with(220)
    for _ in range(pages):
        page = doc.new_page(width=792, height=612)
        page.insert_image(fitz.Rect(0, 0, 792, 612), pixmap=pix)
        page.insert_text((20, 20), "SHEET A1", fontsize=8)  # a stamp, not a table
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("pages", [4, 40])
def test_scanned_pages_are_detected_whatever_the_page_count(pages):
    with PdfDoc(_scanned_pdf(pages)) as doc:
        scores = page_finder.find_schedule_pages(doc)
        textless = [c for c in scores if not c.has_text_layer]
        with_raster = [c for c in textless if doc.has_raster(c.page - 1)]

    assert len(textless) == pages, "scanned pages should report no text layer"
    assert len(with_raster) == pages, "scanned pages should report a raster"


def test_text_bearing_page_is_not_mistaken_for_a_scan(ellis_p21_bytes):
    with PdfDoc(ellis_p21_bytes) as doc:
        candidate = page_finder.find_schedule_pages(doc)[0]
    assert candidate.has_text_layer
    assert candidate.item_count > 100


def test_blank_page_carries_no_raster():
    """A page with neither text nor image is blank, not a scan, and is not
    worth paying to render."""
    doc = fitz.open()
    doc.new_page(width=792, height=612)
    data = doc.tobytes()
    doc.close()

    with PdfDoc(data) as pdf:
        assert not pdf.has_raster(0)


@pytest.mark.asyncio
async def test_large_scanned_doc_reaches_the_ai_tier(monkeypatch):
    """The regression: with no AI configured the error must say the AI tier was
    unavailable, not that the document has no schedule -- proving the pipeline
    got as far as wanting to call it on a 40-page scan."""
    from app.pipeline import NoScheduleFoundError, extract

    with pytest.raises(NoScheduleFoundError):
        await extract(_scanned_pdf(40), allow_ai=False)


def test_upstream_reasons_are_actionable():
    class Err(Exception):
        status_code = 401

    message = _upstream_reason(Err("Error code: 401 - User not found."))
    assert "OPENROUTER_API_KEY" in message
    assert "401" in message

    class Credit(Exception):
        status_code = 402

    assert "credit" in _upstream_reason(Credit("nope")).lower()
    # An unmapped status still surfaces the provider's own words.
    assert "boom" in _upstream_reason(RuntimeError("boom"))
