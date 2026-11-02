"""One reader per page layout, and which books use which.

A layout is named for how its prices are laid out, not for a brand: Allegion
prints several brands the same way, so one reader serves all of them once it is
shown to fit. A book is matched to its readers by file name; an unlisted book is
reported as not loaded rather than read by a reader that merely might fit.

A book's readers run in order. The first reads every page it knows; each after
it reads only the pages left wholly unread, so no price is read twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import fitz

from app.price_book import check
from app.price_book.readers import finish_columns, size_finish_grid, table_rows
from app.price_book.rows import BookRead

Reader = Callable[..., BookRead]

_TABLE = (table_rows.read,)

# file name -> (manufacturer, readers). The name is the one a quote answers
# with; lookup.py maps the schedule codes (HA, VD, LC ...) onto it.
BOOKS: dict[str, tuple[str, tuple[Reader, ...]]] = {
    "Von_Duprin_Price_Book_110058.pdf": ("Von Duprin", (finish_columns.read, table_rows.read)),
    "2026_Price_Book_Complete.pdf": ("Hager", (size_finish_grid.read, table_rows.read)),
    "2026_HS4_Price_Book_v9.pdf": ("Hager", _TABLE),
    "PL15_v1.pdf": ("ABH", _TABLE),
    "Advantage_Price_Book_111615.pdf": ("Allegion Advantage", _TABLE),
    "AlarmLock-Price-Book-2025-v2.pdf": ("Alarm Lock", _TABLE),
    "AMLOCK_pricebook_comcarded_8-2022.pdf": ("American Lock", _TABLE),
    "AMLOCK_pricebook_secprods_8-2022.pdf": ("American Lock", _TABLE),
    "arrow_price_book.pdf": ("Arrow", _TABLE),
    "2025_BEA_Price_Book.pdf": ("BEA", _TABLE),
    "PL78-USA-BEST-Hinges-08-26.pdf": ("Best", _TABLE),
    "PL78-USA-BEST-Locks-Cores-Closers-08-26.pdf": ("Best", _TABLE),
    "PL78-USA-BEST-PRECISION-08-26.pdf": ("Best", _TABLE),
    "PL78-USA-BEST-Sliding-Folding-08-26-1.pdf": ("Best", _TABLE),
    "Brio_PriceBook_111608.pdf": ("Brio", _TABLE),
    "2026_US_Price_List_R6.pdf": ("Camden", _TABLE),
    "Corbin Russwin Price Book - February 02,2026 (04-17-26).pdf": ("Corbin Russwin", _TABLE),
    "design_hardware_Price_Book-USA-01-25.pdf": ("Design Hardware", _TABLE),
    "detex-2026-price-book.pdf": ("Detex", _TABLE),
    "Dexter_Price_Book_111010.pdf": ("Dexter", _TABLE),
    "DON_pricebook_2022.pdf": ("Don-Jo", _TABLE),
    "2026_Dorma_Auditcon_Price_Book.pdf": ("dormakaba", _TABLE),
    "PL38-USA-dk-Architectural-03-26.pdf": ("dormakaba", _TABLE),
    "PL78-E-Plex-Locks-Price_List-USA-03-26.pdf": ("dormakaba", _TABLE),
    "PL78-MF-Locks-Legacy-Price_Book-USA-03-26.pdf": ("dormakaba", _TABLE),
    "PL78-MF-Locks-Price_Book-USA-03-26.pdf": ("dormakaba", _TABLE),
    "PL78-USA-dk-Commercial-03-26.pdf": ("dormakaba", _TABLE),
    "PL78-USA-dk_Simplex-03-26.pdf": ("dormakaba", _TABLE),
    "PL78-USA_RCI_Price_List-03-26.pdf": ("dormakaba", _TABLE),
    "PL78_KEYSCAN_Price_List-USA-03-26.pdf": ("dormakaba", _TABLE),
    "DSI_2026_LIST_PRICING.pdf": ("Dortronics", _TABLE),
    "2026_EMTEK_CABINET_BATH_PRICE-BOOK.pdf": ("Emtek", _TABLE),
    "2026_EMTEK_DOOR-HARDWARE_PRICE-BOOK.pdf": ("Emtek", _TABLE),
    "2026_EMTEK_MULTI-POINT_PRICE-BOOK_US-EN_3_JUL10_-_PRINT_1.pdf": ("Emtek", _TABLE),
    "2026_YALE-EXPRESSIONS_PRICE-BOOK_US-EN_3_JUL10_-_PRINT.pdf": ("Emtek", _TABLE),
    "Falcon_Price_Book_110065.pdf": ("Falcon", _TABLE),
    # Allegion's quick-ship programmes price Schlage, LCN and Von Duprin items
    # their own way, so they stand as makers of their own.
    "Allegion_FastTrack_Price_Book_110064.pdf": ("Allegion FastTrack", _TABLE),
    "Allegion_ProExpress_Price_Book_110445.pdf": ("Allegion ProExpress", _TABLE),
    "Glynn-Johnson_Price_Book_110061.pdf": ("Glynn-Johnson", _TABLE),
    "Ives_Price_Book_110060.pdf": ("Ives", _TABLE),
    "Peaks_Plus_Price_Book_06012026.pdf": ("Kaba Ilco", _TABLE),
    "Kwikset-Box-Pack-Price-Book-Effective-August-2026.pdf": ("Kwikset", _TABLE),
    "Kwikset-BP-Price-Book-Parts-Accessories-June-2026.pdf": ("Kwikset", _TABLE),
    "Kwikset-Clear-Pack-Price-Book-Effective-August-2026.pdf": ("Kwikset", _TABLE),
    "Safe-Lock-Price-Book-Effective-June-2026.pdf": ("Kwikset", _TABLE),
    "LCN_Price_Book_113462.pdf": ("LCN", _TABLE),
    "LOCKEY_pricebook_6-2022.pdf": ("Lockey", _TABLE),
    "Locknetics_Price_Book_113816.pdf": ("Locknetics", _TABLE),
    "ALA530E_-_Marks_2026.pdf": ("Marks", _TABLE),
    "mckinney_price_book_2026.pdf": ("McKinney", _TABLE),
    "medeco_u_s__catalog___price_book_2026.pdf": ("Medeco", _TABLE),
    # Two NGP lists are in the folder; the June 2026 one replaces February's.
    "PRICELIST_JUNE_8th_050726.pdf": ("National Guard", _TABLE),
    "43393_Nor_Rix_PB_Lo (5).pdf": ("Norton Rixson", _TABLE),
    "1768868169086-PBB_Price_Book_2026.pdf": ("PBB", _TABLE),
    "PDQ_2026_Price_Book.pdf": ("PDQ", _TABLE),
    "markar_and_pemko_price_book.pdf": ("Pemko", _TABLE),
    "Republic_Price_Book_163021.pdf": ("Republic", _TABLE),
    "rockwood_price_book___accessories_2026 (1).pdf": ("Rockwood", _TABLE),
    "rockwood_price_book___architectural_series_2026 (1).pdf": ("Rockwood", _TABLE),
    "SARGENT Price Book - July 13,2026 (Rev 8 24 26).pdf": ("Sargent", _TABLE),
    "Allegion_MultiFamily_Price_Book_112057.pdf": ("Schlage", _TABLE),
    "SCH_Residential_Price_Book_17_MR1000_Rev_Oct2026.pdf": ("Schlage", _TABLE),
    "Schlage_Electronics_Price_Book_110056.pdf": ("Schlage", _TABLE),
    "Schlage_Mechanical_Price_Book_110057.pdf": ("Schlage", _TABLE),
    "SDC_PRICE_BOOK_2025.pdf": ("SDC", _TABLE),
    "Securitech_4800_Series_2026_Pricebook_5400-015.pdf": ("Securitech", _TABLE),
    "Securitech_Serpent_Price_List_3-2025.pdf": ("Securitech", _TABLE),
    "Securitech_SerpentMD_Price_List_3-2025.pdf": ("Securitech", _TABLE),
    "Securitech_Trident_Series_2026_LIST_Pricebook_5400-030.pdf": ("Securitech", _TABLE),
    "SOSS_Price_Book_116140.pdf": ("SOSS", _TABLE),
    "Steelcraft_Price_Book_110063.pdf": ("Steelcraft", _TABLE),
    "STI_pricebook_2023.pdf": ("STI", _TABLE),
    "2026_TownSteel_Price_Book.pdf": ("TownSteel", _TABLE),
    "Trimco_List_Price_26.pdf": ("Trimco", _TABLE),
    "42001_ACC_PB_Lo (1).pdf": ("Yale", _TABLE),
    "42930_ACC_Multi-FamilyHousing_DistPriceBook_Lo.pdf": ("Yale", _TABLE),
    "AAA Residential Price Book February 02,2026 Rev. 7-13-26.pdf": ("Yale", _TABLE),
    "Zero_Price_Book_110962.pdf": ("Zero", _TABLE),
    "ZKA_pricebook_2017.pdf": ("ZKTeco", _TABLE),
}

# In the folder but not read, and why -- listed so the report can say so.
SKIPPED: dict[str, str] = {
    "Pages from Schlage_Mechanical_Price_Book_110057.pdf": "one page cut from the Schlage mechanical book, which is read whole",
    "NGP_Price_Book_2-1-2026.pdf": "replaced by the June 2026 NGP list",
    "AADSS1261453.pdf": "no printed prices found",
    "Alarm_Controls_Feb_2_2026_Pricelist_partial.pdf": "no printed prices found",
    "HES_Feb_2_2026_Pricelist_partial.pdf": "no printed prices found",
    "Securitron_Feb_2_2026_Pricelist_partial.pdf": "no printed prices found",
    "PRICING_GUIDE.pdf": "no printed prices found",
    "rockwood_lites_and_louvers_price_book_2026.pdf": "no printed prices found",
}


def read_book(path: str | Path) -> BookRead:
    """Read a listed book and keep only the prices found on their own pages."""
    path = Path(path)
    if path.name not in BOOKS:
        raise KeyError(f"No reader is listed for {path.name}")
    manufacturer, readers = BOOKS[path.name]
    with fitz.open(path) as doc:
        book = readers[0](doc, manufacturer, path.name)
        for reader in readers[1:]:
            book = _then(book, reader(doc, manufacturer, path.name,
                                      pages={u.pdf_page for u in book.unread
                                             if u.reason == "layout not read yet"}))
        check.verify(book, doc)
    for row in book.rows:
        row.book = path.name
    return book


def _then(book: BookRead, more: BookRead) -> BookRead:
    """`book` with the pages `more` read in place of their unread entries."""
    retried = {u.pdf_page for u in book.unread if u.reason == "layout not read yet"}
    book.unread = [u for u in book.unread if u.pdf_page not in retried] + more.unread
    book.unread.sort(key=lambda u: u.pdf_page)
    book.rows += more.rows
    book.prices_read += more.prices_read
    book.rejected += more.rejected
    book.reader = f"{book.reader}+{more.reader}"
    return book
