"""
Finance Audit — Auditing and reconciliation tools

Commands (scoped to finance topic thread):
  /reconcile [personal] <account> <balance> <currency>  — Compare book vs expected
  /trial [personal] [date]                              — Trial balance
  /duplicates [personal]                                — Scan for duplicate entries
"""

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes

from cupbots.topic_filter import topic_command
from cupbots.helpers.logger import get_logger
from plugins._finance_helpers import (
    OPERATING_CURRENCY,
    get_finance_thread_id,
    load_beancount,
    parse_ledger_and_args,
    run_bql,
    run_bql_raw,
    send_long_text,
)

log = get_logger("finance.audit")


async def cmd_reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compare book balance vs expected balance for an account."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, rest = parse_ledger_and_args(args)

    if len(rest) < 3:
        await update.message.reply_text(
            "Usage: /reconcile [personal] <account-filter> <expected-balance> <currency>\n\n"
            "Example: /reconcile Cash:EUR:Wise 15234.56 EUR"
        )
        return

    acct_filter = rest[0]
    try:
        expected = Decimal(rest[1])
    except Exception:
        await update.message.reply_text(f"Invalid balance: {rest[1]}")
        return
    currency = rest[2].upper()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        result_types, result_rows = run_bql_raw(
            ledger_type,
            f"SELECT account, sum(position) "
            f"WHERE account ~ '{acct_filter}' "
            f"GROUP BY account ORDER BY account",
        )

        lines = [
            f"Reconciliation ({ledger_type}) — {acct_filter}",
            "=" * 55,
            f"Expected: {expected:,.2f} {currency}",
            "",
        ]

        # Find actual balance for the target currency
        actual_total = Decimal("0")
        for row in result_rows:
            account = str(row[0])
            inv = row[1]
            if hasattr(inv, "__iter__") and not isinstance(inv, str):
                for item in inv:
                    s = str(item)
                    if currency in s:
                        parts = s.strip().split()
                        try:
                            amt = Decimal(parts[0])
                            lines.append(f"  {account:<40} {amt:>12,.2f} {currency}")
                            actual_total += amt
                        except Exception:
                            pass

        lines.extend([
            "",
            f"  Book balance:     {actual_total:>12,.2f} {currency}",
            f"  Expected balance: {expected:>12,.2f} {currency}",
        ])

        diff = actual_total - expected
        if abs(diff) < Decimal("0.01"):
            lines.append(f"\n  RECONCILED (difference: {diff:,.2f})")
        else:
            lines.append(f"\n  DISCREPANCY: {diff:>+,.2f} {currency}")

        await send_long_text(update, context, "\n".join(lines), "reconcile.txt")
    except Exception as e:
        log.error("Reconciliation failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trial balance — all accounts, should sum to zero."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, rest = parse_ledger_and_args(args)
    as_of = rest[0] if rest else date.today().isoformat()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        result = run_bql(
            ledger_type,
            f"SELECT account, sum(convert(position, '{OPERATING_CURRENCY}')) AS balance "
            f"WHERE date <= {as_of} "
            f"GROUP BY account ORDER BY account",
        )

        # Also compute total to verify it sums to zero
        result_types, result_rows = run_bql_raw(
            ledger_type,
            f"SELECT sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
            f"WHERE date <= {as_of}",
        )
        total_str = "0"
        if result_rows:
            inv = result_rows[0][0]
            if hasattr(inv, "__iter__") and not isinstance(inv, str):
                parts = [str(item) for item in inv]
                total_str = ", ".join(parts) if parts else "0"
            else:
                total_str = str(inv) if inv else "0"

        header = f"Trial Balance ({ledger_type}) — as of {as_of}"
        footer = f"\nTotal (should be ~0): {total_str}"
        await send_long_text(update, context, f"{header}\n{'=' * len(header)}\n\n{result}\n{footer}", "trial.txt")
    except Exception as e:
        log.error("Trial balance failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_duplicates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scan for potential duplicate entries."""
    if not update.message:
        return
    args = context.args or []
    ledger_type, _ = parse_ledger_and_args(args)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        from beancount.core import data as bc_data
        entries, _, _ = load_beancount(ledger_type)

        # Group transactions by (payee, total_amount, currency) within 3-day windows
        txns = []
        for entry in entries:
            if not isinstance(entry, bc_data.Transaction):
                continue
            payee = entry.payee or ""
            for posting in entry.postings:
                if posting.account.startswith("Expenses") or posting.account.startswith("Income"):
                    txns.append({
                        "date": entry.date,
                        "payee": payee.lower().strip(),
                        "amount": abs(posting.units.number),
                        "currency": posting.units.currency,
                        "narration": entry.narration or "",
                        "account": posting.account,
                    })
                    break  # Only first expense/income posting

        # Find duplicates: same payee, same amount+currency, within 3 days
        duplicates = []
        for i in range(len(txns)):
            for j in range(i + 1, len(txns)):
                a, b = txns[i], txns[j]
                if (
                    a["payee"] and a["payee"] == b["payee"]
                    and a["amount"] == b["amount"]
                    and a["currency"] == b["currency"]
                    and abs((a["date"] - b["date"]).days) <= 3
                ):
                    duplicates.append((a, b))

        if not duplicates:
            await update.message.reply_text(f"No potential duplicates found ({ledger_type}).")
            return

        lines = [
            f"Potential Duplicates ({ledger_type}) — {len(duplicates)} pair(s)",
            "=" * 60,
        ]

        for a, b in duplicates[:20]:  # Limit output
            lines.extend([
                "",
                f"  {a['date']} | {a['payee']} | {a['amount']} {a['currency']} | {a['narration'][:30]}",
                f"  {b['date']} | {b['payee']} | {b['amount']} {b['currency']} | {b['narration'][:30]}",
                "-" * 60,
            ])

        if len(duplicates) > 20:
            lines.append(f"\n  ... and {len(duplicates) - 20} more")

        await send_long_text(update, context, "\n".join(lines), "duplicates.txt")
    except Exception as e:
        log.error("Duplicates scan failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for finance audit commands."""
    cmd = msg.command
    args = msg.args or []

    if cmd == "reconcile":
        ledger_type, rest = parse_ledger_and_args(args)
        if len(rest) < 3:
            await reply.reply_text(
                "Usage: /reconcile [personal] <account-filter> <expected-balance> <currency>\n\n"
                "Example: /reconcile Cash:EUR:Wise 15234.56 EUR"
            )
            return True

        acct_filter = rest[0]
        try:
            expected = Decimal(rest[1])
        except Exception:
            await reply.reply_error(f"Invalid balance: {rest[1]}")
            return True
        currency = rest[2].upper()

        try:
            result_types, result_rows = run_bql_raw(
                ledger_type,
                f"SELECT account, sum(position) "
                f"WHERE account ~ '{acct_filter}' "
                f"GROUP BY account ORDER BY account",
            )

            lines = [
                f"Reconciliation ({ledger_type}) -- {acct_filter}",
                "=" * 55,
                f"Expected: {expected:,.2f} {currency}",
                "",
            ]

            actual_total = Decimal("0")
            for row in result_rows:
                account = str(row[0])
                inv = row[1]
                if hasattr(inv, "__iter__") and not isinstance(inv, str):
                    for item in inv:
                        s = str(item)
                        if currency in s:
                            parts = s.strip().split()
                            try:
                                amt = Decimal(parts[0])
                                lines.append(f"  {account:<40} {amt:>12,.2f} {currency}")
                                actual_total += amt
                            except Exception:
                                pass

            lines.extend([
                "",
                f"  Book balance:     {actual_total:>12,.2f} {currency}",
                f"  Expected balance: {expected:>12,.2f} {currency}",
            ])

            diff = actual_total - expected
            if abs(diff) < Decimal("0.01"):
                lines.append(f"\n  RECONCILED (difference: {diff:,.2f})")
            else:
                lines.append(f"\n  DISCREPANCY: {diff:>+,.2f} {currency}")

            await reply.reply_text("\n".join(lines))
        except Exception as e:
            log.error("Reconciliation failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "trial":
        ledger_type, rest = parse_ledger_and_args(args)
        as_of = rest[0] if rest else date.today().isoformat()

        try:
            result = run_bql(
                ledger_type,
                f"SELECT account, sum(convert(position, '{OPERATING_CURRENCY}')) AS balance "
                f"WHERE date <= {as_of} "
                f"GROUP BY account ORDER BY account",
            )

            result_types, result_rows = run_bql_raw(
                ledger_type,
                f"SELECT sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
                f"WHERE date <= {as_of}",
            )
            total_str = "0"
            if result_rows:
                inv = result_rows[0][0]
                if hasattr(inv, "__iter__") and not isinstance(inv, str):
                    parts = [str(item) for item in inv]
                    total_str = ", ".join(parts) if parts else "0"
                else:
                    total_str = str(inv) if inv else "0"

            header = f"Trial Balance ({ledger_type}) -- as of {as_of}"
            footer = f"\nTotal (should be ~0): {total_str}"
            await reply.reply_text(f"{header}\n{'=' * len(header)}\n\n{result}\n{footer}")
        except Exception as e:
            log.error("Trial balance failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "duplicates":
        ledger_type, _ = parse_ledger_and_args(args)

        try:
            from beancount.core import data as bc_data
            entries, _, _ = load_beancount(ledger_type)

            txns = []
            for entry in entries:
                if not isinstance(entry, bc_data.Transaction):
                    continue
                payee = entry.payee or ""
                for posting in entry.postings:
                    if posting.account.startswith("Expenses") or posting.account.startswith("Income"):
                        txns.append({
                            "date": entry.date,
                            "payee": payee.lower().strip(),
                            "amount": abs(posting.units.number),
                            "currency": posting.units.currency,
                            "narration": entry.narration or "",
                            "account": posting.account,
                        })
                        break

            duplicates = []
            for i in range(len(txns)):
                for j in range(i + 1, len(txns)):
                    a, b = txns[i], txns[j]
                    if (
                        a["payee"] and a["payee"] == b["payee"]
                        and a["amount"] == b["amount"]
                        and a["currency"] == b["currency"]
                        and abs((a["date"] - b["date"]).days) <= 3
                    ):
                        duplicates.append((a, b))

            if not duplicates:
                await reply.reply_text(f"No potential duplicates found ({ledger_type}).")
                return True

            lines = [
                f"Potential Duplicates ({ledger_type}) -- {len(duplicates)} pair(s)",
                "=" * 60,
            ]

            for a, b in duplicates[:20]:
                lines.extend([
                    "",
                    f"  {a['date']} | {a['payee']} | {a['amount']} {a['currency']} | {a['narration'][:30]}",
                    f"  {b['date']} | {b['payee']} | {b['amount']} {b['currency']} | {b['narration'][:30]}",
                    "-" * 60,
                ])

            if len(duplicates) > 20:
                lines.append(f"\n  ... and {len(duplicates) - 20} more")

            await reply.reply_text("\n".join(lines))
        except Exception as e:
            log.error("Duplicates scan failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    return False


def register(app: Application):
    """Register finance audit commands."""
    tid = get_finance_thread_id()

    app.add_handler(topic_command("reconcile", cmd_reconcile, thread_id=tid))
    app.add_handler(topic_command("trial", cmd_trial, thread_id=tid))
    app.add_handler(topic_command("duplicates", cmd_duplicates, thread_id=tid))

    log.info("Finance audit plugin loaded (thread: %s)", tid or "any")
