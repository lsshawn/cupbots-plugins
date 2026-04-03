"""
Finance Maintenance — Journal maintenance tools

Commands (scoped to finance topic thread):
  /validate [personal]          — Run bean-check
  /fxsync                       — Fetch missing FX rates
  /void [personal] <search>     — Void an entry (reversing entry)
  /edit [personal] <search>     — Edit entry with LLM assistance
  /summary [personal]           — Regenerate journal summary
"""

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from cupbots.topic_filter import topic_command
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli, _extract_json
from plugins._finance_helpers import (
    FINANCES_DIR,
    OPERATING_CURRENCY,
    SCRIPTS_DIR,
    get_finance_thread_id,
    load_beancount,
    parse_ledger_and_args,
    run_bean_check,
    run_bean_format,
    regenerate_summary,
    send_long_text,
    _get_ledger_paths,
)

log = get_logger("finance.maint")

# FX API (same as generate_rates.py)
FX_API_TEMPLATE = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date}/v1/currencies/{currency}.json"


async def cmd_validate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run bean-check on the journal."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, _ = parse_ledger_and_args(args)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    valid, msg = await asyncio.to_thread(run_bean_check, ledger_type)
    if valid:
        await update.message.reply_text(f"bean-check ({ledger_type}): PASS")
    else:
        await send_long_text(update, context, f"bean-check ({ledger_type}): FAIL\n\n{msg}", "errors.txt")


async def cmd_fxsync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch missing FX rates from journal transactions."""
    if not update.message:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    status_msg = await update.message.reply_text("Scanning for missing FX rates...")

    try:
        from beancount.core import data as bc_data

        # Scan both ledgers for needed rates
        needed = set()
        for lt in ["cupbots", "personal"]:
            entries, _, _ = load_beancount(lt)
            for entry in entries:
                if isinstance(entry, bc_data.Transaction):
                    for posting in entry.postings:
                        if posting.units.currency != OPERATING_CURRENCY:
                            needed.add((entry.date, posting.units.currency))

        # Read existing rates
        fx_path = FINANCES_DIR / "cupbots" / "fx.beancount"
        existing = set()
        if fx_path.exists():
            for line in fx_path.read_text(encoding="utf-8").splitlines():
                m = re.match(r"(\d{4}-\d{2}-\d{2})\s+price\s+EUR\s+[\d.]+\s+(\w+)", line)
                if m:
                    from datetime import date as date_cls
                    existing.add((date_cls.fromisoformat(m.group(1)), m.group(2)))

        to_fetch = sorted(needed - existing)

        if not to_fetch:
            await status_msg.edit_text(f"All {len(needed)} FX rates already present.")
            return

        await status_msg.edit_text(f"Fetching {len(to_fetch)} missing rates (have {len(existing)})...")

        fetched = 0
        failed = 0
        new_lines = []

        async with httpx.AsyncClient(timeout=10) as client:
            for dt, currency in to_fetch:
                date_str = dt.isoformat()
                base_lower = OPERATING_CURRENCY.lower()
                url = FX_API_TEMPLATE.format(date=date_str, currency=base_lower)
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        data = r.json()
                        rate = data.get(base_lower, {}).get(currency.lower())
                        if rate is not None:
                            new_lines.append(f"{date_str} price EUR {rate:.10g} {currency}")
                            fetched += 1
                            continue
                except Exception as e:
                    log.error("FX fetch %s/%s: %s", date_str, currency, e)
                failed += 1

        # Append to fx.beancount
        if new_lines:
            # Read, merge, sort, write
            lines = fx_path.read_text(encoding="utf-8").splitlines() if fx_path.exists() else ["* Currency rate", ""]
            header = []
            prices = []
            for line in lines:
                if re.match(r"\d{4}-\d{2}-\d{2}\s+price\s+", line):
                    prices.append(line)
                elif not prices:
                    header.append(line)

            prices.extend(new_lines)
            prices = sorted(set(prices))
            fx_path.write_text("\n".join(header + prices) + "\n", encoding="utf-8")

        await status_msg.edit_text(
            f"FX sync complete: {fetched} fetched, {failed} failed, {len(existing)} existing"
        )
    except Exception as e:
        log.error("FX sync failed: %s", e)
        await status_msg.edit_text(f"Error: {e}")


async def cmd_void(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Void a transaction by creating a reversing entry."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, rest = parse_ledger_and_args(args)
    search_term = " ".join(rest).strip()

    if not search_term:
        await update.message.reply_text("Usage: /void [personal] <search-term>")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Find the entry
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from extract_top_postings import extract_latest_top_postings
        journal_path = FINANCES_DIR / ledger_type / "journal.beancount"
        journal_text = journal_path.read_text(encoding="utf-8")
        results = extract_latest_top_postings(journal_text, search_term, top_n=1)

        if not results:
            await update.message.reply_text(f"No entry found for '{search_term}'.")
            return

        entry_text = results[0].strip()
        item_id = f"vod:{datetime.now().strftime('%H%M%S')}"
        context.bot_data[item_id] = {
            "entry_text": entry_text,
            "ledger_type": ledger_type,
        }

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Void this entry", callback_data=f"vod:approve:{item_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"vod:skip:{item_id}"),
            ],
        ])

        await update.message.reply_text(
            f"Found entry to void:\n```\n{entry_text}\n```",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error("Void search failed: %s", e)
        await update.message.reply_text(f"Error: {e}")
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))


async def _callback_void(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle void approval."""
    query = update.callback_query
    await query.answer()
    data = query.data

    parts = data.split(":", 2)
    if len(parts) < 3:
        return

    action, item_id = parts[1], parts[2]
    stored = context.bot_data.get(item_id)
    if not stored:
        await query.edit_message_text("Session expired.")
        return

    if action == "skip":
        context.bot_data.pop(item_id, None)
        await query.edit_message_text("Cancelled.")
        return

    if action == "approve":
        entry_text = stored["entry_text"]
        ledger_type = stored["ledger_type"]

        # Generate reversing entry by negating amounts
        lines = entry_text.split("\n")
        reversed_lines = []
        for line in lines:
            # Match the header line: date * "payee" "narration"
            header_match = re.match(r'(\d{4}-\d{2}-\d{2})\s+\*\s+(".*?")\s+(".*?")', line)
            if header_match:
                today = datetime.now().strftime("%Y-%m-%d")
                reversed_lines.append(f'{today} * {header_match.group(2)} "VOID: {header_match.group(3)[1:-1]}"')
                continue
            # Match posting lines with amounts: account  amount currency
            posting_match = re.match(r'(\s+)([\w:]+)\s+(-?[\d,.]+)\s+(\w+)(.*)', line)
            if posting_match:
                indent, account, amount_str, currency, rest = posting_match.groups()
                try:
                    amount = float(amount_str.replace(",", ""))
                    reversed_lines.append(f"{indent}{account}  {-amount:.2f} {currency}{rest}")
                except ValueError:
                    reversed_lines.append(line)
                continue
            # Match id metadata
            id_match = re.match(r'(\s+)id:\s+"(.+)"', line)
            if id_match:
                reversed_lines.append(f'{id_match.group(1)}id: "{id_match.group(2)}-void"')
                continue
            # Balance posting (no amount) — keep as-is
            if line.strip() and not line.strip().startswith(";"):
                reversed_lines.append(line)

        reversing_entry = "\n".join(reversed_lines)

        # Add to journal
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            from add_beancount_entries import add_entries_to_beancount
            parsed = {"postings": [reversing_entry], "commodities": []}
            add_entries_to_beancount(parsed, ledger_type)
            await asyncio.to_thread(run_bean_format, ledger_type)
            valid, check_msg = await asyncio.to_thread(run_bean_check, ledger_type)

            status = f"Reversing entry added.\nbean-check: {'PASS' if valid else 'FAIL'}"
            if not valid:
                status += f"\n{check_msg[:500]}"
            await query.edit_message_text(status)
        except Exception as e:
            await query.edit_message_text(f"Error voiding: {e}")
        finally:
            if str(SCRIPTS_DIR) in sys.path:
                sys.path.remove(str(SCRIPTS_DIR))
            context.bot_data.pop(item_id, None)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit an entry with LLM assistance."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, rest = parse_ledger_and_args(args)
    search_term = " ".join(rest).strip()

    if not search_term:
        await update.message.reply_text("Usage: /edit [personal] <search-term>\nThen describe changes.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Find the entry
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from extract_top_postings import extract_latest_top_postings
        journal_path = FINANCES_DIR / ledger_type / "journal.beancount"
        journal_text = journal_path.read_text(encoding="utf-8")
        results = extract_latest_top_postings(journal_text, search_term, top_n=1)

        if not results:
            await update.message.reply_text(f"No entry found for '{search_term}'.")
            return

        entry_text = results[0].strip()
        item_id = f"edt:{datetime.now().strftime('%H%M%S')}"
        context.bot_data[item_id] = {
            "entry_text": entry_text,
            "ledger_type": ledger_type,
            "state": "awaiting_instructions",
        }

        await update.message.reply_text(
            f"Found entry:\n```\n{entry_text}\n```\n\nDescribe the changes you want to make (reply to this message):",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error("Edit search failed: %s", e)
        await update.message.reply_text(f"Error: {e}")
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Regenerate journal summary."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, _ = parse_ledger_and_args(args)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    if await asyncio.to_thread(regenerate_summary, ledger_type):
        await update.message.reply_text(f"Summary regenerated ({ledger_type}).")
    else:
        await update.message.reply_text(f"Failed to regenerate summary ({ledger_type}).")


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for finance maintenance commands."""
    cmd = msg.command
    args = msg.args or []

    if cmd == "validate":
        ledger_type, _ = parse_ledger_and_args(args)
        valid, check_msg = await asyncio.to_thread(run_bean_check, ledger_type)
        if valid:
            await reply.reply_text(f"bean-check ({ledger_type}): PASS")
        else:
            await reply.reply_text(f"bean-check ({ledger_type}): FAIL\n\n{check_msg}")
        return True

    if cmd == "fxsync":
        try:
            from beancount.core import data as bc_data

            needed = set()
            for lt in ["cupbots", "personal"]:
                entries, _, _ = load_beancount(lt)
                for entry in entries:
                    if isinstance(entry, bc_data.Transaction):
                        for posting in entry.postings:
                            if posting.units.currency != OPERATING_CURRENCY:
                                needed.add((entry.date, posting.units.currency))

            fx_path = FINANCES_DIR / "cupbots" / "fx.beancount"
            existing = set()
            if fx_path.exists():
                for line in fx_path.read_text(encoding="utf-8").splitlines():
                    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+price\s+EUR\s+[\d.]+\s+(\w+)", line)
                    if m:
                        from datetime import date as date_cls
                        existing.add((date_cls.fromisoformat(m.group(1)), m.group(2)))

            to_fetch = sorted(needed - existing)

            if not to_fetch:
                await reply.reply_text(f"All {len(needed)} FX rates already present.")
                return True

            await reply.reply_text(f"Fetching {len(to_fetch)} missing rates (have {len(existing)})...")

            fetched = 0
            failed = 0
            new_lines = []

            async with httpx.AsyncClient(timeout=10) as client:
                for dt, currency in to_fetch:
                    date_str = dt.isoformat()
                    base_lower = OPERATING_CURRENCY.lower()
                    url = FX_API_TEMPLATE.format(date=date_str, currency=base_lower)
                    try:
                        r = await client.get(url)
                        if r.status_code == 200:
                            data = r.json()
                            rate = data.get(base_lower, {}).get(currency.lower())
                            if rate is not None:
                                new_lines.append(f"{date_str} price EUR {rate:.10g} {currency}")
                                fetched += 1
                                continue
                    except Exception as e:
                        log.error("FX fetch %s/%s: %s", date_str, currency, e)
                    failed += 1

            if new_lines:
                lines = fx_path.read_text(encoding="utf-8").splitlines() if fx_path.exists() else ["* Currency rate", ""]
                header_lines = []
                prices = []
                for line in lines:
                    if re.match(r"\d{4}-\d{2}-\d{2}\s+price\s+", line):
                        prices.append(line)
                    elif not prices:
                        header_lines.append(line)

                prices.extend(new_lines)
                prices = sorted(set(prices))
                fx_path.write_text("\n".join(header_lines + prices) + "\n", encoding="utf-8")

            await reply.reply_text(
                f"FX sync complete: {fetched} fetched, {failed} failed, {len(existing)} existing"
            )
        except Exception as e:
            log.error("FX sync failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "void":
        ledger_type, rest = parse_ledger_and_args(args)
        search_term = " ".join(rest).strip()

        if not search_term:
            await reply.reply_text("Usage: /void [personal] <search-term>")
            return True

        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            from extract_top_postings import extract_latest_top_postings
            journal_path = FINANCES_DIR / ledger_type / "journal.beancount"
            journal_text = journal_path.read_text(encoding="utf-8")
            results = extract_latest_top_postings(journal_text, search_term, top_n=1)

            if not results:
                await reply.reply_text(f"No entry found for '{search_term}'.")
                return True

            entry_text = results[0].strip()

            # Generate reversing entry (no approval buttons, just do it)
            lines = entry_text.split("\n")
            reversed_lines = []
            for line in lines:
                header_match = re.match(r'(\d{4}-\d{2}-\d{2})\s+\*\s+(".*?")\s+(".*?")', line)
                if header_match:
                    today = datetime.now().strftime("%Y-%m-%d")
                    reversed_lines.append(f'{today} * {header_match.group(2)} "VOID: {header_match.group(3)[1:-1]}"')
                    continue
                posting_match = re.match(r'(\s+)([\w:]+)\s+(-?[\d,.]+)\s+(\w+)(.*)', line)
                if posting_match:
                    indent, account, amount_str, currency, rest_line = posting_match.groups()
                    try:
                        amount = float(amount_str.replace(",", ""))
                        reversed_lines.append(f"{indent}{account}  {-amount:.2f} {currency}{rest_line}")
                    except ValueError:
                        reversed_lines.append(line)
                    continue
                id_match = re.match(r'(\s+)id:\s+"(.+)"', line)
                if id_match:
                    reversed_lines.append(f'{id_match.group(1)}id: "{id_match.group(2)}-void"')
                    continue
                if line.strip() and not line.strip().startswith(";"):
                    reversed_lines.append(line)

            reversing_entry = "\n".join(reversed_lines)

            from add_beancount_entries import add_entries_to_beancount
            parsed = {"postings": [reversing_entry], "commodities": []}
            add_entries_to_beancount(parsed, ledger_type)
            await asyncio.to_thread(run_bean_format, ledger_type)
            valid, check_msg = await asyncio.to_thread(run_bean_check, ledger_type)

            output = f"Original entry:\n{entry_text}\n\nReversing entry added.\nbean-check: {'PASS' if valid else 'FAIL'}"
            if not valid:
                output += f"\n{check_msg[:500]}"
            await reply.reply_text(output)
        except Exception as e:
            log.error("Void failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        finally:
            if str(SCRIPTS_DIR) in sys.path:
                sys.path.remove(str(SCRIPTS_DIR))
        return True

    if cmd == "edit":
        await reply.reply_text("The /edit command is not available on this platform (requires interactive UI).")
        return True

    if cmd == "summary":
        ledger_type, _ = parse_ledger_and_args(args)
        if await asyncio.to_thread(regenerate_summary, ledger_type):
            await reply.reply_text(f"Summary regenerated ({ledger_type}).")
        else:
            await reply.reply_error(f"Failed to regenerate summary ({ledger_type}).")
        return True

    return False


def register(app: Application):
    """Register finance maintenance commands."""
    tid = get_finance_thread_id()

    app.add_handler(topic_command("validate", cmd_validate, thread_id=tid))
    app.add_handler(topic_command("fxsync", cmd_fxsync, thread_id=tid))
    app.add_handler(topic_command("void", cmd_void, thread_id=tid))
    app.add_handler(topic_command("edit", cmd_edit, thread_id=tid))
    app.add_handler(topic_command("summary", cmd_summary, thread_id=tid))
    app.add_handler(CallbackQueryHandler(_callback_void, pattern=r"^vod:"))

    log.info("Finance maintenance plugin loaded (thread: %s)", tid or "any")
