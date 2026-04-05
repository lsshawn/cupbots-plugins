"""
Finance Query — Beancount ledger queries.

Commands:
  /fbal [personal] [filter]      — Show account balances
  /fsearch [personal] <query>    — AI-powered journal search
  /fquery [personal] <BQL>       — Run raw BQL query
  /faccount [personal] [filter]  — List chart of accounts
"""

import re
from datetime import date
from pathlib import Path

from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli
from cupbots.helpers.access import is_admin
from plugins._finance_helpers import (
    FINANCES_DIR,
    _get_ledger_paths,
    load_beancount,
    parse_ledger_and_args,
    run_bql,
    run_bql_raw,
    send_long_text,
)

log = get_logger("finance.query")

COMMANDS = ("fbal", "fsearch", "fquery", "faccount")


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for finance query commands."""
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

    if cmd == "fbal":
        await _fbal(reply, args)
        return True

    if cmd == "fsearch":
        await _fsearch(reply, args)
        return True

    if cmd == "fquery":
        await _fquery(reply, args)
        return True

    if cmd == "faccount":
        await _faccount(reply, args)
        return True

    return False


async def _fbal(reply, args):
    ledger_type, rest = parse_ledger_and_args(args)
    acct_filter = " ".join(rest).strip() if rest else ""

    where = "WHERE account ~ 'Assets' OR account ~ 'Liabilities'"
    if acct_filter:
        where = f"WHERE account ~ '{acct_filter}'"

    await reply.send_typing()
    try:
        result = run_bql(
            ledger_type,
            f"SELECT account, sum(position) {where} GROUP BY account ORDER BY account",
        )
        header = f"Balances ({ledger_type})"
        if acct_filter:
            header += f" ~ {acct_filter}"
        await send_long_text(reply, f"{header}\n{'=' * len(header)}\n\n{result}", "balances.txt")
    except Exception as e:
        log.error("Balance query failed: %s", e)
        await reply.reply_error(f"Error: {e}")


async def _fsearch(reply, args):
    ledger_type, rest = parse_ledger_and_args(args)
    query_str = " ".join(rest).strip()

    if not query_str:
        await reply.reply_text(
            "Usage: /fsearch [personal] <query>\n\n"
            "Examples:\n"
            "  /fsearch how much did I spend on travel this year\n"
            "  /fsearch personal grocery expenses last month\n"
            "  /fsearch all payments from Third-Idea"
        )
        return

    await reply.send_typing()

    paths = _get_ledger_paths(ledger_type)
    journal_path = paths["journal"]
    summary_path = paths["summary"]
    today = date.today().isoformat()

    system_prompt = f"""You are a beancount accounting assistant. Today is {today}.

You have access to the Read tool. The user is querying the '{ledger_type}' ledger.

Key files:
- Journal: {journal_path}
- Chart of accounts & examples: {summary_path}
- FX rates: {paths['fx']}

The operating currency is EUR. The ledger uses beancount 3 format.

To answer the user's query:
1. First read {summary_path} to understand the account structure
2. Then read {journal_path} and search for relevant entries
3. Present the results clearly — show matching transactions, totals, or analysis as requested

Format your response as plain text. Use fixed-width alignment for tables.
Keep it concise but complete. Show actual transaction entries when the user is searching for specific items."""

    try:
        result = await run_claude_cli(
            query_str,
            model="sonnet",
            system_prompt=system_prompt,
            tools="Read,Grep,Bash(grep:*,awk:*,head:*,tail:*,wc:*)",
            max_turns=10,
            timeout=120,
        )

        response = result["text"]
        if not response or response == "No response from Claude.":
            await reply.reply_text("No results found.")
            return

        await send_long_text(reply, response, "search.txt")
    except Exception as e:
        log.error("AI search failed: %s", e)
        await reply.reply_error(f"Error: {e}")


async def _fquery(reply, args):
    ledger_type, rest = parse_ledger_and_args(args)
    bql = " ".join(rest).strip()

    if not bql:
        await reply.reply_text(
            "Usage: /fquery [personal] <BQL>\n\n"
            "Example:\n"
            "  /fquery SELECT date, narration, account, position WHERE payee ~ 'Caltex'"
        )
        return

    await reply.send_typing()
    try:
        result = run_bql(ledger_type, bql)
        await send_long_text(reply, f"BQL ({ledger_type})\n\n{result}", "query.txt")
    except Exception as e:
        log.error("BQL query failed: %s", e)
        await reply.reply_error(f"Query error: {e}")


async def _faccount(reply, args):
    ledger_type, rest = parse_ledger_and_args(args)
    acct_filter = " ".join(rest).strip().lower() if rest else ""

    await reply.send_typing()
    try:
        from beancount.core import data as bc_data
        entries, errors, options = load_beancount(ledger_type)

        opened = set()
        closed = set()
        for entry in entries:
            if isinstance(entry, bc_data.Open):
                opened.add(entry.account)
            elif isinstance(entry, bc_data.Close):
                closed.add(entry.account)

        active = sorted(opened - closed)
        if acct_filter:
            active = [a for a in active if acct_filter in a.lower()]

        header = f"Accounts ({ledger_type})"
        if acct_filter:
            header += f" ~ {acct_filter}"
        text = f"{header}\n{'=' * len(header)}\n\n" + "\n".join(active)
        await send_long_text(reply, text, "accounts.txt")
    except Exception as e:
        log.error("Accounts query failed: %s", e)
        await reply.reply_error(f"Error: {e}")
