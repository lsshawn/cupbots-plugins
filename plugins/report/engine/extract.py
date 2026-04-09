"""Extract text from uploaded source files (MD, PDF, DOCX, TXT).

Pure Python — no CupBots imports. Used by the plugin to ingest user-uploaded
documents before passing them through the synthesis pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}


def extract_text(file_path: str | Path) -> str:
    """Extract plain text from a file.

    Raises:
        ValueError: Unsupported file extension.
        FileNotFoundError: File does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: {suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8")

    if suffix == ".pdf":
        return _extract_pdf(path)

    if suffix == ".docx":
        return _extract_docx(path)

    return ""


def _extract_pdf(path: Path) -> str:
    """Extract text from PDF using pdfplumber (same dep wiki already uses)."""
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is required for PDF extraction. "
            "Declared in plugin.json pip_dependencies."
        )

    with pdfplumber.open(path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages[:100]]
    return "\n\n".join(pages)


def _extract_docx(path: Path) -> str:
    """Extract text from DOCX using python-docx."""
    try:
        import docx
    except ImportError:
        raise ImportError(
            "python-docx is required for DOCX extraction. "
            "Declared in plugin.json pip_dependencies."
        )

    doc = docx.Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)
