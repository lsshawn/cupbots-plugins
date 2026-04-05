"""
Shared helpers for finance plugins.

Provides beancount loading, BQL query execution, date parsing,
ledger argument parsing, and long-text output utilities.
"""

import re
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from cupbots.helpers.logger import get_logger

log = get_logger("finance")

# Paths
from cupbots.config import get_config as _get_cfg
FINANCES_DIR = Path(_get_cfg().get("allowed_paths", {}).get("finances", "/home/ss/projects/note/finances"))
SCRIPTS_DIR = FINANCES_DIR / "scripts"
OPERATING_CURRENCY = "EUR"


def _get_ledger_paths(ledger_type: str) -> dict:
    """Get all relevant paths for a ledger type."""
    root = FINANCES_DIR / ledger_type
    return {
        "root": root,
        "journal": root / "journal.beancount",
        "summary": root / "journal_summary.beancount",
        "fx": FINANCES_DIR / "cupbots" / "fx.beancount",
        "income": root / "Income",
        "expenses": root / "Expenses",
    }


def _get_combined_journal() -> Path:
    """Path to the combined journal (both personal + cupbots)."""
    return FINANCES_DIR / "personal" / "combined.beancount"


# --- Argument parsing ---

def parse_ledger_and_args(args: list[str]) -> tuple[str, list[str]]:
    """Extract ledger type from command args. Default cupbots."""
    if args and args[0].lower() in ("personal", "cupbots"):
        return args[0].lower(), args[1:]
    return "cupbots", list(args)


PERIOD_HELP = (
    "Periods: jan-dec, q1-q4, ytd, last-month, last-quarter, "
    "last-3m, last-6m, last-12m, 2025, 2025-03"
)


def parse_date_range(args: list[str]) -> tuple[date, date, list[str]]:
    """Parse period arguments into (start_date, end_date, remaining_args).

    Supports: jan-dec, q1-q4, ytd, 2025, 2025-03, last-month, last-quarter,
    last-Nm (e.g. last-3m, last-6m, last-12m).
    Default: current month.
    """
    today = date.today()
    year = today.year

    if not args:
        # Default: current month
        start = today.replace(day=1)
        if today.month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = today.replace(month=today.month + 1, day=1)
        return start, end, []

    token = args[0].lower()
    rest = args[1:]

    # Month names (with or without year: "mar", "mar-2025")
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    month_year = re.match(r"^([a-z]{3})-(\d{4})$", token)
    if month_year and month_year.group(1) in months:
        m = months[month_year.group(1)]
        y = int(month_year.group(2))
        start = date(y, m, 1)
        end = date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)
        return start, end, rest

    if token in months:
        m = months[token]
        start = date(year, m, 1)
        end = date(year, m + 1, 1) if m < 12 else date(year + 1, 1, 1)
        return start, end, rest

    # Quarters (with or without year: "q1", "q1-2025")
    quarters = {"q1": (1, 4), "q2": (4, 7), "q3": (7, 10), "q4": (10, 1)}
    quarter_year = re.match(r"^(q[1-4])-(\d{4})$", token)
    if quarter_year and quarter_year.group(1) in quarters:
        sm, em = quarters[quarter_year.group(1)]
        y = int(quarter_year.group(2))
        start = date(y, sm, 1)
        end = date(y, em, 1) if em > 1 else date(y + 1, 1, 1)
        return start, end, rest

    if token in quarters:
        sm, em = quarters[token]
        start = date(year, sm, 1)
        end = date(year, em, 1) if em > 1 else date(year + 1, 1, 1)
        return start, end, rest

    if token == "ytd":
        return date(year, 1, 1), today + timedelta(days=1), rest

    if token == "last-month":
        first_this = today.replace(day=1)
        end = first_this
        start = (first_this - timedelta(days=1)).replace(day=1)
        return start, end, rest

    if token == "last-quarter":
        q = (today.month - 1) // 3  # 0-based current quarter
        if q == 0:
            start = date(year - 1, 10, 1)
            end = date(year, 1, 1)
        else:
            start = date(year, (q - 1) * 3 + 1, 1)
            end = date(year, q * 3 + 1, 1)
        return start, end, rest

    # last-Nm (rolling months: last-3m, last-6m, last-12m)
    rolling = re.match(r"^last-(\d+)m$", token)
    if rolling:
        n = int(rolling.group(1))
        end = today + timedelta(days=1)
        # Go back n months from start of current month
        m = today.month - n
        y = today.year
        while m < 1:
            m += 12
            y -= 1
        start = date(y, m, 1)
        return start, end, rest

    # YYYY format
    if re.match(r"^\d{4}$", token):
        y = int(token)
        return date(y, 1, 1), date(y + 1, 1, 1), rest

    # YYYY-MM format
    m = re.match(r"^(\d{4})-(\d{2})$", token)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = date(y, mo, 1)
        end = date(y, mo + 1, 1) if mo < 12 else date(y + 1, 1, 1)
        return start, end, rest

    # Unrecognized — treat as not a date arg
    return today.replace(day=1), (
        today.replace(month=today.month + 1, day=1) if today.month < 12
        else date(year + 1, 1, 1)
    ), args


# --- Beancount loading & BQL ---

def load_beancount(ledger_type: str):
    """Load a beancount journal. Returns (entries, errors, options)."""
    from beancount import loader
    journal = _get_ledger_paths(ledger_type)["journal"]
    return loader.load_file(str(journal))


def load_combined():
    """Load the combined journal (personal + cupbots). Returns (entries, errors, options)."""
    from beancount import loader
    return loader.load_file(str(_get_combined_journal()))


def run_bql(ledger_type: str, bql: str) -> str:
    """Run a BQL query and return formatted text."""
    from beanquery import query
    entries, errors, options = load_beancount(ledger_type)
    return _format_bql_result(query.run_query(entries, options, bql))


def run_bql_combined(bql: str) -> str:
    """Run a BQL query on the combined journal."""
    from beanquery import query
    entries, errors, options = load_combined()
    return _format_bql_result(query.run_query(entries, options, bql))


def run_bql_raw(ledger_type: str, bql: str):
    """Run BQL and return raw (result_types, result_rows)."""
    from beanquery import query
    entries, errors, options = load_beancount(ledger_type)
    return query.run_query(entries, options, bql)


def run_bql_combined_raw(bql: str):
    """Run BQL on combined journal and return raw (result_types, result_rows)."""
    from beanquery import query
    entries, errors, options = load_combined()
    return query.run_query(entries, options, bql)


def _format_bql_result(query_result) -> str:
    """Format BQL query result as an aligned text table."""
    result_types, result_rows = query_result

    if not result_rows:
        return "No results."

    # Extract column names
    headers = [col.name for col in result_types]

    # Convert rows to strings
    str_rows = []
    for row in result_rows:
        str_row = []
        for val in row:
            if val is None:
                str_row.append("")
            elif isinstance(val, Decimal):
                str_row.append(f"{val:,.2f}")
            elif hasattr(val, '__iter__') and not isinstance(val, str):
                # Inventory/position — render each item
                parts = []
                for item in val:
                    parts.append(str(item))
                str_row.append(", ".join(parts) if parts else "0")
            else:
                str_row.append(str(val))
        str_rows.append(str_row)

    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    # Format
    lines = []
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)
    lines.append("-" * len(header_line))
    for row in str_rows:
        lines.append("  ".join(
            cell.ljust(widths[i]) if i < len(widths) else cell
            for i, cell in enumerate(row)
        ))

    return "\n".join(lines)


# --- Output helpers ---

async def send_long_text(reply, text: str, filename: str = "report.txt"):
    """Send text as message if short, or truncate with mdpubs link if >4000 chars."""
    if len(text) <= 4000:
        await reply.reply_text(text)
        return
    # Try publishing long reports via mdpubs
    try:
        from plugins.mdpubs.mdpubs_plugin import publish_or_fallback
        url, _ = await publish_or_fallback(
            key=filename, title=filename, content=text,
        )
        if url:
            await reply.reply_text(
                text[:2000] + f"\n\n... Full report: {url}"
            )
            return
    except Exception:
        pass
    # Fallback: truncate
    await reply.reply_text(
        text[:3900] + f"\n\n... (truncated, {len(text)} chars total)"
    )


# --- Validation helpers ---

def run_bean_check(ledger_type: str) -> tuple[bool, str]:
    """Run bean-check on the journal. Returns (success, error_output)."""
    paths = _get_ledger_paths(ledger_type)
    journal = str(paths["journal"])
    bean_check = str(FINANCES_DIR.parent / "venv" / "bin" / "bean-check")

    try:
        result = subprocess.run(
            [bean_check, journal],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, "Valid"
        else:
            errors = (result.stderr or result.stdout or "Unknown error").strip()
            return False, errors[:2000]
    except FileNotFoundError:
        return False, "bean-check not found"
    except Exception as e:
        return False, str(e)


def run_bean_format(ledger_type: str) -> bool:
    """Run bean-format on the journal."""
    paths = _get_ledger_paths(ledger_type)
    journal = str(paths["journal"])
    bean_format = str(FINANCES_DIR.parent / "venv" / "bin" / "bean-format")

    try:
        result = subprocess.run(
            [bean_format, "-o", journal, journal],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def regenerate_summary(ledger_type: str) -> bool:
    """Regenerate journal_summary.beancount."""
    script = SCRIPTS_DIR / "generate_journal_summary.py"
    if not script.exists():
        return False
    python = str(FINANCES_DIR.parent / "venv" / "bin" / "python")

    try:
        result = subprocess.run(
            [python, str(script), ledger_type],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False
