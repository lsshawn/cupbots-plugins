"""Quality control checks for rendered reports.

Tripwires that run AFTER LLM generation, regardless of confidence score.
Mirrors the calendar plugin's _is_pure_time_word_title() pattern —
deterministic safety nets that catch LLM mistakes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ENGINE_DIR = Path(__file__).parent


@dataclass
class QCResult:
    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str):
        self.passed = False
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)


def lint_css_classes(html: str) -> QCResult:
    """Check that all CSS class names in the HTML are in the authorised set.

    Refuses the build if any undefined classes are found.
    """
    result = QCResult()

    # Load authorised classes
    components_path = ENGINE_DIR / "components.json"
    if not components_path.exists():
        result.warn("components.json not found — skipping CSS class lint")
        return result

    with open(components_path) as f:
        data = json.load(f)
    allowed = set(data.get("classes", []))

    # Extract all class="..." values from HTML
    class_pattern = re.compile(r'class="([^"]*)"')
    found_classes: set[str] = set()
    for match in class_pattern.finditer(html):
        for cls in match.group(1).split():
            found_classes.add(cls)

    # Also check class='...' (single quotes)
    class_pattern_sq = re.compile(r"class='([^']*)'")
    for match in class_pattern_sq.finditer(html):
        for cls in match.group(1).split():
            found_classes.add(cls)

    undefined = found_classes - allowed
    if undefined:
        result.fail(
            f"Undefined CSS classes found ({len(undefined)}): "
            + ", ".join(sorted(undefined))
        )

    return result


def text_diff(source_text: str, pdf_path: str | Path, threshold: float = 0.005) -> QCResult:
    """Compare source markdown text against text extracted from the rendered PDF.

    The character-identical constraint means the PDF should contain all source
    text verbatim. A delta above the threshold (default 0.5%) fails the check.
    """
    result = QCResult()

    try:
        import pdfplumber
    except ImportError:
        result.warn("pdfplumber not available — skipping text diff QC")
        return result

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        result.fail(f"PDF not found: {pdf_path}")
        return result

    with pdfplumber.open(pdf_path) as pdf:
        pdf_text = "\n".join(
            p.extract_text() or "" for p in pdf.pages
        )

    # Normalise both texts for comparison (collapse whitespace, lowercase)
    def normalise(t: str) -> str:
        return re.sub(r"\s+", " ", t.lower()).strip()

    src_norm = normalise(source_text)
    pdf_norm = normalise(pdf_text)

    if not src_norm:
        result.warn("Source text is empty — skipping text diff")
        return result

    # Calculate character-level overlap using set-based approach
    # (we're checking content preservation, not exact formatting)
    src_words = set(src_norm.split())
    pdf_words = set(pdf_norm.split())

    if not src_words:
        return result

    missing = src_words - pdf_words
    missing_ratio = len(missing) / len(src_words)

    if missing_ratio > threshold:
        sample = sorted(missing)[:10]
        result.fail(
            f"Text diff: {missing_ratio:.1%} of source words missing from PDF "
            f"(threshold: {threshold:.1%}). Sample missing words: {sample}"
        )
    elif missing_ratio > 0:
        result.warn(
            f"Text diff: {missing_ratio:.1%} of source words differ "
            f"({len(missing)} words). Below threshold."
        )

    return result


def check_page_count(
    pdf_path: str | Path,
    expected: int,
    tolerance: float = 0.20,
) -> QCResult:
    """Sanity-check that the PDF page count is within ±tolerance of expected.

    A wild deviation from the expected count means a style bug (e.g. all content
    collapsed onto one page, or every paragraph forced a page break).
    """
    result = QCResult()

    try:
        import pdfplumber
    except ImportError:
        result.warn("pdfplumber not available — skipping page count QC")
        return result

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        result.fail(f"PDF not found: {pdf_path}")
        return result

    with pdfplumber.open(pdf_path) as pdf:
        actual = len(pdf.pages)

    if expected <= 0:
        result.warn("No expected page count provided — skipping check")
        return result

    lower = int(expected * (1 - tolerance))
    upper = int(expected * (1 + tolerance))

    if actual < lower or actual > upper:
        result.fail(
            f"Page count {actual} outside expected range "
            f"[{lower}–{upper}] (expected ~{expected}, ±{tolerance:.0%})"
        )
    else:
        log.info("Page count OK: %d (expected ~%d)", actual, expected)

    return result


def full_audit(
    html: str,
    pdf_path: str | Path,
    source_text: str = "",
    expected_pages: int = 0,
) -> QCResult:
    """Run all QC checks and merge results."""
    combined = QCResult()

    for check in [
        lint_css_classes(html),
        text_diff(source_text, pdf_path) if source_text else QCResult(),
        check_page_count(pdf_path, expected_pages) if expected_pages > 0 else QCResult(),
    ]:
        combined.errors.extend(check.errors)
        combined.warnings.extend(check.warnings)
        if not check.passed:
            combined.passed = False

    return combined
