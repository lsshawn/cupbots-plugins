"""
Finance Maintenance — Journal maintenance tools.

Commands:
  /validate [personal]          — Run bean-check
  /fxsync                       — Fetch missing FX rates
  /void [personal] <search>     — Void an entry (reversing entry)
  /summary [personal]           — Regenerate journal summary
"""

import asyncio
import re
import sys
from datetime import datetime

import httpx

from cupbots.helpers.logger import get_logger
from cupbots.helpers.access import is_admin
from plugins._finance_helpers import (
    FINANCES_DIR,
    OPERATING_CURRENCY,
    SCRIPTS_DIR,
    load_beancount,
    parse_ledger_and_args,
    run_bean_check,
    run_bean_format,
    regenerate_summary,
    send_long_text,
)

log = get_logger("finance.maint")

COMMANDS = ("validate", "fxsync", "void", "summary")

# FX API (same as generate_rates.py)
FX_API_TEMPLATE = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date}/v1/currencies/{currency}.json"


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for finance maintenance commands."""
    cmd = msg.command
    if cmd not in COMMANDS:
        return False

    args = msg.args or []

    # --help support
    if args and args[0] in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    # Access control
    if not is_admin(msg.platform, msg.sender_id) and not msg.sender_role:
        await reply.reply_text("Finance commands are restricted.")
        return True

    if cmd == "validate":
        await _validate(reply, args)
        return True

    if cmd == "fxsync":
        await _fxsync(reply)
        return True

    if cmd == "void":
        await _void(reply, args)
        return True

    if cmd == "summary":
        await _summary(reply, args)
        return True

    return False


async def _validate(reply, args):
    ledger_type, _ = parse_ledger_and_args(args)
    await reply.send_typing()
    valid, check_msg = await asyncio.to_thread(run_bean_check, ledger_type)
    if valid:
        await reply.reply_text(f"bean-check ({ledger_type}): PASS")
    else:
        await send_long_text(reply, f"bean-check ({ledger_type}): FAIL\n\n{check_msg}", "errors.txt")


async def _fxsync(reply):
    await reply.send_typing()
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
            return

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


async def _void(reply, args):
    ledger_type, rest = parse_ledger_and_args(args)
    search_term = " ".join(rest).strip()

    if not search_term:
        await reply.reply_text("Usage: /void [personal] <search-term>")
        return

    await reply.send_typing()
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from extract_top_postings import extract_latest_top_postings
        journal_path = FINANCES_DIR / ledger_type / "journal.beancount"
        journal_text = journal_path.read_text(encoding="utf-8")
        results = extract_latest_top_postings(journal_text, search_term, top_n=1)

        if not results:
            await reply.reply_text(f"No entry found for '{search_term}'.")
            return

        entry_text = results[0].strip()

        # Generate reversing entry by negating amounts
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


async def _summary(reply, args):
    ledger_type, _ = parse_ledger_and_args(args)
    await reply.send_typing()
    if await asyncio.to_thread(regenerate_summary, ledger_type):
        await reply.reply_text(f"Summary regenerated ({ledger_type}).")
    else:
        await reply.reply_error(f"Failed to regenerate summary ({ledger_type}).")
