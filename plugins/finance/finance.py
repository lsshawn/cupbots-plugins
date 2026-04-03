"""
Finance — Beancount journal recording

Commands (scoped to finance topic thread):
  /finance [cupbots|personal] [income|expenses]  — Scan & process unprocessed invoices
  /expense [personal] <desc>     — Add expense (attach receipt)
  /income [personal] <desc>      — Record income (attach invoice)
  /transfer [personal] <desc>    — Inter-account transfer
  /invoice [personal] <desc>     — Record invoice sent (AR entry)
  /payment [personal] <desc>     — Record payment received (clear AR)
"""

import asyncio
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cupbots.config import get_config, get_thread_id
from cupbots.topic_filter import topic_command
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli, _extract_json

log = get_logger("finance")

# Paths
FINANCES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "finances"
SCRIPTS_DIR = FINANCES_DIR / "scripts"

# FX API (same as generate_rates.py)
FX_API_TEMPLATE = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date}/v1/currencies/{currency}.json"
OPERATING_CURRENCY = "EUR"

# Processing queue: list of (ledger_type, file_path) pending user action
_queue: list[dict] = []
_processing = False


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


def _format_entry_for_telegram(parsed: dict, file_path: Path) -> str:
    """Format parsed invoice data for telegram display."""
    lines = [f"📄 *{file_path.name}*\n"]

    for posting in parsed.get("postings", []):
        lines.append(f"```\n{posting}\n```")

    if parsed.get("commodities"):
        lines.append("\n💱 FX rates:")
        for comm in parsed["commodities"]:
            lines.append(f"`{comm}`")

    if parsed.get("file_path") and parsed.get("file_name"):
        lines.append(f"\n📁 → `{parsed['file_path']}/{parsed['file_name']}`")

    return "\n".join(lines)


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


async def cmd_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main /finance command — scan and process invoices."""
    global _processing
    if not update.message:
        return

    args = context.args or []

    if args and args[0] in ("--help", "help"):
        await update.message.reply_text(__doc__.strip())
        return

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
        await update.message.reply_text("✅ No unprocessed invoices found.")
        return

    file_list = "\n".join(
        f"  • `{d['file_path'].name}` ({d['ledger_type']})"
        for d in all_files
    )

    # Store scan results and ask for confirmation before processing
    scan_id = f"finscan:{datetime.now().strftime('%H%M%S')}"
    context.bot_data[scan_id] = {
        "files": [{"ledger_type": d["ledger_type"], "file_path": str(d["file_path"])} for d in all_files],
        "chat_id": update.message.chat_id,
        "thread_id": update.message.message_thread_id,
    }

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Process", callback_data=f"finscan:run:{scan_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"finscan:cancel:{scan_id}"),
        ],
    ])
    await update.message.reply_text(
        f"📋 Found {len(all_files)} unprocessed invoice(s):\n{file_list}\n\n"
        "Process them?",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def _process_single_invoice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ledger_type: str,
    file_path: Path,
):
    """Process a single invoice: OCR → FX → ask user → add to journal."""
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text(
        f"🔍 Processing `{file_path.name}` ({ledger_type})...",
        parse_mode="Markdown",
    )

    # 1. OCR and parse
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    parsed = await _ocr_and_parse_invoice(file_path, ledger_type)
    if not parsed:
        await msg.edit_text(f"❌ Failed to parse `{file_path.name}`")
        return

    # 2. Enrich FX rates from real API
    parsed = await _enrich_with_fx_rates(parsed)

    # 3. Store in context for callback
    item_id = f"fin:{file_path.stem}"
    context.bot_data[item_id] = {
        "parsed": parsed,
        "ledger_type": ledger_type,
        "file_path": str(file_path),
    }

    # 4. Show to user for approval
    display = _format_entry_for_telegram(parsed, file_path)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"fin:approve:{item_id}"),
            InlineKeyboardButton("❌ Skip", callback_data=f"fin:skip:{item_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Retry", callback_data=f"fin:retry:{item_id}"),
        ],
    ])

    try:
        await msg.edit_text(display, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        # Markdown parsing can fail with special chars in beancount entries
        await msg.edit_text(display, reply_markup=kb)


async def _callback_finance_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /finance scan confirmation buttons."""
    global _processing
    query = update.callback_query
    await query.answer()
    data = query.data

    parts = data.split(":", 3)
    if len(parts) < 3:
        return

    action = parts[1]
    scan_id = parts[2]

    stored = context.bot_data.pop(scan_id, None)
    if not stored:
        await query.edit_message_text("⚠️ Session expired. Run /finance again.")
        return

    if action == "cancel":
        await query.edit_message_text("Cancelled.")
        return

    if action == "run":
        all_files = stored["files"]
        await query.edit_message_text(
            f"⏳ Processing {len(all_files)} invoice(s)..."
        )

        _processing = True
        for item in all_files:
            if not _processing:
                await context.bot.send_message(
                    chat_id=stored["chat_id"],
                    message_thread_id=stored.get("thread_id"),
                    text="⏹️ Processing stopped.",
                )
                return
            await _process_single_invoice(
                update, context, item["ledger_type"], Path(item["file_path"])
            )


async def _callback_finance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle finance approval/skip/retry buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if not data.startswith("fin:"):
        return

    parts = data.split(":", 2)
    if len(parts) < 3:
        return

    action = parts[1]
    item_id = parts[2]

    stored = context.bot_data.get(item_id)
    if not stored:
        await query.edit_message_text("⚠️ Session expired. Run /finance again.")
        return

    parsed = stored["parsed"]
    ledger_type = stored["ledger_type"]
    file_path = Path(stored["file_path"])

    if action == "skip":
        context.bot_data.pop(item_id, None)
        await query.edit_message_text(f"⏭️ Skipped `{file_path.name}`")
        return

    if action == "retry":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        await query.edit_message_text(
            f"🔄 Retrying `{file_path.name}`...",
        )
        # Re-process
        new_parsed = await _ocr_and_parse_invoice(file_path, ledger_type)
        if not new_parsed:
            await query.edit_message_text(f"❌ Retry failed for `{file_path.name}`")
            return

        new_parsed = await _enrich_with_fx_rates(new_parsed)
        stored["parsed"] = new_parsed

        display = _format_entry_for_telegram(new_parsed, file_path)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"fin:approve:{item_id}"),
                InlineKeyboardButton("❌ Skip", callback_data=f"fin:skip:{item_id}"),
            ],
            [
                InlineKeyboardButton("🔄 Retry", callback_data=f"fin:retry:{item_id}"),
            ],
        ])
        try:
            await query.edit_message_text(display, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await query.edit_message_text(display, reply_markup=kb)
        return

    if action == "approve":
        status_lines = [f"✅ Approved `{file_path.name}`\n"]

        # 1. Add entries to beancount
        success, msg_text = _add_entries_to_beancount(parsed, ledger_type)
        if success:
            status_lines.append(f"📝 {msg_text}")
        else:
            status_lines.append(f"❌ {msg_text}")
            await query.edit_message_text("\n".join(status_lines))
            return

        # 2. Format journal
        await asyncio.to_thread(_run_bean_format, ledger_type)

        # 3. Validate with bean-check
        valid, check_msg = await asyncio.to_thread(_run_bean_check, ledger_type)
        if valid:
            status_lines.append("✅ bean-check passed")
        else:
            status_lines.append(f"⚠️ bean-check errors:\n```\n{check_msg}\n```")
            # Don't move file if validation fails
            await query.edit_message_text("\n".join(status_lines), parse_mode="Markdown")
            return

        # 4. Move invoice to correct folder
        moved, move_msg = _move_invoice(file_path, parsed, ledger_type)
        if moved:
            status_lines.append(f"📁 {move_msg}")
        else:
            status_lines.append(f"⚠️ {move_msg}")

        # 5. Regenerate summary
        if await asyncio.to_thread(_regenerate_summary, ledger_type):
            status_lines.append("📊 Summary regenerated")

        # Cleanup
        context.bot_data.pop(item_id, None)

        try:
            await query.edit_message_text("\n".join(status_lines), parse_mode="Markdown")
        except Exception:
            await query.edit_message_text("\n".join(status_lines))


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


async def _download_attachment(msg, context) -> tuple[Path | None, str | None]:
    """Download photo or document from a Telegram message. Returns (path, media_type)."""
    if msg.photo:
        photo = msg.photo[-1]  # largest resolution
        file = await context.bot.get_file(photo.file_id)
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        await file.download_to_drive(tmp.name)
        return Path(tmp.name), "image/jpeg"

    if msg.document:
        doc = msg.document
        mime = doc.mime_type or ""
        # Only accept images and PDFs
        if not (mime.startswith("image/") or mime == "application/pdf"):
            return None, None
        ext = Path(doc.file_name).suffix if doc.file_name else ".bin"
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(tmp.name)
        return Path(tmp.name), mime

    return None, None


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


async def _handle_expense_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos/documents sent with /expense caption."""
    msg = update.message
    if not msg or not msg.caption:
        return
    caption = msg.caption.strip()
    if not caption.startswith("/expense"):
        return
    # Parse args from caption (strip /expense)
    context.args = caption.split()[1:]
    await cmd_expense(update, context)


async def cmd_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /expense command — add expense via natural language + optional attachment."""
    msg = update.message
    if not msg:
        return

    args = context.args or []
    text = " ".join(args)

    has_attachment = bool(msg.photo or msg.document)
    has_reply_attachment = bool(
        msg.reply_to_message and (msg.reply_to_message.photo or msg.reply_to_message.document)
    )
    if not text and not has_attachment and not has_reply_attachment:
        await msg.reply_text(
            "Usage: /expense [personal] <description>\n"
            "Examples:\n"
            "  /expense 50 EUR groceries\n"
            "  /expense personal 200 MYR electricity bill\n"
            "  Send a receipt photo with /expense as caption\n"
            "  Reply to a receipt image with /expense"
        )
        return

    # Determine ledger type
    ledger_type = "cupbots"
    if text.lower().startswith("personal "):
        ledger_type = "personal"
        text = text[len("personal "):].strip()
    elif text.lower() == "personal":
        ledger_type = "personal"
        text = ""

    # Download attachment: check current message first, then replied-to message
    attachment_path, attachment_mime = await _download_attachment(msg, context)
    if not attachment_path and msg.reply_to_message:
        attachment_path, attachment_mime = await _download_attachment(msg.reply_to_message, context)

    if not text and not attachment_path:
        await msg.reply_text("Please provide a description or attach a receipt.")
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    status_msg = await msg.reply_text(
        f"🧾 Processing expense ({ledger_type})...",
        disable_notification=True,
    )

    # Parse with LLM
    parsed = await _parse_expense_with_llm(text, ledger_type, attachment_path, attachment_mime)
    if not parsed:
        await status_msg.edit_text("❌ Failed to parse expense. Try again with more details.")
        # Clean up temp file
        if attachment_path:
            attachment_path.unlink(missing_ok=True)
        return

    # Ensure attachment has a destination path
    if attachment_path:
        _ensure_file_destination(parsed, attachment_path)

    # Enrich FX rates
    parsed = await _enrich_with_fx_rates(parsed)

    # Store in context for callback
    item_id = f"exp:{datetime.now().strftime('%H%M%S')}"
    context.bot_data[item_id] = {
        "parsed": parsed,
        "ledger_type": ledger_type,
        "attachment_path": str(attachment_path) if attachment_path else None,
        "attachment_mime": attachment_mime,
    }

    # Format display
    lines = [f"🧾 *Expense ({ledger_type})*\n"]
    for posting in parsed.get("postings", []):
        lines.append(f"```\n{posting}\n```")
    if parsed.get("commodities"):
        lines.append("\n💱 FX rates:")
        for comm in parsed["commodities"]:
            lines.append(f"`{comm}`")
    if attachment_path and parsed.get("file_path") and parsed.get("file_name"):
        lines.append(f"\n📎 Receipt → `{parsed['file_path']}/{parsed['file_name']}`")

    display = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"exp:approve:{item_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"exp:skip:{item_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Retry", callback_data=f"exp:retry:{item_id}"),
        ],
    ])

    try:
        await status_msg.edit_text(display, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await status_msg.edit_text(display, reply_markup=kb)


async def _callback_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle expense approval/cancel/retry buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if not data.startswith("exp:"):
        return

    parts = data.split(":", 2)
    if len(parts) < 3:
        return

    action = parts[1]
    item_id = parts[2]

    stored = context.bot_data.get(item_id)
    if not stored:
        await query.edit_message_text("⚠️ Session expired. Run /expense again.")
        return

    parsed = stored["parsed"]
    ledger_type = stored["ledger_type"]
    attachment_path = Path(stored["attachment_path"]) if stored.get("attachment_path") else None
    attachment_mime = stored.get("attachment_mime")

    if action == "skip":
        context.bot_data.pop(item_id, None)
        if attachment_path:
            attachment_path.unlink(missing_ok=True)
        await query.edit_message_text("❌ Expense cancelled.")
        return

    if action == "retry":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        await query.edit_message_text("🔄 Retrying...")
        # Re-extract description from the original postings narration
        desc = ""
        for p in parsed.get("postings", []):
            match = re.search(r'\*\s+"[^"]*"\s+"([^"]*)"', p)
            if match:
                desc = match.group(1)
                break

        new_parsed = await _parse_expense_with_llm(desc, ledger_type, attachment_path, attachment_mime)
        if not new_parsed:
            await query.edit_message_text("❌ Retry failed.")
            return

        new_parsed = await _enrich_with_fx_rates(new_parsed)
        stored["parsed"] = new_parsed

        lines = [f"🧾 *Expense ({ledger_type})*\n"]
        for posting in new_parsed.get("postings", []):
            lines.append(f"```\n{posting}\n```")
        if new_parsed.get("commodities"):
            lines.append("\n💱 FX rates:")
            for comm in new_parsed["commodities"]:
                lines.append(f"`{comm}`")
        if attachment_path and new_parsed.get("file_path") and new_parsed.get("file_name"):
            lines.append(f"\n📎 Receipt → `{new_parsed['file_path']}/{new_parsed['file_name']}`")

        display = "\n".join(lines)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"exp:approve:{item_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"exp:skip:{item_id}"),
            ],
            [
                InlineKeyboardButton("🔄 Retry", callback_data=f"exp:retry:{item_id}"),
            ],
        ])
        try:
            await query.edit_message_text(display, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await query.edit_message_text(display, reply_markup=kb)
        return

    if action == "approve":
        status_lines = ["✅ Expense approved\n"]

        # 1. Add entries to beancount
        success, msg_text = _add_entries_to_beancount(parsed, ledger_type)
        if success:
            status_lines.append(f"📝 {msg_text}")
        else:
            status_lines.append(f"❌ {msg_text}")
            await query.edit_message_text("\n".join(status_lines))
            return

        # 2. Format journal
        await asyncio.to_thread(_run_bean_format, ledger_type)

        # 3. Validate
        valid, check_msg = await asyncio.to_thread(_run_bean_check, ledger_type)
        if valid:
            status_lines.append("✅ bean-check passed")
        else:
            status_lines.append(f"⚠️ bean-check errors:\n```\n{check_msg}\n```")
            await query.edit_message_text("\n".join(status_lines), parse_mode="Markdown")
            return

        # 4. Move attachment if present
        if attachment_path and attachment_path.exists() and parsed.get("file_path") and parsed.get("file_name"):
            dest_dir = FINANCES_DIR / ledger_type / parsed["file_path"]
            dest_path = dest_dir / parsed["file_name"]
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.move(str(attachment_path), str(dest_path))
                status_lines.append(f"📎 Receipt saved to `{dest_path.relative_to(FINANCES_DIR)}`")
            except Exception as e:
                status_lines.append(f"⚠️ Failed to save receipt: {e}")
        elif attachment_path:
            attachment_path.unlink(missing_ok=True)

        # 5. Regenerate summary
        if await asyncio.to_thread(_regenerate_summary, ledger_type):
            status_lines.append("📊 Summary regenerated")

        context.bot_data.pop(item_id, None)

        try:
            await query.edit_message_text("\n".join(status_lines), parse_mode="Markdown")
        except Exception:
            await query.edit_message_text("\n".join(status_lines))


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

        "invoice": f"""You are an accounting expert. Parse the user's invoice description into a beancount journal entry.

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


async def _handle_recording_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    record_type: str,
    callback_prefix: str,
):
    """Generic handler for income/transfer/invoice/payment commands."""
    msg = update.message
    if not msg:
        return

    args = context.args or []
    text = " ".join(args)

    # Determine ledger type
    ledger_type = "cupbots"
    if text.lower().startswith("personal "):
        ledger_type = "personal"
        text = text[len("personal "):].strip()
    elif text.lower() == "personal":
        ledger_type = "personal"
        text = ""

    # Check for attachments (current message or replied-to)
    attachment_path, attachment_mime = await _download_attachment(msg, context)
    if not attachment_path and msg.reply_to_message:
        attachment_path, attachment_mime = await _download_attachment(msg.reply_to_message, context)

    has_attachment = bool(msg.photo or msg.document)
    has_reply_attachment = bool(
        msg.reply_to_message and (msg.reply_to_message.photo or msg.reply_to_message.document)
    )

    if not text and not attachment_path:
        examples = {
            "income": f"/{record_type} [personal] <description>\n  /{record_type} 5000 USD from Third-Idea",
            "transfer": f"/{record_type} [personal] <description>\n  /{record_type} 500 EUR from Wise to Jar",
            "invoice": f"/{record_type} [personal] <client> <amount> <currency>\n  /{record_type} Third-Idea 5000 USD March dev",
            "payment": f"/{record_type} [personal] <client> <amount> <currency>\n  /{record_type} Third-Idea 5000 USD wire-ref-123",
        }
        await msg.reply_text(f"Usage: {examples.get(record_type, '')}")
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    status_msg = await msg.reply_text(
        f"Processing {record_type} ({ledger_type})...",
        disable_notification=True,
    )

    parsed = await _parse_recording_with_llm(text, ledger_type, record_type, attachment_path, attachment_mime)
    if not parsed:
        await status_msg.edit_text(f"Failed to parse {record_type}. Try again with more details.")
        if attachment_path:
            attachment_path.unlink(missing_ok=True)
        return

    # Ensure attachment has destination
    if attachment_path:
        _ensure_file_destination(parsed, attachment_path)

    # Enrich FX rates
    parsed = await _enrich_with_fx_rates(parsed)

    # Store for callback
    item_id = f"{callback_prefix}:{datetime.now().strftime('%H%M%S')}"
    context.bot_data[item_id] = {
        "parsed": parsed,
        "ledger_type": ledger_type,
        "attachment_path": str(attachment_path) if attachment_path else None,
        "attachment_mime": attachment_mime,
        "record_type": record_type,
    }

    # Display for approval
    lines = [f"*{record_type.title()} ({ledger_type})*\n"]
    for posting in parsed.get("postings", []):
        lines.append(f"```\n{posting}\n```")
    if parsed.get("commodities"):
        lines.append("\nFX rates:")
        for comm in parsed["commodities"]:
            lines.append(f"`{comm}`")
    if attachment_path and parsed.get("file_path") and parsed.get("file_name"):
        lines.append(f"\nReceipt: `{parsed['file_path']}/{parsed['file_name']}`")

    display = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"{callback_prefix}:approve:{item_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"{callback_prefix}:skip:{item_id}"),
        ],
        [
            InlineKeyboardButton("Retry", callback_data=f"{callback_prefix}:retry:{item_id}"),
        ],
    ])

    try:
        await status_msg.edit_text(display, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await status_msg.edit_text(display, reply_markup=kb)


async def _callback_recording(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic callback for income/transfer/invoice/payment approvals."""
    query = update.callback_query
    await query.answer()
    data = query.data

    # Parse: prefix:action:item_id
    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    action, item_id = parts[1], parts[2]

    stored = context.bot_data.get(item_id)
    if not stored:
        await query.edit_message_text("Session expired.")
        return

    parsed = stored["parsed"]
    ledger_type = stored["ledger_type"]
    attachment_path = Path(stored["attachment_path"]) if stored.get("attachment_path") else None
    record_type = stored.get("record_type", "income")

    if action == "skip":
        context.bot_data.pop(item_id, None)
        if attachment_path:
            attachment_path.unlink(missing_ok=True)
        await query.edit_message_text("Cancelled.")
        return

    if action == "retry":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        await query.edit_message_text("Retrying...")

        desc = ""
        for p in parsed.get("postings", []):
            match = re.search(r'\*\s+"[^"]*"\s+"([^"]*)"', p)
            if match:
                desc = match.group(1)
                break

        new_parsed = await _parse_recording_with_llm(
            desc, ledger_type, record_type, attachment_path, stored.get("attachment_mime")
        )
        if not new_parsed:
            await query.edit_message_text("Retry failed.")
            return

        if attachment_path:
            _ensure_file_destination(new_parsed, attachment_path)
        new_parsed = await _enrich_with_fx_rates(new_parsed)
        stored["parsed"] = new_parsed

        lines = [f"*{record_type.title()} ({ledger_type})*\n"]
        for posting in new_parsed.get("postings", []):
            lines.append(f"```\n{posting}\n```")
        if new_parsed.get("commodities"):
            lines.append("\nFX rates:")
            for comm in new_parsed["commodities"]:
                lines.append(f"`{comm}`")

        display = "\n".join(lines)
        prefix = item_id.split(":")[0]
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=f"{prefix}:approve:{item_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"{prefix}:skip:{item_id}"),
            ],
            [
                InlineKeyboardButton("Retry", callback_data=f"{prefix}:retry:{item_id}"),
            ],
        ])
        try:
            await query.edit_message_text(display, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await query.edit_message_text(display, reply_markup=kb)
        return

    if action == "approve":
        status_lines = [f"{record_type.title()} approved\n"]

        success, msg_text = _add_entries_to_beancount(parsed, ledger_type)
        if success:
            status_lines.append(f"Entries added")
        else:
            status_lines.append(f"Error: {msg_text}")
            await query.edit_message_text("\n".join(status_lines))
            return

        await asyncio.to_thread(_run_bean_format, ledger_type)

        valid, check_msg = await asyncio.to_thread(_run_bean_check, ledger_type)
        if valid:
            status_lines.append("bean-check passed")
        else:
            status_lines.append(f"bean-check errors:\n```\n{check_msg}\n```")
            await query.edit_message_text("\n".join(status_lines), parse_mode="Markdown")
            return

        # Move attachment if present
        if attachment_path and attachment_path.exists() and parsed.get("file_path") and parsed.get("file_name"):
            dest_dir = FINANCES_DIR / ledger_type / parsed["file_path"]
            dest_path = dest_dir / parsed["file_name"]
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.move(str(attachment_path), str(dest_path))
                status_lines.append(f"Receipt saved to `{dest_path.relative_to(FINANCES_DIR)}`")
            except Exception as e:
                status_lines.append(f"Failed to save receipt: {e}")
        elif attachment_path:
            attachment_path.unlink(missing_ok=True)

        if await asyncio.to_thread(_regenerate_summary, ledger_type):
            status_lines.append("Summary regenerated")

        context.bot_data.pop(item_id, None)

        try:
            await query.edit_message_text("\n".join(status_lines), parse_mode="Markdown")
        except Exception:
            await query.edit_message_text("\n".join(status_lines))


async def cmd_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record income."""
    await _handle_recording_command(update, context, "income", "inc")


async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record inter-account transfer."""
    await _handle_recording_command(update, context, "transfer", "xfr")


async def cmd_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record invoice sent to client."""
    await _handle_recording_command(update, context, "invoice", "inv")


async def cmd_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record payment received from client."""
    await _handle_recording_command(update, context, "payment", "pay")


async def _handle_income_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos/documents with /income caption."""
    msg = update.message
    if not msg or not msg.caption:
        return
    caption = msg.caption.strip()
    if not caption.startswith("/income"):
        return
    context.args = caption.split()[1:]
    await cmd_income(update, context)


async def _handle_recording_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos/documents with /transfer, /invoice, /payment captions."""
    msg = update.message
    if not msg or not msg.caption:
        return
    caption = msg.caption.strip()
    parts = caption.split()
    cmd = parts[0].lower().lstrip("/")
    context.args = parts[1:]

    cmd_map = {
        "transfer": (cmd_transfer, "transfer", "xfr"),
        "invoice": (cmd_invoice, "invoice", "inv"),
        "payment": (cmd_payment, "payment", "pay"),
    }
    if cmd in cmd_map:
        handler, _, _ = cmd_map[cmd]
        await handler(update, context)


# --- Finance caption intent detection ---

FINANCE_COMMANDS = {
    "expense": {"keywords": ["expense", "spent", "paid", "bought", "purchase", "cost", "receipt"], "cmd": "expense"},
    "income": {"keywords": ["income", "received", "earned", "salary", "revenue", "payment received"], "cmd": "income"},
    "transfer": {"keywords": ["transfer", "moved", "convert", "xfr"], "cmd": "transfer"},
    "invoice": {"keywords": ["invoice", "billed", "invoiced", "inv"], "cmd": "invoice"},
    "payment": {"keywords": ["payment", "pay", "settled", "cleared"], "cmd": "payment"},
    "finance": {"keywords": ["finance", "scan", "process"], "cmd": "finance"},
}


def _detect_finance_intent(caption: str) -> tuple[str | None, str]:
    """Detect intended finance command from a mistyped caption.

    Returns (suggested_command, remaining_args) or (None, "") if no match.
    """
    parts = caption.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None, ""

    typed_cmd = parts[0].lower().lstrip("/")
    rest_parts = parts[1:]

    # Check if the typed command itself is a known finance command
    known_cmds = {"finance", "expense", "income", "transfer", "invoice", "payment"}
    if typed_cmd in known_cmds:
        return None, ""  # Already a valid command, not our problem

    # Combine typed command and rest into a single string for keyword matching
    full_text = " ".join([typed_cmd] + rest_parts).lower()

    # Parse out ledger type for the suggestion
    ledger = ""
    if "personal" in full_text:
        ledger = "personal "

    # Score each command by keyword matches
    best_cmd = None
    best_score = 0
    for cmd_name, info in FINANCE_COMMANDS.items():
        score = sum(1 for kw in info["keywords"] if kw in full_text)
        if score > best_score:
            best_score = score
            best_cmd = info["cmd"]

    if not best_cmd:
        return None, ""

    # Build the remaining args (strip out command-like words and 'personal')
    remaining = [p for p in rest_parts if p.lower() not in known_cmds and p.lower() != "personal"]
    args_str = ledger + " ".join(remaining)
    return best_cmd, args_str.strip()


async def _handle_finance_caption_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all for attachment captions that look finance-related but didn't match a known command."""
    msg = update.message
    if not msg or not msg.caption:
        return
    caption = msg.caption.strip()

    suggested_cmd, args = _detect_finance_intent(caption)
    if not suggested_cmd:
        return

    # Build the corrected command
    corrected = f"/{suggested_cmd} {args}".strip()

    # Store for callback
    item_id = f"ffix:{datetime.now().strftime('%H%M%S')}"
    context.bot_data[item_id] = {
        "corrected_cmd": suggested_cmd,
        "corrected_args": args,
        "message_id": msg.message_id,
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Yes, run {corrected}", callback_data=f"ffix:run:{item_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"ffix:cancel:{item_id}"),
    ]])

    await msg.reply_text(
        f"Did you mean `{corrected}`?",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def _callback_finance_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confirmation callback for corrected finance commands."""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "ffix:run:ffix:123456" or "ffix:cancel:ffix:123456"
    parts = data.split(":", 3)
    action = parts[1]
    item_id = ":".join(parts[2:])

    stored = context.bot_data.pop(item_id, None)
    if not stored:
        await query.edit_message_text("Session expired.")
        return

    if action == "cancel":
        await query.edit_message_text("Cancelled.")
        return

    if action == "run":
        cmd = stored["corrected_cmd"]
        args = stored["corrected_args"]

        # Re-dispatch to the correct handler using the original message
        original_msg = query.message.reply_to_message
        if not original_msg:
            await query.edit_message_text("Could not find original message.")
            return

        # Set up context.args
        context.args = args.split() if args else []

        # Create a fake update with the original message
        update._effective_message = original_msg

        cmd_handlers = {
            "expense": cmd_expense,
            "income": cmd_income,
            "transfer": cmd_transfer,
            "invoice": cmd_invoice,
            "payment": cmd_payment,
        }

        handler = cmd_handlers.get(cmd)
        if handler:
            await query.edit_message_text(f"Running /{cmd} {args}...")
            # Use the original message for the handler
            from telegram import Update as TgUpdate
            fake_update = TgUpdate(
                update_id=update.update_id,
                message=original_msg,
            )
            await handler(fake_update, context)
        else:
            await query.edit_message_text(f"Unknown command: /{cmd}")


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

    if not text:
        examples = {
            "expense": f"/{record_type} [personal] <description>\n  /{record_type} 50 EUR groceries\n  /{record_type} personal 200 MYR electricity bill",
            "income": f"/{record_type} [personal] <description>\n  /{record_type} 5000 USD from Third-Idea",
            "transfer": f"/{record_type} [personal] <description>\n  /{record_type} 500 EUR from Wise to Jar",
            "invoice": f"/{record_type} [personal] <client> <amount> <currency>\n  /{record_type} Third-Idea 5000 USD March dev",
            "payment": f"/{record_type} [personal] <client> <amount> <currency>\n  /{record_type} Third-Idea 5000 USD wire-ref-123",
        }
        await reply.reply_text(f"Usage: {examples.get(record_type, '')}")
        return

    # Parse with LLM (no attachment support for WhatsApp -- text only)
    if record_type == "expense":
        parsed = await _parse_expense_with_llm(text, ledger_type)
    else:
        parsed = await _parse_recording_with_llm(text, ledger_type, record_type)

    if not parsed:
        await reply.reply_error(f"Failed to parse {record_type}. Try again with more details.")
        return

    # Enrich FX rates
    parsed = await _enrich_with_fx_rates(parsed)

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

    valid, check_msg = _run_bean_check(ledger_type)
    if valid:
        _regenerate_summary(ledger_type)
        await reply.reply_text(f"Recorded. bean-check passed.")
    else:
        await reply.reply_error(f"Entries added but bean-check errors:\n{check_msg[:1000]}")


async def handle_command(msg, reply) -> bool:
    """Cross-platform command handler for finance recording."""
    cmd = msg.command
    args = msg.args or []

    if cmd == "expense":
        await _handle_recording_cross_platform(msg, reply, "expense")
        return True

    if cmd == "income":
        await _handle_recording_cross_platform(msg, reply, "income")
        return True

    if cmd == "transfer":
        await _handle_recording_cross_platform(msg, reply, "transfer")
        return True

    if cmd == "invoice":
        await _handle_recording_cross_platform(msg, reply, "invoice")
        return True

    if cmd == "payment":
        await _handle_recording_cross_platform(msg, reply, "payment")
        return True

    # Skip /finance for WhatsApp -- too interactive (scan + approval buttons)

    return False


def register(app: Application):
    """Register finance commands."""
    thread_id = get_thread_id("finance")
    # If thread_id is 0 or None, command works in any thread
    tid = thread_id if thread_id else None

    # Existing recording
    app.add_handler(topic_command("finance", cmd_finance, thread_id=tid))
    app.add_handler(topic_command("expense", cmd_expense, thread_id=tid))

    # New recording commands
    app.add_handler(topic_command("income", cmd_income, thread_id=tid))
    app.add_handler(topic_command("transfer", cmd_transfer, thread_id=tid))
    app.add_handler(topic_command("invoice", cmd_invoice, thread_id=tid))
    app.add_handler(topic_command("payment", cmd_payment, thread_id=tid))

    # Attachment handlers (Telegram doesn't treat captions as commands)
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.CaptionRegex(r"^/expense"),
        _handle_expense_attachment,
    ))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.CaptionRegex(r"^/income"),
        _handle_income_attachment,
    ))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.CaptionRegex(r"^/(transfer|invoice|payment)"),
        _handle_recording_attachment,
    ))

    # Catch-all: attachment with finance-ish caption that didn't match above
    finance_words = "|".join(["financ", "expens", "income", "transfer", "invoice", "payment",
                              "receipt", "paid", "spent", "bought"])
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.CaptionRegex(rf"(?i)({finance_words})"),
        _handle_finance_caption_fallback,
    ))

    # Callbacks
    app.add_handler(CallbackQueryHandler(_callback_finance_scan, pattern=r"^finscan:"))
    app.add_handler(CallbackQueryHandler(_callback_finance, pattern=r"^fin:"))
    app.add_handler(CallbackQueryHandler(_callback_expense, pattern=r"^exp:"))
    app.add_handler(CallbackQueryHandler(_callback_recording, pattern=r"^inc:"))
    app.add_handler(CallbackQueryHandler(_callback_recording, pattern=r"^xfr:"))
    app.add_handler(CallbackQueryHandler(_callback_recording, pattern=r"^inv:"))
    app.add_handler(CallbackQueryHandler(_callback_recording, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(_callback_finance_fix, pattern=r"^ffix:"))

    log.info("Finance plugin loaded (thread: %s)", tid or "any")
