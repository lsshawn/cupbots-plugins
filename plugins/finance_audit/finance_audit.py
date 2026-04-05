"""
Finance Audit — Auditing and reconciliation tools.

Commands:
  /reconcile [personal] <account> <balance> <currency>  — Compare book vs expected
  /trial [personal] [date]                              — Trial balance
  /duplicates [personal]                                — Scan for duplicate entries
"""

from datetime import date
from decimal import Decimal

from cupbots.helpers.logger import get_logger
from cupbots.helpers.access import is_admin
from plugins._finance_helpers import (
    OPERATING_CURRENCY,
    load_beancount,
    parse_ledger_and_args,
    run_bql,
    run_bql_raw,
    send_long_text,
)

log = get_logger("finance.audit")

COMMANDS = ("reconcile", "trial", "duplicates")


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for finance audit commands."""
    cmd = msg.command
    if cmd not in COMMANDS:
        return False

    args = msg.args or []

    # --help support
    if args and args[0] in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    # Access control: admin-only or role-gated (framework role check runs first,
    # but we add explicit admin gate for safety)
    if not is_admin(msg.platform, msg.sender_id) and not msg.sender_role:
        await reply.reply_text("Finance commands are restricted.")
        return True

    if cmd == "reconcile":
        await _reconcile(msg, reply, args)
        return True

    if cmd == "trial":
        await _trial(msg, reply, args)
        return True

    if cmd == "duplicates":
        await _duplicates(msg, reply, args)
        return True

    return False


async def _reconcile(msg, reply, args):
    """Compare book balance vs expected balance for an account."""
    ledger_type, rest = parse_ledger_and_args(args)

    if len(rest) < 3:
        await reply.reply_text(
            "Usage: /reconcile [personal] <account-filter> <expected-balance> <currency>\n\n"
            "Example: /reconcile Cash:EUR:Wise 15234.56 EUR"
        )
        return

    acct_filter = rest[0]
    try:
        expected = Decimal(rest[1])
    except Exception:
        await reply.reply_error(f"Invalid balance: {rest[1]}")
        return
    currency = rest[2].upper()

    await reply.send_typing()
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

        await send_long_text(reply, "\n".join(lines), "reconcile.txt")
    except Exception as e:
        log.error("Reconciliation failed: %s", e)
        await reply.reply_error(f"Error: {e}")


async def _trial(msg, reply, args):
    """Trial balance — all accounts, should sum to zero."""
    ledger_type, rest = parse_ledger_and_args(args)
    as_of = rest[0] if rest else date.today().isoformat()

    await reply.send_typing()
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
        await send_long_text(reply, f"{header}\n{'=' * len(header)}\n\n{result}\n{footer}", "trial.txt")
    except Exception as e:
        log.error("Trial balance failed: %s", e)
        await reply.reply_error(f"Error: {e}")


async def _duplicates(msg, reply, args):
    """Scan for potential duplicate entries."""
    ledger_type, _ = parse_ledger_and_args(args)

    await reply.send_typing()
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
            return

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

        await send_long_text(reply, "\n".join(lines), "duplicates.txt")
    except Exception as e:
        log.error("Duplicates scan failed: %s", e)
        await reply.reply_error(f"Error: {e}")
