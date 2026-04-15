"""
Finance — /finance command hub for all write/maintenance operations.

Commands:
  /finance [cupbots|personal] [income|expenses]  — Scan & process unprocessed invoices
  /finance expense [personal] <desc>     — Add expense (attach receipt)
  /finance income [personal] <desc>      — Record income (attach invoice)
  /finance transfer [personal] <desc>    — Inter-account transfer
  /finance ar [personal] <desc>          — Record invoice sent (AR entry)
  /finance payment [personal] <desc>     — Record payment received (clear AR)
  /finance void [personal] <search>      — Void an entry (reversing entry)
  /finance validate [personal]           — Run bean-check
  /finance fxsync                        — Fetch missing FX rates
  /finance summary [personal]            — Regenerate journal summary
  /finance reconcile [personal] <acct> <bal> <cur> — Reconcile account
  /finance duplicates [personal]         — Scan for duplicate entries
  /finance invoice <client> <items>      — Create & send Stripe invoice
  /finance invoice list [client]         — List invoices
  /finance invoice status <id>           — Check invoice status
  /finance invoice accounts              — List Stripe accounts
"""

import asyncio
import re
import subprocess
import sys
import tempfile
from copy import copy
from datetime import datetime
from pathlib import Path

import httpx

from cupbots.config import get_config
from cupbots.helpers.access import is_admin
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli, _extract_json
from plugins._finance_helpers import send_long_text

log = get_logger("finance")

# Paths
from cupbots.config import get_config as _get_cfg
FINANCES_DIR = Path(_get_cfg().get("allowed_paths", {}).get("finances", "/home/ss/projects/note/finances"))
SCRIPTS_DIR = FINANCES_DIR / "scripts"

# FX API (same as generate_rates.py)
FX_API_TEMPLATE = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date}/v1/currencies/{currency}.json"
OPERATING_CURRENCY = "EUR"

# Processing queue: list of (ledger_type, file_path) pending user action
_queue: list[dict] = []
_processing = False

COMMANDS = (
    "finance",
)

SUBCOMMANDS = ("expense", "income", "transfer", "ar", "payment")

# Map /finance subcommands to (target_plugin_module, rewritten_command)
_DELEGATED_SUBCOMMANDS = {
    # finance_maintenance
    "void": ("finance_maintenance", "void"),
    "validate": ("finance_maintenance", "validate"),
    "fxsync": ("finance_maintenance", "fxsync"),
    "summary": ("finance_maintenance", "summary"),
    # finance_audit
    "reconcile": ("finance_audit", "reconcile"),
    "duplicates": ("finance_audit", "duplicates"),
    # invoice
    "invoice": ("invoice", "invoice"),
}


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


def _scan_unprocessed(ledger_type: str, folder_filter: str | None = None) -> list[Path]:
    """Find unprocessed invoice files (PDFs, images) in Income/ and Expenses/ root."""
    paths = _get_ledger_paths(ledger_type)
    files = []
    extensions = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic"}

    folder_map = {"income": [paths["income"]], "expenses": [paths["expenses"]]}
    folders = folder_map.get(folder_filter, [paths["income"], paths["expenses"]])
    for folder in folders:
        if not folder.exists():
            continue
        for item in sorted(folder.iterdir()):
            if item.is_file() and item.suffix.lower() in extensions:
                files.append(item)

    return files


def _read_existing_fx_rates() -> dict[tuple[str, str], float]:
    """Read existing FX rates from cupbots/fx.beancount into {(date, currency): rate}."""
    fx_path = FINANCES_DIR / "cupbots" / "fx.beancount"
    rates = {}
    if not fx_path.exists():
        return rates
    for line in fx_path.read_text(encoding="utf-8").splitlines():
        m = re.match(
            r"(\d{4}-\d{2}-\d{2})\s+price\s+EUR\s+([\d.]+)\s+(\w+)", line
        )
        if m:
            rates[(m.group(1), m.group(3))] = float(m.group(2))
    return rates


def _append_fx_rate_to_file(date_str: str, currency: str, rate: float):
    """Append a new price directive to cupbots/fx.beancount, sorted by date."""
    fx_path = FINANCES_DIR / "cupbots" / "fx.beancount"
    new_line = f"{date_str} price EUR {rate:.10g} {currency}"

    if not fx_path.exists():
        fx_path.write_text(f"* Currency rate\n\n{new_line}\n", encoding="utf-8")
        return

    lines = fx_path.read_text(encoding="utf-8").splitlines()
    # Find insertion point: after header, sorted by date
    header_lines = []
    price_lines = []
    for line in lines:
        if re.match(r"\d{4}-\d{2}-\d{2}\s+price\s+", line):
            price_lines.append(line)
        else:
            if not price_lines:
                header_lines.append(line)
            else:
                # Trailing non-price lines, keep them
                price_lines.append(line)

    price_lines.append(new_line)
    # Sort price lines by date, then currency
    price_lines = sorted(
        price_lines,
        key=lambda l: (
            re.match(r"(\d{4}-\d{2}-\d{2}).*?(\w+)\s*$", l).group(1) if re.match(r"\d{4}", l) else "",
            re.match(r"(\d{4}-\d{2}-\d{2}).*?(\w+)\s*$", l).group(2) if re.match(r"\d{4}", l) else "",
        ),
    )

    fx_path.write_text(
        "\n".join(header_lines + price_lines) + "\n", encoding="utf-8"
    )
    log.info("Added FX rate: %s", new_line)


async def _fetch_fx_rate(date_str: str, currency: str) -> float | None:
    """Fetch exchange rate from OPERATING_CURRENCY to currency on date."""
    if currency == OPERATING_CURRENCY:
        return None

    base_lower = OPERATING_CURRENCY.lower()
    currency_lower = currency.lower()
    url = FX_API_TEMPLATE.format(date=date_str, currency=base_lower)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                rates = data.get(base_lower, {})
                return rates.get(currency_lower)
    except Exception as e:
        log.error("FX rate fetch failed for %s on %s: %s", currency, date_str, e)
    return None


def _read_journal_summary(ledger_type: str) -> str:
    """Read journal_summary.beancount for context."""
    paths = _get_ledger_paths(ledger_type)
    try:
        return paths["summary"].read_text(encoding="utf-8")
    except Exception:
        return ""


async def _ocr_and_parse_invoice(file_path: Path, ledger_type: str) -> dict | None:
    """
    Send invoice to Claude for OCR and beancount entry generation.
    Returns dict with keys: postings, commodities, file_path, file_name
    """
    summary = _read_journal_summary(ledger_type)

    system_prompt = f"""You are an accounting expert to record beancount journal entries.

Ensure each journal entry is valid, accurate, and always balances to pass audit.

If there are OCR results, note that some OCR may contain multiple receipts in one file. In this case:
- Detect and separate each receipt in the OCR text.
- For each receipt, extract the date, payee, total amount, currency, and any unique reference numbers (such as Invoice #, Receipt No., Reference No., Batch No., etc.).
- Create a separate journal entry for each receipt.
- If multiple receipts are for the same payee and date, and the business context requires them to be summed as a single transaction, you may sum their totals and reference all relevant receipt numbers in the metadata.
- If unsure, default to separate entries per receipt.

When instructed to create journal entries, check if user gave sample posting entries. Use the latest posting and adhere to the chart of account used.

On the narration, keep it short and don't include invoice/receipt ID here because it's in id metadata.

The operating currency is {OPERATING_CURRENCY}. If the posting currency differs from {OPERATING_CURRENCY}, include a commodity price entry for the conversion rate.

Reply ONLY with valid JSON (no markdown fences, no other text) in this format:
{{
  "postings": [
    "2023-10-26 * \\"Payee\\" \\"Narration\\"\\n  Expenses:Category  50.00 USD\\n  Assets:Cash:USD:Wise\\n  id: \\"invoice-id\\""
  ],
  "commodities": [
    "2023-10-26 price {OPERATING_CURRENCY} 1.05 USD"
  ],
  "file_path": "Expenses/Category/Subcategory",
  "file_name": "2023-10-26-payee-description.pdf"
}}

Important rules:
- file_path is the relative directory path where the invoice should be stored (follows beancount document convention matching the account)
- file_name should follow the pattern: YYYY-MM-DD-payee-description.ext
- commodities should be price directives from {OPERATING_CURRENCY} to the posting currency
- Each posting must include an id metadata field with the invoice/receipt ID
- Ensure postings balance correctly

Here is the current chart of accounts and example transactions:
{summary}"""

    prompt = (
        f"Read the file at {file_path} and process this invoice/receipt for the '{ledger_type}' ledger. "
        f"My base currency is {OPERATING_CURRENCY}."
    )

    try:
        result = await run_claude_cli(
            prompt,
            model="sonnet",
            system_prompt=system_prompt,
            tools="Read",
            max_turns=5,
            timeout=120,
        )
        parsed = _extract_json(result["text"])
        if parsed and isinstance(parsed, dict):
            return parsed
        log.error("Failed to parse invoice JSON: %s", result["text"][:500])
        return None

    except Exception as e:
        log.error("Invoice OCR failed for %s: %s", file_path.name, e)
        return None


async def _enrich_with_fx_rates(parsed: dict) -> dict:
    """Fetch real FX rates for any commodities that need them.

    Checks cupbots/fx.beancount first; only fetches from API if the
    (date, currency) pair is missing, and appends new rates to the file.
    """
    commodities = parsed.get("commodities", [])
    enriched_commodities = []

    existing_rates = _read_existing_fx_rates()

    for comm in commodities:
        # Parse: "2025-01-01 price EUR 1.05 USD"
        match = re.match(
            r"(\d{4}-\d{2}-\d{2})\s+price\s+(\w+)\s+[\d.]+\s+(\w+)", comm
        )
        if match:
            date_str, base, quote = match.group(1), match.group(2), match.group(3)

            # Check if rate already exists in fx.beancount
            cached = existing_rates.get((date_str, quote))
            if cached is not None:
                log.info("FX rate for %s on %s found in fx.beancount: %s", quote, date_str, cached)
                enriched_commodities.append(
                    f"{date_str} price {base} {cached:.10g} {quote}"
                )
                continue

            # Fetch from API
            rate = await _fetch_fx_rate(date_str, quote)
            if rate is not None:
                enriched_commodities.append(
                    f"{date_str} price {base} {rate:.10g} {quote}"
                )
                # Save to fx.beancount for future use
                _append_fx_rate_to_file(date_str, quote, rate)
                existing_rates[(date_str, quote)] = rate
                continue
        # Keep original if we couldn't fetch
        enriched_commodities.append(comm)

    parsed["commodities"] = enriched_commodities
    return parsed


def _ensure_accounts_exist(parsed: dict, ledger_type: str) -> list[str]:
    """Check postings for new accounts and add open directives if needed.

    Returns list of newly opened account names.
    """
    postings = parsed.get("postings", [])
    if not postings:
        return []

    # Extract account names from postings
    account_re = re.compile(r"^\s{2,}([A-Z][A-Za-z0-9\-:]+[A-Za-z0-9])\s", re.MULTILINE)
    posting_accounts = set()
    for entry_text in postings:
        for m in account_re.finditer(entry_text):
            posting_accounts.add(m.group(1))

    if not posting_accounts:
        return []

    # Read existing accounts from journal
    journal_path = _get_ledger_paths(ledger_type)["journal"]
    try:
        content = journal_path.read_text(encoding="utf-8")
    except Exception:
        return []

    open_re = re.compile(r"^\d{4}-\d{2}-\d{2}\s+open\s+(\S+)", re.MULTILINE)
    existing_accounts = {m.group(1) for m in open_re.finditer(content)}

    new_accounts = posting_accounts - existing_accounts
    if not new_accounts:
        return []

    # Add open directives for new accounts
    today = datetime.now().strftime("%Y-%m-%d")
    open_lines = "\n".join(
        f"{today} open {acc}" for acc in sorted(new_accounts)
    )

    # Insert open directives before the first transaction
    tx_re = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\*", re.MULTILINE)
    match = tx_re.search(content)
    if match:
        insert_pos = match.start()
        new_content = content[:insert_pos] + open_lines + "\n\n" + content[insert_pos:]
    else:
        new_content = content.rstrip() + "\n\n" + open_lines + "\n"

    journal_path.write_text(new_content, encoding="utf-8")
    log.info("Opened new accounts in %s: %s", ledger_type, ", ".join(sorted(new_accounts)))
    return sorted(new_accounts)


def _add_entries_to_beancount(parsed: dict, ledger_type: str) -> tuple[bool, str]:
    """Add entries to beancount journal. Returns (success, message)."""
    # Ensure any new accounts exist before adding entries
    new_accounts = _ensure_accounts_exist(parsed, ledger_type)

    # Import the existing add_beancount_entries module
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from add_beancount_entries import add_entries_to_beancount
        add_entries_to_beancount(parsed, ledger_type)
        msg = "Entries added successfully"
        if new_accounts:
            msg += f" (new accounts: {', '.join(new_accounts)})"
        return True, msg
    except Exception as e:
        return False, f"Error adding entries: {e}"
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))


def _find_duplicate(parsed: dict, ledger_type: str) -> str | None:
    """Check if parsed entry is a likely duplicate of an existing journal entry.

    Three tiers:
      1. Exact receipt/invoice ID match → definite duplicate
      2. Same amount within ±3 days of same payee → likely duplicate
      3. Same amount within ±3 days, different payee → suspicious, warn

    Parses the journal once into lightweight tuples. No LLM call.
    Returns a warning string if duplicate found, None otherwise.
    """
    journal_path = _get_ledger_paths(ledger_type)["journal"]
    try:
        content = journal_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # --- Extract new entry signals ---
    new_ids = set()
    new_entries = []  # list of (date_obj, amount_float, currency, payee_lower, narration_lower)
    for posting in parsed.get("postings", []):
        id_match = re.search(r'id:\s*"([^"]+)"', posting)
        if id_match:
            new_ids.add(id_match.group(1).strip().lower())
        header = re.match(r'(\d{4}-\d{2}-\d{2})\s+\*\s+"([^"]*)"\s+"([^"]*)"', posting)
        if not header:
            header = re.match(r'(\d{4}-\d{2}-\d{2})\s+\*\s+"([^"]*)"', posting)
        if header:
            try:
                date_obj = datetime.strptime(header.group(1), "%Y-%m-%d")
            except ValueError:
                continue
            payee = header.group(2).lower()
            narration = header.group(3).lower() if header.lastindex >= 3 else ""
            # Find first debit amount (positive number on expense/asset line)
            amt_match = re.search(r'(\d[\d,]*\.?\d*)\s+([A-Z]{3})', posting)
            if amt_match:
                try:
                    amount = float(amt_match.group(1).replace(",", ""))
                except ValueError:
                    continue
                currency = amt_match.group(2)
                new_entries.append((date_obj, amount, currency, payee, narration))

    if not new_ids and not new_entries:
        return None

    # --- Parse existing entries in one pass ---
    existing_ids: dict[str, str] = {}
    existing_entries = []  # same shape as new_entries + summary string

    tx_re = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+\*\s+"([^"]*)"(?:\s+"([^"]*)")?', re.MULTILINE)
    for match in tx_re.finditer(content):
        date_str, payee, narration = match.group(1), match.group(2), match.group(3) or ""
        start = match.start()
        next_tx = tx_re.search(content, match.end() + 1)
        block = content[start:next_tx.start() if next_tx else len(content)]
        summary = f"{date_str} \"{payee}\" \"{narration}\""

        id_match = re.search(r'id:\s*"([^"]+)"', block)
        if id_match:
            existing_ids[id_match.group(1).strip().lower()] = summary

        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        amt_match = re.search(r'(\d[\d,]*\.?\d*)\s+([A-Z]{3})', block.split('\n', 1)[-1])
        if amt_match:
            try:
                amount = float(amt_match.group(1).replace(",", ""))
            except ValueError:
                continue
            currency = amt_match.group(2)
            existing_entries.append((date_obj, amount, currency, payee.lower(), narration.lower(), summary))

    # --- Tier 1: exact ID match ---
    for nid in new_ids:
        if nid in existing_ids:
            return f"Duplicate ID '{nid}' already exists: {existing_ids[nid]}"

    # --- Tier 2 & 3: fuzzy amount + date window ---
    from datetime import timedelta
    for n_date, n_amt, n_cur, n_payee, n_narr in new_entries:
        for e_date, e_amt, e_cur, e_payee, e_narr, e_summary in existing_entries:
            if n_cur != e_cur:
                continue
            day_diff = abs((n_date - e_date).days)
            if day_diff > 3:
                continue
            # Amount must be within 1% or ±0.50 (rounding differences)
            if n_amt == 0:
                continue
            amt_diff = abs(n_amt - e_amt)
            if amt_diff > max(n_amt * 0.01, 0.50):
                continue

            # Same amount window — check payee similarity
            if n_payee == e_payee:
                return f"Likely duplicate: {e_summary} (same payee, amount, ±3 days)"

            # Different payee but same amount + date window — could be same vendor, different spelling
            # Check if payee or narration words overlap
            n_words = set(n_payee.split()) | set(n_narr.split())
            e_words = set(e_payee.split()) | set(e_narr.split())
            # Drop tiny filler words that cause false positives
            filler = {"", "sdn", "bhd", "the", "of", "and", "a", "at", "to", "for", "in", "on"}
            n_words -= filler
            e_words -= filler
            if n_words and e_words and n_words & e_words:
                return f"Suspicious match: {e_summary} (similar payee/narration, same amount, ±3 days)"

    return None


def _move_invoice(file_path: Path, parsed: dict, ledger_type: str) -> tuple[bool, str]:
    """Move invoice to the correct beancount document folder."""
    new_relative_dir = parsed.get("file_path")
    new_filename = parsed.get("file_name")

    if not new_relative_dir or not new_filename:
        return False, "No destination path in parsed data"

    dest_dir = FINANCES_DIR / ledger_type / new_relative_dir
    dest_path = dest_dir / new_filename

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        file_path.rename(dest_path)
        return True, f"Moved to {dest_path.relative_to(FINANCES_DIR)}"
    except OSError as e:
        return False, f"Failed to move: {e}"


def _run_bean_check(ledger_type: str) -> tuple[bool, str]:
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


def _run_bean_format(ledger_type: str) -> bool:
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


def _regenerate_summary(ledger_type: str) -> bool:
    """Regenerate journal_summary.beancount."""
    script = SCRIPTS_DIR / "generate_journal_summary.py"
    if not script.exists():
        return False

    try:
        result = subprocess.run(
            [sys.executable, str(script), ledger_type],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _ensure_file_destination(parsed: dict, attachment_path: Path):
    """Ensure parsed data has file_path and file_name when an attachment exists.

    If the LLM didn't return a destination, derive one from the posting date
    and expense account so the receipt is always stored.
    """
    if parsed.get("file_path") and parsed.get("file_name"):
        return

    # Try to extract date and account from the first posting
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder = "Expenses"
    narration = "receipt"

    for posting in parsed.get("postings", []):
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", posting)
        if date_match:
            date_str = date_match.group(1)
        # Extract expense account for folder: "Expenses:Food:Groceries" -> "Expenses/Food/Groceries"
        acct_match = re.search(r"(Expenses:\S+)", posting)
        if acct_match:
            folder = acct_match.group(1).replace(":", "/")
        # Extract narration for filename
        narr_match = re.search(r'\*\s+"[^"]*"\s+"([^"]*)"', posting)
        if narr_match:
            narration = re.sub(r"[^\w\s-]", "", narr_match.group(1)).strip().replace(" ", "-").lower()[:40]
        break

    ext = attachment_path.suffix or ".jpg"
    parsed["file_path"] = folder
    parsed["file_name"] = f"{date_str}-{narration}{ext}"
    log.info("Generated file destination: %s/%s", folder, parsed["file_name"])


async def _parse_expense_with_llm(
    description: str,
    ledger_type: str,
    attachment_path: Path | None = None,
    attachment_mime: str | None = None,
) -> dict | None:
    """Use LLM to parse a natural-language expense into beancount entries."""
    summary = _read_journal_summary(ledger_type)
    today = datetime.now().strftime("%Y-%m-%d")

    tax_relief_note = ""
    if ledger_type == "personal":
        tax_relief_note = """
IMPORTANT — Malaysian Tax Relief (LHDN):
For the personal ledger, if the expense qualifies for Malaysian tax relief, use the appropriate Expenses:Tax:* account instead of the regular expense account:
- Expenses:Tax:Parents — parents medical/dental/carer (RM8,000/yr)
- Expenses:Tax:Education — education fees for self (RM7,000/yr)
- Expenses:Tax:Medical — serious disease/fertility/vaccination/dental (RM10,000/yr)
- Expenses:Tax:MedicalCheckup — medical exam/covid test/mental health/monitoring (RM1,000/yr)
- Expenses:Tax:Lifestyle — books/computer/smartphone/internet/skill courses (RM2,500/yr)
- Expenses:Tax:Sports — sports equipment/facility/gym (RM1,000/yr)
- Expenses:Tax:Insurance — life insurance + EPF voluntary (RM7,000/yr)
- Expenses:Tax:PRS — deferred annuity / PRS (RM3,000/yr)
- Expenses:Tax:MedicalInsurance — education & medical insurance (RM4,000/yr)
- Expenses:Tax:SOCSO — SOCSO contributions (RM350/yr)
- Expenses:Tax:EV — EV charging / composting machine (RM2,500/yr)
- Expenses:Tax:HousingLoan — housing loan interest, first home (RM5,000-7,000/yr)
Only use these if the expense clearly qualifies. If unsure, use the regular expense account.
"""

    system_prompt = f"""You are an accounting expert. Parse the user's expense description into a beancount journal entry.

Today's date is {today}. Use today's date unless the user specifies otherwise.
The operating currency is {OPERATING_CURRENCY}.
The target ledger is '{ledger_type}'.

Rules:
- Pick the most appropriate expense account from the chart of accounts below
- Pick the most likely payment source (Assets account) based on the currency and context
- If a receipt/image is attached, extract details from it (amount, payee, date, receipt ID)
- Each posting must include an id metadata field (use receipt ID if available, otherwise generate a short one like "exp-YYYYMMDD-description")
- If the currency differs from {OPERATING_CURRENCY}, include a commodity price entry
- Keep narration short and descriptive
{tax_relief_note}

Reply ONLY with valid JSON (no markdown fences):
{{
  "postings": [
    "2025-01-15 * \\"Payee\\" \\"Narration\\"\\n  Expenses:Category  50.00 EUR\\n  Assets:Cash:EUR:Wise\\n  id: \\"exp-20250115-groceries\\""
  ],
  "commodities": [],
  "file_path": "Expenses/Category",
  "file_name": "2025-01-15-payee-description.ext"
}}

If no attachment, omit file_path and file_name (set to null).

Chart of accounts and example transactions:
{summary}"""

    model = "sonnet" if attachment_path else "haiku"
    if attachment_path:
        prompt_text = (
            f"Read the file at {attachment_path} and "
            + (f"add this expense to the '{ledger_type}' ledger: {description}" if description
               else f"process this receipt for the '{ledger_type}' ledger.")
        )
    else:
        prompt_text = f"Add this expense to the '{ledger_type}' ledger: {description}"

    try:
        result = await run_claude_cli(
            prompt_text,
            model=model,
            system_prompt=system_prompt,
            tools="Read",
            max_turns=5,
            timeout=120,
        )
        parsed = _extract_json(result["text"])
        if parsed and isinstance(parsed, dict):
            return parsed
        log.error("Failed to parse expense JSON: %s", result["text"][:500])
        return None

    except Exception as e:
        log.error("Expense LLM call failed: %s", e)
        return None


async def _parse_recording_with_llm(
    description: str,
    ledger_type: str,
    record_type: str,
    attachment_path: Path | None = None,
    attachment_mime: str | None = None,
) -> dict | None:
    """Generic LLM parser for recording commands (income, transfer, invoice, payment)."""
    summary = _read_journal_summary(ledger_type)
    today = datetime.now().strftime("%Y-%m-%d")

    type_prompts = {
        "income": f"""You are an accounting expert. Parse the user's income description into a beancount journal entry.

Today's date is {today}. The operating currency is {OPERATING_CURRENCY}. The target ledger is '{ledger_type}'.

Rules:
- This is INCOME. The credit side should be an Income:* account.
- The debit side should be an Assets:* account (Cash, Receivables, etc.)
- Pick accounts from the chart below
- Each posting must include an id metadata field
- If the currency differs from {OPERATING_CURRENCY}, include a commodity price entry
- Keep narration short

Reply ONLY with valid JSON (no markdown fences):
{{
  "postings": ["2025-01-15 * \\"Client\\" \\"Payment received\\"\\n  Assets:Cash:USD:Wise  5000.00 USD\\n  Income:Clients:Name\\n  id: \\"inv-20250115\\""],
  "commodities": [],
  "file_path": "Income/Clients/Name",
  "file_name": "2025-01-15-client-invoice.ext"
}}

If no attachment, set file_path and file_name to null.

Chart of accounts and example transactions:
{summary}""",

        "transfer": f"""You are an accounting expert. Parse the user's transfer description into a beancount journal entry.

Today's date is {today}. The operating currency is {OPERATING_CURRENCY}. The target ledger is '{ledger_type}'.

Rules:
- This is a TRANSFER between accounts (Assets to Assets, or Assets to Liabilities like credit card payments)
- NO Income or Expenses accounts should be involved
- Pick accounts from the chart below
- Each posting must include an id metadata field
- If currencies differ, include a commodity price entry
- Keep narration short

Reply ONLY with valid JSON (no markdown fences):
{{
  "postings": ["2025-01-15 * \\"Transfer\\" \\"Wise to Jar\\"\\n  Assets:Cash:Jar:USD:Wise  500.00 USD\\n  Assets:Cash:USD:Wise  -500.00 USD\\n  id: \\"xfr-20250115\\""],
  "commodities": []
}}

Chart of accounts:
{summary}""",

        "ar": f"""You are an accounting expert. Parse the user's invoice description into a beancount journal entry.

Today's date is {today}. The operating currency is {OPERATING_CURRENCY}. The target ledger is '{ledger_type}'.

Rules:
- This is an INVOICE SENT to a client (accounts receivable)
- Debit: Assets:Receivables:Clients (or sub-account matching the client)
- Credit: Income:Clients:<client> or Income:Software:<product>
- Pick accounts from the chart below, or suggest a new sub-account if needed
- Each posting must include an id metadata field with the invoice number
- If the currency differs from {OPERATING_CURRENCY}, include a commodity price entry

Reply ONLY with valid JSON (no markdown fences):
{{
  "postings": ["2025-01-15 * \\"Client Name\\" \\"January invoice\\"\\n  Assets:Receivables:Clients  5000.00 USD\\n  Income:Clients:Name\\n  id: \\"INV-2025-001\\""],
  "commodities": ["2025-01-15 price EUR 1.05 USD"],
  "file_path": "Income/Clients/Name",
  "file_name": "2025-01-15-client-invoice.ext"
}}

If no attachment, set file_path and file_name to null.

Chart of accounts:
{summary}""",

        "payment": f"""You are an accounting expert. Parse the user's payment received description into a beancount journal entry.

Today's date is {today}. The operating currency is {OPERATING_CURRENCY}. The target ledger is '{ledger_type}'.

Rules:
- This is a PAYMENT RECEIVED from a client (clearing accounts receivable)
- Debit: Assets:Cash:* (the bank/wallet where payment was received)
- Credit: Assets:Receivables:Clients (clearing the receivable)
- NO Income accounts — income was already recorded when the invoice was sent
- Pick accounts from the chart below
- Each posting must include an id metadata field
- If the currency differs from {OPERATING_CURRENCY}, include a commodity price entry

Reply ONLY with valid JSON (no markdown fences):
{{
  "postings": ["2025-01-20 * \\"Client Name\\" \\"Payment for INV-2025-001\\"\\n  Assets:Cash:USD:Wise  5000.00 USD\\n  Assets:Receivables:Clients  -5000.00 USD\\n  id: \\"pay-20250120-client\\""],
  "commodities": []
}}

Chart of accounts:
{summary}""",
    }

    system_prompt = type_prompts.get(record_type, type_prompts["income"])

    model = "sonnet" if attachment_path else "haiku"
    if attachment_path:
        prompt_text = (
            f"Read the file at {attachment_path} and "
            + (f"record this {record_type} for the '{ledger_type}' ledger: {description}" if description
               else f"process this document as {record_type} for the '{ledger_type}' ledger.")
        )
    else:
        prompt_text = f"Record this {record_type} for the '{ledger_type}' ledger: {description}"

    try:
        result = await run_claude_cli(
            prompt_text,
            model=model,
            system_prompt=system_prompt,
            tools="Read",
            max_turns=5,
            timeout=120,
        )
        parsed = _extract_json(result["text"])
        if parsed and isinstance(parsed, dict):
            return parsed
        log.error("Failed to parse %s JSON: %s", record_type, result["text"][:500])
        return None
    except Exception as e:
        log.error("%s LLM call failed: %s", record_type, e)
        return None


async def _handle_recording_cross_platform(msg, reply, record_type: str) -> None:
    """Shared cross-platform handler for expense/income/transfer/invoice/payment."""
    args = msg.args or []
    text = " ".join(args)

    # Determine ledger type
    ledger_type = "cupbots"
    if text.lower().startswith("personal "):
        ledger_type = "personal"
        text = text[len("personal "):].strip()
    elif text.lower() == "personal":
        ledger_type = "personal"
        text = ""

    has_attachment = bool(getattr(msg, "media_path", None))
    if not text and not has_attachment:
        examples = {
            "expense": f"/finance {record_type} [personal] <description>\n  /finance {record_type} 50 EUR groceries\n  /finance {record_type} personal 200 MYR electricity bill",
            "income": f"/finance {record_type} [personal] <description>\n  /finance {record_type} 5000 USD from Third-Idea",
            "transfer": f"/finance {record_type} [personal] <description>\n  /finance {record_type} 500 EUR from Wise to Jar",
            "ar": f"/finance {record_type} [personal] <client> <amount> <currency>\n  /finance {record_type} Third-Idea 5000 USD March dev",
            "payment": f"/finance {record_type} [personal] <client> <amount> <currency>\n  /finance {record_type} Third-Idea 5000 USD wire-ref-123",
        }
        await reply.reply_text(f"Usage: {examples.get(record_type, '')}")
        return

    await reply.send_typing()

    # Parse with LLM — pass attachment if available (receipt image, invoice PDF)
    attachment = Path(msg.media_path) if getattr(msg, "media_path", None) else None
    if record_type == "expense":
        parsed = await _parse_expense_with_llm(text, ledger_type, attachment_path=attachment)
    else:
        parsed = await _parse_recording_with_llm(text, ledger_type, record_type, attachment_path=attachment)

    if not parsed:
        await reply.reply_error(f"Failed to parse {record_type}. Try again with more details.")
        return

    # Enrich FX rates
    parsed = await _enrich_with_fx_rates(parsed)

    # Check for duplicates before recording
    dup_warning = _find_duplicate(parsed, ledger_type)
    if dup_warning:
        await reply.reply_error(f"Skipped — {dup_warning}")
        return

    # Show what will be recorded
    lines = [f"{record_type.title()} ({ledger_type})", ""]
    for posting in parsed.get("postings", []):
        lines.append(posting)
    if parsed.get("commodities"):
        lines.append("\nFX rates:")
        for comm in parsed["commodities"]:
            lines.append(f"  {comm}")

    await reply.reply_text("\n".join(lines))

    # Auto-approve (no interactive buttons in WhatsApp)
    success, msg_text = _add_entries_to_beancount(parsed, ledger_type)
    if not success:
        await reply.reply_error(f"Error adding entries: {msg_text}")
        return

    _run_bean_format(ledger_type)

    # Move receipt/invoice to beancount documents folder
    file_status = ""
    if attachment:
        _ensure_file_destination(parsed, attachment)
        moved, move_msg = _move_invoice(attachment, parsed, ledger_type)
        file_status = f" {move_msg}" if moved else f" Warning: {move_msg}"

    valid, check_msg = _run_bean_check(ledger_type)
    if valid:
        _regenerate_summary(ledger_type)
        await reply.reply_text(f"Recorded. bean-check passed.{file_status}")
    else:
        await send_long_text(reply, f"Entries added but bean-check errors:\n{check_msg}", "bean-check-errors.txt")


async def _handle_finance_scan(msg, reply) -> None:
    """Scan and auto-process unprocessed invoices."""
    args = msg.args or []

    # Determine which ledgers to scan
    ledger_types = []
    folder_filter = None
    if args and args[0] in ("cupbots", "personal"):
        ledger_types = [args[0]]
        if len(args) > 1 and args[1] in ("income", "expenses"):
            folder_filter = args[1]
    else:
        ledger_types = ["cupbots", "personal"]

    # Scan for unprocessed files
    all_files = []
    for lt in ledger_types:
        files = _scan_unprocessed(lt, folder_filter)
        for f in files:
            all_files.append({"ledger_type": lt, "file_path": f})

    if not all_files:
        await reply.reply_text("No unprocessed invoices found.")
        return

    file_list = "\n".join(
        f"  - {d['file_path'].name} ({d['ledger_type']})"
        for d in all_files
    )
    await reply.reply_text(f"Found {len(all_files)} unprocessed invoice(s):\n{file_list}\n\nProcessing...")

    await reply.send_typing()

    for item in all_files:
        file_path = item["file_path"]
        ledger_type = item["ledger_type"]

        await reply.reply_text(f"Processing {file_path.name} ({ledger_type})...")

        # 1. OCR and parse
        parsed = await _ocr_and_parse_invoice(file_path, ledger_type)
        if not parsed:
            await reply.reply_error(f"Failed to parse {file_path.name}")
            continue

        # 2. Duplicate check
        dup_warning = _find_duplicate(parsed, ledger_type)
        if dup_warning:
            await reply.reply_text(f"⏭ Skipped {file_path.name} — {dup_warning}")
            continue

        # 3. Enrich FX rates
        parsed = await _enrich_with_fx_rates(parsed)

        # 4. Show entries
        lines = [f"{file_path.name}", ""]
        for posting in parsed.get("postings", []):
            lines.append(posting)
        if parsed.get("commodities"):
            lines.append("\nFX rates:")
            for comm in parsed["commodities"]:
                lines.append(f"  {comm}")
        if parsed.get("file_path") and parsed.get("file_name"):
            lines.append(f"\nDest: {parsed['file_path']}/{parsed['file_name']}")

        await reply.reply_text("\n".join(lines))

        # 5. Auto-approve: add entries
        success, msg_text = _add_entries_to_beancount(parsed, ledger_type)
        if not success:
            await reply.reply_error(f"Error adding entries for {file_path.name}: {msg_text}")
            continue

        # 6. Format journal
        _run_bean_format(ledger_type)

        # 7. Validate
        valid, check_msg = _run_bean_check(ledger_type)
        if not valid:
            await send_long_text(reply, f"Entries added for {file_path.name} but bean-check errors:\n{check_msg}", "bean-check-errors.txt")
            continue

        # 8. Move invoice
        moved, move_msg = _move_invoice(file_path, parsed, ledger_type)
        status = f"Recorded {file_path.name}. bean-check passed."
        if moved:
            status += f" {move_msg}"
        else:
            status += f" Warning: {move_msg}"

        # 8. Regenerate summary
        _regenerate_summary(ledger_type)

        await reply.reply_text(status)


async def _delegate(sub: str, msg, reply, remaining_args: list) -> bool:
    """Delegate a /finance subcommand to another plugin by rewriting msg.command/args."""
    target_module, target_cmd = _DELEGATED_SUBCOMMANDS[sub]

    try:
        import importlib
        mod = importlib.import_module(f"plugins.{target_module}.{target_module}")
    except ImportError:
        await reply.reply_error(f"Plugin {target_module} not available.")
        return True

    handler = getattr(mod, "handle_command", None)
    if not handler:
        await reply.reply_error(f"Plugin {target_module} has no handler.")
        return True

    delegated_msg = copy(msg)
    delegated_msg.command = target_cmd
    delegated_msg.args = remaining_args
    return await handler(delegated_msg, reply)


async def handle_command(msg, reply) -> bool:
    """Hub command handler — routes /finance subcommands to appropriate plugins."""
    cmd = msg.command

    if cmd not in COMMANDS:
        return False

    args = msg.args or []
    if args and args[0] in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    if not is_admin(msg.platform, msg.sender_id) and not msg.sender_role:
        await reply.reply_text("Finance commands are restricted.")
        return True

    sub = args[0].lower() if args else None

    # Delegated subcommands (maintenance, audit, invoice)
    if sub and sub in _DELEGATED_SUBCOMMANDS:
        return await _delegate(sub, msg, reply, args[1:])

    # Native recording subcommands: /finance expense, /finance income, etc.
    if sub and sub in SUBCOMMANDS:
        msg.args = args[1:]
        await _handle_recording_cross_platform(msg, reply, sub)
        return True

    # Default: /finance [cupbots|personal] [income|expenses] — scan mode
    await _handle_finance_scan(msg, reply)
    return True
