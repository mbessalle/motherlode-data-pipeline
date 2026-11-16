"""Text of a PDF: the text layer first, OCR only for the pages that have none.

Most announcements are digital PDFs. Reading their text layer is free and takes milliseconds. A scanned page
has no text layer, so its text comes from OCR: RapidOCR, which runs locally on ONNX Runtime and needs no system
package. Only the pages without text are rendered and read, so a run spends OCR time on scans and on nothing else.

OCR is an optional install (`pip install ".[ocr]"`). Without it a scanned page stays empty, the source fails the
"too short" test as before, and the run report says why.
"""
from __future__ import annotations

import io
from collections.abc import Callable
from functools import cache

MIN_CHARS_PER_PAGE = 100   # a page with less text than this is treated as a scan
OCR_DPI = 200
# RapidOCR shrinks an image whose longer side is over 2000 px. An A4 page at 200 dpi is 2339 px tall, and the
# shrink turned a justified line of deal terms ("CAD$22,500 cash and 1,500,000 ... shares") into noise in a test.
OCR_MAX_SIDE = 4000


def pdf_text(content: bytes, ocr: Callable[[bytes, list[int]], list[str]] | None = None) -> tuple[str, str]:
    """(text, how it was read). `how` is 'pdf' when every page had a text layer, otherwise it names the OCR."""
    from pypdf import PdfReader

    pages = [page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages]
    scanned = [i for i, text in enumerate(pages) if len(text.strip()) < MIN_CHARS_PER_PAGE]
    if not scanned:
        return "\n".join(pages), "pdf"
    try:
        read = (ocr or ocr_pages)(content, scanned)
    except ImportError:
        return "\n".join(pages), f"pdf, {len(scanned)} of {len(pages)} pages without text (OCR not installed)"
    for index, text in zip(scanned, read, strict=True):
        if len(text.strip()) > len(pages[index].strip()):
            pages[index] = text
    return "\n".join(pages), f"pdf+ocr ({len(scanned)} of {len(pages)} pages)"


def ocr_pages(content: bytes, indexes: list[int]) -> list[str]:
    """OCR the given pages (0-based), one text per page, lines in reading order from top to bottom."""
    import numpy as np
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(content)
    texts = []
    for index in indexes:
        image = document[index].render(scale=OCR_DPI / 72).to_pil().convert("RGB")
        result = _engine()(np.array(image))
        texts.append("\n".join(result.txts or ()))
    return texts


@cache
def _engine():
    from rapidocr import RapidOCR

    return RapidOCR(params={"Global.max_side_len": OCR_MAX_SIDE, "Global.log_level": "error"})
