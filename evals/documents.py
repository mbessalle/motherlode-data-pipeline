"""A PDF's text, read once: the check steps re-read the same documents, and OCR of a scanned page takes seconds.

The text is the pipeline's own (`pipeline.pdf.pdf_text`), joined on single spaces as the search step does, and
cached on disk by the file's hash in evals/.cache/pdf_text/, so it is the same text the pipeline validated.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from pipeline.pdf import pdf_text

CACHE = Path(__file__).parent / ".cache" / "pdf_text"


def document_text(path: Path | str) -> str:
    content = Path(str(path).removeprefix("file://")).read_bytes()
    cached = CACHE / f"{hashlib.sha256(content).hexdigest()[:32]}.txt"
    if cached.exists():
        return cached.read_text()
    text = " ".join(pdf_text(content)[0].split())
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(text)
    return text
