"""PDF text: the text layer is read for free, and only pages without one go to OCR."""
import io
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from pipeline.pdf import pdf_text
from pipeline.search import read_page

DEAL = ("Acme Gold Corp acquires 100% of the Foggy Mountain claims in exchange for CAD$22,500 cash and 1,500,000 "
        "fully paid ordinary shares.")


def digital_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, written by hand so the test needs no PDF library to create it."""
    stream = f"BT /F1 9 Tf 20 400 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(b"%PDF-1.4\n"), []
    out.seek(0, 2)
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    out.write(b"".join(b"%010d 00000 n \n" % offset for offset in offsets))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def with_blank_page(content: bytes) -> bytes:
    """The PDF plus a page with no text layer, which is what a scanned page looks like to a text extractor."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(content)))
    writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_digital_pdf_is_read_without_ocr():
    def ocr(content, pages):
        raise AssertionError("OCR must not run on a page that has text")

    text, how = pdf_text(digital_pdf(DEAL), ocr=ocr)
    assert "1,500,000" in text and how == "pdf"


def test_only_the_page_without_text_goes_to_ocr():
    calls = []

    def ocr(content, pages):
        calls.append(pages)
        return ["Scanned page: the vendors retain a 2% NSR royalty, buy-back of 1% for CAD$500,000. " * 2]

    text, how = pdf_text(with_blank_page(digital_pdf(DEAL)), ocr=ocr)
    assert calls == [[1]]
    assert "1,500,000" in text and "CAD$500,000" in text
    assert how == "pdf+ocr (1 of 2 pages)"


def test_without_ocr_installed_the_scan_stays_empty_and_the_reason_is_recorded():
    def ocr(content, pages):
        raise ImportError("rapidocr")

    text, how = pdf_text(with_blank_page(digital_pdf(DEAL)), ocr=ocr)
    assert "1,500,000" in text and how == "pdf, 1 of 2 pages without text (OCR not installed)"


def test_read_page_hands_a_pdf_to_the_pdf_reader():
    def get(url, headers, timeout):
        return SimpleNamespace(status_code=200, headers={"content-type": "application/pdf"}, content=digital_pdf(DEAL))

    text, how = read_page("https://asx.test/announcement.pdf", get)
    assert "CAD$22,500" in text and how == "pdf"


def test_real_ocr_reads_the_numbers_of_a_scanned_deal():
    """Runs RapidOCR on a page image. Skipped unless the ocr extra is installed; the models (about 31 MB) are
    downloaded on first use."""
    pytest.importorskip("rapidocr")
    from PIL import Image, ImageDraw, ImageFont

    page = Image.new("L", (1654, 2339), 255)                       # A4 at 200 dpi
    font = ImageFont.load_default(size=30)
    draw = ImageDraw.Draw(page)
    for row, line in enumerate(("Acme Gold Corp acquires 100% of the Foggy Mountain claims in exchange for",
                                "CAD$22,500 cash and 1,500,000 fully paid ordinary shares.",
                                "The vendors retain a 2% NSR royalty; 1% can be bought back for CAD$500,000.")):
        draw.text((160, 300 + row * 48), line, fill=0, font=font)
    scan = io.BytesIO()
    page.save(scan, format="PDF", resolution=200)

    text, how = pdf_text(scan.getvalue())
    assert how == "pdf+ocr (1 of 1 pages)"
    for number in ("22,500", "1,500,000", "500,000", "100%", "2%"):
        assert number in text, (number, text)
