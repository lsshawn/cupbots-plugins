"""
Finance Query — Beancount ledger queries

Commands (scoped to finance topic thread):
  /fbal [personal] [filter]      — Show account balances
  /fsearch [personal] <query>    — AI-powered journal search
  /fsearch --doc <query>         — Search + show receipt/document
  /fquery [personal] <BQL>       — Run raw BQL query
  /fquery --doc <BQL>            — Query + show documents for results
  /faccount [personal] [filter]  — List chart of accounts
"""

import re
from datetime import date
from pathlib import Path

from telegram import InputMediaDocument, InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes

from cupbots.topic_filter import topic_command
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli
from plugins._finance_helpers import (
    FINANCES_DIR,
    SCRIPTS_DIR,
    get_finance_thread_id,
    _get_ledger_paths,
    load_beancount,
    parse_ledger_and_args,
    run_bql,
    run_bql_raw,
    send_long_text,
)

log = get_logger("finance.query")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
DOC_EXTS = {".pdf"} | IMAGE_EXTS


def _find_documents(ledger_type: str, entries_text: list[str]) -> list[Path]:
    """Find document files matching transaction entries.

    Searches by date prefix and id metadata in the ledger directory tree.
    Returns list of file paths found (deduplicated, max 10).
    """
    ledger_root = FINANCES_DIR / ledger_type
    found = []
    seen = set()

    for entry in entries_text:
        # Extract date from entry header
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", entry.strip())
        if not date_match:
            continue
        date_str = date_match.group(1)

        # Extract id metadata if present
        id_match = re.search(r'id:\s*"([^"]+)"', entry)
        entry_id = id_match.group(1) if id_match else None

        # Extract account paths to narrow search (e.g. Expenses:Travel:Transport -> Expenses/Travel/Transport)
        account_paths = []
        for acct_match in re.finditer(r"(Expenses|Income):[:\w]+", entry):
            acct_path = acct_match.group(0).replace(":", "/")
            account_paths.append(acct_path)

        # Search in account-specific directories first, then broadly
        search_dirs = [ledger_root / p for p in account_paths] if account_paths else []
        search_dirs.append(ledger_root)

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            # Find files starting with the date prefix
            for f in search_dir.rglob(f"{date_str}*"):
                if f.suffix.lower() in DOC_EXTS and f not in seen:
                    seen.add(f)
                    found.append(f)
                    if len(found) >= 10:
                        return found

    return found


async def _send_documents(update: Update, context: ContextTypes.DEFAULT_TYPE, files: list[Path]):
    """Send document files as photos (images) or documents (PDFs)."""
    msg = update.message
    thread_id = msg.message_thread_id

    if not files:
        await msg.reply_text("No documents found for these entries.")
        return

    # Send as media group if multiple, individual if single
    if len(files) == 1:
        f = files[0]
        if f.suffix.lower() in IMAGE_EXTS:
            await msg.reply_photo(photo=open(f, "rb"), caption=f.name)
        else:
            await msg.reply_document(document=open(f, "rb"), filename=f.name)
        return

    # Group into batches of 10 (Telegram limit)
    media = []
    for f in files[:10]:
        if f.suffix.lower() in IMAGE_EXTS:
            media.append(InputMediaPhoto(media=open(f, "rb"), caption=f.name))
        else:
            media.append(InputMediaDocument(media=open(f, "rb"), caption=f.name, filename=f.name))

    await context.bot.send_media_group(
        chat_id=msg.chat_id,
        media=media,
        message_thread_id=thread_id,
    )


async def cmd_fbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show account balances."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, rest = parse_ledger_and_args(args)
    acct_filter = " ".join(rest).strip() if rest else ""

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    where = "WHERE account ~ 'Assets' OR account ~ 'Liabilities'"
    if acct_filter:
        where = f"WHERE account ~ '{acct_filter}'"

    try:
        result = run_bql(
            ledger_type,
            f"SELECT account, sum(position) {where} GROUP BY account ORDER BY account",
        )
        header = f"Balances ({ledger_type})"
        if acct_filter:
            header += f" ~ {acct_filter}"
        await send_long_text(update, context, f"{header}\n{'=' * len(header)}\n\n{result}", "balances.txt")
    except Exception as e:
        log.error("Balance query failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_fsearch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """AI-powered search across journal entries."""
    if not update.message:
        return
    args = context.args or []
    show_docs = "--doc" in args
    if show_docs:
        args = [a for a in args if a != "--doc"]
    ledger_type, rest = parse_ledger_and_args(args)
    query_str = " ".join(rest).strip()

    if not query_str:
        await update.message.reply_text(
            "Usage: /fsearch [--doc] [personal] <query>\n\n"
            "Examples:\n"
            "  /fsearch how much did I spend on travel this year\n"
            "  /fsearch --doc petrol receipts from march\n"
            "  /fsearch personal grocery expenses last month\n"
            "  /fsearch all payments from Third-Idea"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

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

Format your response as plain text suitable for Telegram. Use fixed-width alignment for tables.
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
            await update.message.reply_text("No results found.")
            return

        await send_long_text(update, context, response, "search.txt", parse_mode=None)

        # If --doc, extract dates from the AI response and find documents
        if show_docs:
            # Find all dates mentioned in the response
            date_matches = re.findall(r"\d{4}-\d{2}-\d{2}", response)
            if date_matches:
                pseudo_entries = [f"{d} placeholder" for d in set(date_matches)]
                docs = _find_documents(ledger_type, pseudo_entries)
                if docs:
                    await _send_documents(update, context, docs)
                else:
                    await update.message.reply_text("No documents found for these entries.")
    except Exception as e:
        log.error("AI search failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_fquery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run raw BQL query. Use --doc to also show matching documents."""
    if not update.message:
        return
    args = context.args or []
    show_docs = "--doc" in args
    if show_docs:
        args = [a for a in args if a != "--doc"]
    ledger_type, rest = parse_ledger_and_args(args)
    bql = " ".join(rest).strip()

    if not bql:
        await update.message.reply_text(
            "Usage: /fquery [--doc] [personal] <BQL>\n\n"
            "Example:\n"
            "  /fquery SELECT date, narration, account, position WHERE payee ~ 'Caltex'\n"
            "  /fquery --doc SELECT * WHERE date >= 2026-03-01 AND account ~ 'Expenses:Travel'"
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        from plugins._finance_helpers import run_bql_raw
        result = run_bql(ledger_type, bql)
        await send_long_text(update, context, f"BQL ({ledger_type})\n\n{result}", "query.txt")

        if show_docs:
            # Extract dates from BQL results to find documents
            # Run the query again to get raw rows with date info
            result_types, result_rows = run_bql_raw(ledger_type, bql)
            # Build pseudo-entry strings for document matching
            entries = []
            for row in result_rows:
                row_str = "  ".join(str(v) for v in row if v is not None)
                entries.append(row_str)

            docs = _find_documents(ledger_type, entries)
            if docs:
                await _send_documents(update, context, docs)
            else:
                await update.message.reply_text("No documents found for query results.")
    except Exception as e:
        log.error("BQL query failed: %s", e)
        await update.message.reply_text(f"Query error: {e}")


async def cmd_faccount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List chart of accounts."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, rest = parse_ledger_and_args(args)
    acct_filter = " ".join(rest).strip().lower() if rest else ""

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        from beancount.core import data as bc_data
        entries, errors, options = load_beancount(ledger_type)

        # Collect open accounts not yet closed
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
        await send_long_text(update, context, text, "accounts.txt")
    except Exception as e:
        log.error("Accounts query failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for finance query commands."""
    cmd = msg.command
    args = msg.args or []

    if cmd == "fbal":
        ledger_type, rest = parse_ledger_and_args(args)
        acct_filter = " ".join(rest).strip() if rest else ""

        where = "WHERE account ~ 'Assets' OR account ~ 'Liabilities'"
        if acct_filter:
            where = f"WHERE account ~ '{acct_filter}'"

        try:
            result = run_bql(
                ledger_type,
                f"SELECT account, sum(position) {where} GROUP BY account ORDER BY account",
            )
            header = f"Balances ({ledger_type})"
            if acct_filter:
                header += f" ~ {acct_filter}"
            await reply.reply_text(f"{header}\n{'=' * len(header)}\n\n{result}")
        except Exception as e:
            log.error("Balance query failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "fsearch":
        show_docs = "--doc" in args
        if show_docs:
            args = [a for a in args if a != "--doc"]
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
            return True

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
                return True

            await reply.reply_text(response)
        except Exception as e:
            log.error("AI search failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "fquery":
        show_docs = "--doc" in args
        if show_docs:
            args = [a for a in args if a != "--doc"]
        ledger_type, rest = parse_ledger_and_args(args)
        bql = " ".join(rest).strip()

        if not bql:
            await reply.reply_text(
                "Usage: /fquery [personal] <BQL>\n\n"
                "Example:\n"
                "  /fquery SELECT date, narration, account, position WHERE payee ~ 'Caltex'"
            )
            return True

        try:
            result = run_bql(ledger_type, bql)
            await reply.reply_text(f"BQL ({ledger_type})\n\n{result}")
        except Exception as e:
            log.error("BQL query failed: %s", e)
            await reply.reply_error(f"Query error: {e}")
        return True

    if cmd == "faccount":
        ledger_type, rest = parse_ledger_and_args(args)
        acct_filter = " ".join(rest).strip().lower() if rest else ""

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
            await reply.reply_text(text)
        except Exception as e:
            log.error("Accounts query failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    return False


def register(app: Application):
    """Register finance query commands."""
    tid = get_finance_thread_id()

    app.add_handler(topic_command("fbal", cmd_fbal, thread_id=tid))
    app.add_handler(topic_command("fsearch", cmd_fsearch, thread_id=tid))
    app.add_handler(topic_command("fquery", cmd_fquery, thread_id=tid))
    app.add_handler(topic_command("faccount", cmd_faccount, thread_id=tid))

    log.info("Finance query plugin loaded (thread: %s)", tid or "any")
