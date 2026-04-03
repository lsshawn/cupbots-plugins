"""
Finance Wise — Wise account queries

Commands (scoped to finance topic thread):
  /wise [personal|cupbots] [period]     — Show balances + recent transactions
  /wise balances [personal|cupbots]     — Show balances only
  /wise txns [personal|cupbots] [period] — Show transactions
  /wise ask [personal|cupbots] <question> — AI-powered Wise account query
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes

from cupbots.topic_filter import topic_command
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli
from plugins._finance_helpers import (
    get_finance_thread_id,
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


async def cmd_wise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Wise balances and recent transactions."""
    if not update.message:
        return
    args = context.args or []

    # Check for subcommands
    if args and args[0].lower() in ("balances", "bal"):
        return await cmd_wise_balances(update, context)
    if args and args[0].lower() in ("txns", "transactions", "tx"):
        args = args[1:]
        context.args = args
        return await cmd_wise_txns(update, context)
    if args and args[0].lower() == "ask":
        args = args[1:]
        context.args = args
        return await cmd_wise_ask(update, context)

    profile, rest = _parse_wise_args(args)
    days, _ = _days_from_period(rest)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        ws = _get_wise()
        balances = await asyncio.to_thread(ws.fetch_balances, profile)
        txns = await asyncio.to_thread(ws.fetch_transactions, profile, days)

        bal_text = ws.format_balances_text(balances, profile)
        txn_text = ws.format_transactions_text(txns)

        label = "CupBots" if profile == "business" else "Personal"
        output = f"{bal_text}\n\n{label} Transactions (last {days}d)\n{'=' * 30}\n{txn_text}"
        await send_long_text(update, context, output, "wise.txt")
    except Exception as e:
        log.error("Wise overview failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_wise_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Wise balances only."""
    if not update.message:
        return
    args = (context.args or [])[1:]  # skip 'balances' subcommand
    profile, _ = _parse_wise_args(args)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        ws = _get_wise()
        balances = await asyncio.to_thread(ws.fetch_balances, profile)
        text = ws.format_balances_text(balances, profile)
        await send_long_text(update, context, text, "balances.txt")
    except Exception as e:
        log.error("Wise balances failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_wise_txns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Wise transactions."""
    if not update.message:
        return
    args = context.args or []
    profile, rest = _parse_wise_args(args)
    days, _ = _days_from_period(rest)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        ws = _get_wise()
        txns = await asyncio.to_thread(ws.fetch_transactions, profile, days)
        text = ws.format_transactions_text(txns)
        label = "CupBots" if profile == "business" else "Personal"
        header = f"Wise {label} Transactions (last {days}d)\n{'=' * 40}"
        await send_long_text(update, context, f"{header}\n{text}", "transactions.txt")
    except Exception as e:
        log.error("Wise transactions failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_wise_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """AI-powered Wise account query."""
    if not update.message:
        return
    args = context.args or []
    profile, rest = _parse_wise_args(args)
    question = " ".join(rest).strip()

    if not question:
        await update.message.reply_text(
            "Usage: /wise ask [personal|cupbots] <question>\n\n"
            "Examples:\n"
            "  /wise ask what's my USD balance?\n"
            "  /wise ask cupbots show me all conversions this month\n"
            "  /wise ask how much did I send to IBKR recently?\n"
            "  /wise ask personal what fees did I pay last month?"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    status_msg = await update.message.reply_text("Querying Wise account...")

    try:
        # Fetch live data to provide as context
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
Format for Telegram — plain text, use fixed-width alignment for tables."""

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
            await status_msg.edit_text("No answer — try rephrasing.")
            return

        await status_msg.delete()
        await send_long_text(update, context, response, "wise_answer.txt", parse_mode=None)
    except Exception as e:
        log.error("Wise ask failed: %s", e)
        await status_msg.edit_text(f"Error: {e}")


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for Wise commands."""
    if msg.command != "wise":
        return False

    args = msg.args or []

    # Route subcommands
    if args and args[0].lower() in ("balances", "bal"):
        sub_args = args[1:]
        profile, _ = _parse_wise_args(sub_args)
        try:
            ws = _get_wise()
            balances = await asyncio.to_thread(ws.fetch_balances, profile)
            text = ws.format_balances_text(balances, profile)
            await reply.reply_text(text)
        except Exception as e:
            log.error("Wise balances failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if args and args[0].lower() in ("txns", "transactions", "tx"):
        sub_args = args[1:]
        profile, rest = _parse_wise_args(sub_args)
        days, _ = _days_from_period(rest)
        try:
            ws = _get_wise()
            txns = await asyncio.to_thread(ws.fetch_transactions, profile, days)
            text = ws.format_transactions_text(txns)
            label = "CupBots" if profile == "business" else "Personal"
            header = f"Wise {label} Transactions (last {days}d)\n{'=' * 40}"
            await reply.reply_text(f"{header}\n{text}")
        except Exception as e:
            log.error("Wise transactions failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if args and args[0].lower() == "ask":
        sub_args = args[1:]
        profile, rest = _parse_wise_args(sub_args)
        question = " ".join(rest).strip()

        if not question:
            await reply.reply_text(
                "Usage: /wise ask [personal|cupbots] <question>\n\n"
                "Examples:\n"
                "  /wise ask what's my USD balance?\n"
                "  /wise ask cupbots show me all conversions this month\n"
                "  /wise ask how much did I send to IBKR recently?"
            )
            return True

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
                return True

            await reply.reply_text(response)
        except Exception as e:
            log.error("Wise ask failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    # Default: balances + recent txns
    profile, rest = _parse_wise_args(args)
    days, _ = _days_from_period(rest)

    try:
        ws = _get_wise()
        balances = await asyncio.to_thread(ws.fetch_balances, profile)
        txns = await asyncio.to_thread(ws.fetch_transactions, profile, days)

        bal_text = ws.format_balances_text(balances, profile)
        txn_text = ws.format_transactions_text(txns)

        label = "CupBots" if profile == "business" else "Personal"
        output = f"{bal_text}\n\n{label} Transactions (last {days}d)\n{'=' * 30}\n{txn_text}"
        await reply.reply_text(output)
    except Exception as e:
        log.error("Wise overview failed: %s", e)
        await reply.reply_error(f"Error: {e}")
    return True


def register(app: Application):
    """Register Wise finance commands."""
    tid = get_finance_thread_id()

    app.add_handler(topic_command("wise", cmd_wise, thread_id=tid))

    log.info("Finance Wise plugin loaded (thread: %s)", tid or "any")
