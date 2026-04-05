"""
Finance Wise — Wise account queries.

Commands:
  /wise [personal|cupbots] [period]      — Show balances + recent transactions
  /wise balances [personal|cupbots]      — Show balances only
  /wise txns [personal|cupbots] [period] — Show transactions
  /wise ask [personal|cupbots] <question> — AI-powered Wise account query
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli
from cupbots.helpers.access import is_admin
from plugins._finance_helpers import (
    parse_date_range,
    send_long_text,
)

log = get_logger("finance.wise")

# Lazy import wise_sync (add finances/scripts to path)
FINANCE_SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent / "finances" / "scripts"


def _get_wise():
    """Lazy import wise_sync module."""
    if str(FINANCE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(FINANCE_SCRIPTS))
    import wise_sync
    return wise_sync


def _parse_wise_args(args: list[str]) -> tuple[str, list[str]]:
    """Parse profile from args. Maps 'cupbots' -> 'business', 'personal' -> 'personal'."""
    if args and args[0].lower() in ("personal", "cupbots", "business"):
        profile = args[0].lower()
        if profile == "cupbots":
            profile = "business"
        return profile, args[1:]
    return "personal", list(args)


def _days_from_period(args: list[str]) -> tuple[int, list[str]]:
    """Convert period args to a number of days lookback."""
    if not args:
        return 30, []
    start, end, rest = parse_date_range(args)
    days = (date.today() - start).days + 1
    return max(days, 1), rest


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for Wise commands."""
    if msg.command != "wise":
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

    # Route subcommands
    if args and args[0].lower() in ("balances", "bal"):
        await _balances(reply, args[1:])
        return True

    if args and args[0].lower() in ("txns", "transactions", "tx"):
        await _txns(reply, args[1:])
        return True

    if args and args[0].lower() == "ask":
        await _ask(reply, args[1:])
        return True

    # Default: balances + recent txns
    await _overview(reply, args)
    return True


async def _overview(reply, args):
    profile, rest = _parse_wise_args(args)
    days, _ = _days_from_period(rest)

    await reply.send_typing()
    try:
        ws = _get_wise()
        balances = await asyncio.to_thread(ws.fetch_balances, profile)
        txns = await asyncio.to_thread(ws.fetch_transactions, profile, days)

        bal_text = ws.format_balances_text(balances, profile)
        txn_text = ws.format_transactions_text(txns)

        label = "CupBots" if profile == "business" else "Personal"
        output = f"{bal_text}\n\n{label} Transactions (last {days}d)\n{'=' * 30}\n{txn_text}"
        await send_long_text(reply, output, "wise.txt")
    except Exception as e:
        log.error("Wise overview failed: %s", e)
        await reply.reply_error(f"Error: {e}")


async def _balances(reply, args):
    profile, _ = _parse_wise_args(args)

    await reply.send_typing()
    try:
        ws = _get_wise()
        balances = await asyncio.to_thread(ws.fetch_balances, profile)
        text = ws.format_balances_text(balances, profile)
        await send_long_text(reply, text, "balances.txt")
    except Exception as e:
        log.error("Wise balances failed: %s", e)
        await reply.reply_error(f"Error: {e}")


async def _txns(reply, args):
    profile, rest = _parse_wise_args(args)
    days, _ = _days_from_period(rest)

    await reply.send_typing()
    try:
        ws = _get_wise()
        txns = await asyncio.to_thread(ws.fetch_transactions, profile, days)
        text = ws.format_transactions_text(txns)
        label = "CupBots" if profile == "business" else "Personal"
        header = f"Wise {label} Transactions (last {days}d)\n{'=' * 40}"
        await send_long_text(reply, f"{header}\n{text}", "transactions.txt")
    except Exception as e:
        log.error("Wise transactions failed: %s", e)
        await reply.reply_error(f"Error: {e}")


async def _ask(reply, args):
    profile, rest = _parse_wise_args(args)
    question = " ".join(rest).strip()

    if not question:
        await reply.reply_text(
            "Usage: /wise ask [personal|cupbots] <question>\n\n"
            "Examples:\n"
            "  /wise ask what's my USD balance?\n"
            "  /wise ask cupbots show me all conversions this month\n"
            "  /wise ask how much did I send to IBKR recently?"
        )
        return

    await reply.send_typing()
    await reply.reply_text("Querying Wise account...")

    try:
        ws = _get_wise()
        balances = await asyncio.to_thread(ws.fetch_balances, profile)
        txns = await asyncio.to_thread(ws.fetch_transactions, profile, 60)

        bal_text = ws.format_balances_text(balances, profile)
        txn_text = ws.format_transactions_text(txns)

        label = "CupBots (business)" if profile == "business" else "Personal"
        ledger = "cupbots" if profile == "business" else "personal"

        system_prompt = f"""You are a Wise account assistant. Today is {date.today().isoformat()}.

You have live Wise API data for the {label} account:

{bal_text}

Recent Transactions (last 60 days):
{txn_text}

The user's beancount ledger for this account is at: finances/{ledger}/journal.beancount
You have the Read and Grep tools to look up journal entries if needed.

Answer the user's question about their Wise account. Be concise and use numbers.
Format as plain text, use fixed-width alignment for tables."""

        result = await run_claude_cli(
            question,
            model="haiku",
            system_prompt=system_prompt,
            tools="Read,Grep",
            max_turns=5,
            timeout=60,
        )

        response = result["text"]
        if not response or response == "No response from Claude.":
            await reply.reply_text("No answer -- try rephrasing.")
            return

        await send_long_text(reply, response, "wise_answer.txt")
    except Exception as e:
        log.error("Wise ask failed: %s", e)
        await reply.reply_error(f"Error: {e}")
