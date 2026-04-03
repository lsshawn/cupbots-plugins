"""
Finance FIRE — Financial Independence tracking

Commands (scoped to finance topic thread):
  /money              — Master dashboard (FIRE + tiers + allocation + actions)
  /money fire         — Detailed FIRE dashboard
  /money balances     — All balances by custodian
  /money tiers        — Cash tier breakdown
  /money budget       — Expense budget with FIRE multipliers
  /money growth       — Monthly net worth growth chart
  /money scenarios    — FIRE what-if scenarios
  /money networth     — Net worth (both ledgers)

  Legacy commands (still work):
  /fire, /portfolio, /cashtiers, /firebudget, /firescenario, /networth, /savings
"""

import asyncio
import json
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes

from cupbots.topic_filter import topic_command
from cupbots.helpers.db import get_db
from cupbots.helpers.logger import get_logger
from plugins.mdpubs_plugin import publish_or_fallback
from plugins._finance_helpers import (
    OPERATING_CURRENCY,
    get_finance_thread_id,
    run_bql_combined_raw,
    send_long_text,
)

log = get_logger("finance.fire")

from cupbots.config import get_config as _get_cfg
_note_root = Path(_get_cfg().get("allowed_paths", {}).get("notes", "/home/ss/projects/note"))
VENV_PY = str(_note_root / "venv" / "bin" / "python3")
PORTFOLIO_SYNC = str(_note_root / "finances" / "scripts" / "portfolio_sync.py")



def _text_to_markdown(text: str, title: str) -> str:
    """Convert plain-text report to markdown with frontmatter."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"---\n"
        f"title: \"{title}\"\n"
        f"mdpubs-hide-meta: true\n"
        f"---\n\n"
        f"*Last updated: {now}*\n\n"
        f"```\n{text}\n```\n"
    )


def _run_portfolio_sync(*args) -> dict:
    """Run portfolio_sync.py with given args and return parsed JSON."""
    cmd = [VENV_PY, PORTFOLIO_SYNC, "--json"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return json.loads(result.stdout)


def _run_portfolio_sync_text(*args) -> str:
    """Run portfolio_sync.py and return formatted text output (no --json)."""
    cmd = [VENV_PY, PORTFOLIO_SYNC] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    # Strip ANSI color codes for Telegram
    import re
    text = re.sub(r'\033\[[0-9;]*m', '', result.stdout)
    return text.strip()


def _sum_eur(inventory) -> Decimal:
    """Sum EUR amounts from an inventory."""
    total = Decimal("0")
    if inventory is None:
        return total
    if hasattr(inventory, "__iter__") and not isinstance(inventory, str):
        for item in inventory:
            parts = str(item).strip().split()
            if parts:
                try:
                    total += Decimal(parts[0])
                except Exception:
                    pass
    elif isinstance(inventory, Decimal):
        total = inventory
    return total


def _format_inv(inventory) -> str:
    """Format inventory as string."""
    if inventory is None:
        return "0"
    if hasattr(inventory, "__iter__") and not isinstance(inventory, str):
        parts = [str(item) for item in inventory]
        return ", ".join(parts) if parts else "0"
    return str(inventory)


async def cmd_networth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Net worth across both ledgers."""
    if not update.message:
        return
    args = context.args or []
    as_of = args[0] if args else date.today().isoformat()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        result_types, result_rows = run_bql_combined_raw(
            f"SELECT root(account, 2) AS category, sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
            f"WHERE (account ~ 'Assets' OR account ~ 'Liabilities') AND date <= {as_of} "
            f"GROUP BY category ORDER BY category"
        )

        total_assets = Decimal("0")
        total_liabilities = Decimal("0")
        asset_lines = []
        liability_lines = []

        for row in result_rows:
            category = str(row[0])
            amount = _sum_eur(row[1])
            if category.startswith("Assets"):
                asset_lines.append((category, amount))
                total_assets += amount
            elif category.startswith("Liabilities"):
                liability_lines.append((category, amount))
                total_liabilities += amount

        net_worth = total_assets + total_liabilities  # Liabilities are negative

        lines = [
            f"Net Worth — as of {as_of}",
            "=" * 55,
            "",
            "ASSETS",
            "-" * 55,
        ]
        for cat, amt in sorted(asset_lines):
            lines.append(f"  {cat:<40} {amt:>12,.2f}")
        lines.append(f"  {'TOTAL ASSETS':<40} {total_assets:>12,.2f} {OPERATING_CURRENCY}")

        if liability_lines:
            lines.extend(["", "LIABILITIES", "-" * 55])
            for cat, amt in sorted(liability_lines):
                lines.append(f"  {cat:<40} {amt:>12,.2f}")
            lines.append(f"  {'TOTAL LIABILITIES':<40} {total_liabilities:>12,.2f} {OPERATING_CURRENCY}")

        lines.extend([
            "",
            "=" * 55,
            f"  {'NET WORTH':<40} {net_worth:>12,.2f} {OPERATING_CURRENCY}",
        ])

        await send_long_text(update, context, "\n".join(lines), "networth.txt")
    except Exception as e:
        log.error("Net worth failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_savings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Savings rate calculation."""
    if not update.message:
        return
    args = context.args or []
    from plugins._finance_helpers import parse_date_range
    start, end, _ = parse_date_range(args)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        # Use personal ledger for savings rate (personal spending)
        from plugins._finance_helpers import run_bql_raw
        result_types, result_rows = run_bql_raw(
            "personal",
            f"SELECT root(account, 1) AS type, sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
            f"WHERE (account ~ 'Income' OR account ~ 'Expenses') "
            f"AND date >= {start} AND date < {end} "
            f"GROUP BY type ORDER BY type",
        )

        income = Decimal("0")
        expenses = Decimal("0")
        for row in result_rows:
            acct_type = str(row[0])
            amount = _sum_eur(row[1])
            if acct_type == "Income":
                income = -amount  # Income is negative in beancount
            elif acct_type == "Expenses":
                expenses = amount

        savings = income - expenses
        rate = (savings / income * 100) if income > 0 else Decimal("0")

        lines = [
            f"Savings Rate (personal) — {start} to {end}",
            "=" * 45,
            "",
            f"  Income:     {income:>12,.2f} {OPERATING_CURRENCY}",
            f"  Expenses:   {expenses:>12,.2f} {OPERATING_CURRENCY}",
            f"  Savings:    {savings:>12,.2f} {OPERATING_CURRENCY}",
            "",
            f"  Rate:       {rate:>11.1f}%",
        ]

        await send_long_text(update, context, "\n".join(lines), "savings.txt")
    except Exception as e:
        log.error("Savings rate failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live portfolio with prices and allocation."""
    if not update.message:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        data = await asyncio.to_thread(_run_portfolio_sync)
        total = data["total_usd"]

        lines = [
            f"Portfolio — ${total:,.0f} USD",
            f"as of {data['as_of']}",
            "=" * 50,
            "",
            "ALLOCATION",
            "-" * 50,
        ]
        for cls, info in sorted(data["by_asset_class"].items()):
            bar = "#" * int(info["pct"] / 5)
            lines.append(f"  {cls:<12} ${info['value_usd']:>10,.0f}  {info['pct']:>5.1f}%  {bar}")

        lines.extend(["", "BY CUSTODIAN", "-" * 50])
        for cust, val in data["by_custodian"].items():
            pct = val / total * 100 if total > 0 else 0
            lines.append(f"  {cust:<12} ${val:>10,.0f}  {pct:>5.1f}%")

        lines.extend(["", "TOP POSITIONS", "-" * 50])
        sorted_pos = sorted(
            [p for p in data["positions"] if not p.get("ticker", "").startswith(("CASH", "FD_"))],
            key=lambda p: -p["value_usd"],
        )
        for p in sorted_pos[:15]:
            pct = p["value_usd"] / total * 100 if total > 0 else 0
            lines.append(
                f"  {p['ticker']:<12} {p['shares']:>8.2f} x ${p['price']:>8.2f}  "
                f"${p['value_usd']:>9,.0f}  {pct:>4.1f}%"
            )

        await send_long_text(update, context, "\n".join(lines), "portfolio.txt")
    except Exception as e:
        log.error("Portfolio failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_fire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FIRE dashboard with NPER projections and live portfolio data."""
    if not update.message:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        data = await asyncio.to_thread(_run_portfolio_sync, "--fire")

        total = data["total_assets_usd"]
        portfolio = data["portfolio"]["total_usd"]
        other = data["other_assets_usd"]
        fire_target = data["fire_target"]
        fi_pct = data["fi_progress_pct"]

        lines = [
            "FIRE Dashboard",
            "=" * 50,
            "",
            f"  Portfolio:           ${portfolio:>12,.0f}",
            f"  Other Assets:        ${other:>12,.0f}",
            f"  Total Assets:        ${total:>12,.0f}",
            "",
            f"  Annual Income:       ${data['annual_income_usd']:>12,.0f}",
            f"  Annual Expenses:     ${data['annual_expense_usd']:>12,.0f}",
            f"  Annual Savings:      ${data['annual_savings_usd']:>12,.0f}",
            f"  Savings Rate:        {data['savings_rate']:>11.1f}%",
            "",
            f"  FIRE Target:         ${fire_target:>12,.0f}",
            f"  Lean FIRE (80%):     ${data['lean_fire']:>12,.0f}",
            f"  Fat FIRE (120%):     ${data['fat_fire']:>12,.0f}",
            f"  FI Progress:         {fi_pct:>11.1f}%",
            "",
            f"  Years to Lean FIRE:  {data['years_to_lean']:>11}",
            f"  Years to FIRE:       {data['years_to_fire']:>11}",
            f"  Years to Fat FIRE:   {data['years_to_fat']:>11}",
            "",
            f"  Real Return:         {data['real_return']*100:>10.1f}%",
            f"  Withdrawal Rate:     {data['withdrawal_rate']*100:>10.1f}%",
        ]

        await send_long_text(update, context, "\n".join(lines), "fire.txt")
    except Exception as e:
        log.error("FIRE dashboard failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_cashtiers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cash tier analysis — emergency fund, operating buffer, trip fund."""
    if not update.message:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        data = await asyncio.to_thread(_run_portfolio_sync, "--tiers")

        lines = ["Cash Tiers", "=" * 50, ""]

        for tier_name, tier in data.items():
            target = tier["target_usd"]
            actual = tier["actual_usd"]
            excess = tier["excess_usd"]
            status = "OK" if excess >= 0 else "LOW"

            lines.append(f"{tier['description']} ({tier_name})")
            lines.append(f"  Target:  ${target:>10,.0f}")
            lines.append(f"  Actual:  ${actual:>10,.0f}")
            lines.append(f"  Excess:  ${excess:>10,.0f}  [{status}]")
            for p in tier.get("positions", []):
                lines.append(f"    - {p['name']}: ${p['value_usd']:,.0f}")
            lines.append("")

        await send_long_text(update, context, "\n".join(lines), "cashtiers.txt")
    except Exception as e:
        log.error("Cash tiers failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_firebudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Annual expense budget with FIRE multipliers."""
    if not update.message:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        data = await asyncio.to_thread(_run_portfolio_sync, "--fire")
        breakdown = data["expense_breakdown"]

        lines = [
            "FIRE Expense Budget",
            "=" * 55,
            f"{'Category':<22} {'Annual':>10} {'x':>3} {'FIRE':>12}",
            "-" * 55,
        ]

        total_annual = 0
        total_fire = 0
        current_cat = ""
        for item in breakdown:
            if item["category"] != current_cat:
                current_cat = item["category"]
                lines.append(f"\n{current_cat.upper()}")
            lines.append(
                f"  {item['name']:<20} ${item['annual_usd']:>8,.0f} {item['fire_multiplier']:>3}x ${item['fire_usd']:>10,.0f}"
            )
            total_annual += item["annual_usd"]
            total_fire += item["fire_usd"]

        lines.extend([
            "",
            "-" * 55,
            f"  {'TOTAL':<20} ${total_annual:>8,.0f}      ${total_fire:>10,.0f}",
            f"  {'Monthly':<20} ${total_annual/12:>8,.0f}      ${total_fire/12:>10,.0f}",
        ])

        await send_long_text(update, context, "\n".join(lines), "firebudget.txt")
    except Exception as e:
        log.error("FIRE budget failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def cmd_firescenario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FIRE what-if scenario analysis."""
    if not update.message:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /firescenario <change>\n"
            "Examples:\n"
            "  /firescenario +200000 exit\n"
            "  /firescenario +20000 income\n"
            "  /firescenario +2 return\n"
            "  /firescenario -10000 expense"
        )
        return

    scenario = " ".join(args)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        data = await asyncio.to_thread(_run_portfolio_sync, "--scenario", scenario)

        if "error" in data:
            await update.message.reply_text(f"Error: {data['error']}")
            return

        lines = [
            "Scenario Analysis",
            "=" * 40,
            "",
            f"  Scenario:      {data['scenario']}",
            f"  Base years:    {data['base_years']}",
            f"  New years:     {data['new_years']}",
            f"  Years saved:   {data['years_saved']}",
            f"  Reduction:     {data['pct_reduction']}%",
        ]

        await send_long_text(update, context, "\n".join(lines))
    except Exception as e:
        log.error("Scenario failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


MONEY_SUBCOMMANDS = {
    "fire": "--fire",
    "balances": "--balances",
    "tiers": "--tiers",
    "budget": "--budget",
    "growth": "--growth",
    "scenarios": "--scenarios",
    "networth": "--networth",
}

# Subcommands that get published to mdpubs (not scenarios — those are ad-hoc)
MONEY_PUBLISH_TITLES = {
    None: ("money_dashboard", "Financial Dashboard"),
    "fire": ("money_fire", "FIRE Dashboard"),
    "balances": ("money_balances", "Balances by Custodian"),
    "tiers": ("money_tiers", "Cash Tiers"),
    "budget": ("money_budget", "FIRE Expense Budget"),
    "growth": ("money_growth", "Net Worth Growth"),
    "scenarios": ("money_scenarios", "FIRE Scenarios"),
    "networth": ("money_networth", "Net Worth"),
}

MONEY_HELP = """💰 /money — Financial dashboard

/money          Master dashboard
/money fire     FIRE detail
/money balances All balances by bank
/money tiers    Cash tier breakdown
/money budget   Expense budget
/money growth   Net worth growth chart
/money scenarios FIRE what-if scenarios
/money networth Net worth (both ledgers)

Reports are published to mdpubs.com — only the link is sent here."""


async def cmd_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified finance command — master dashboard + subcommands."""
    if not update.message:
        return

    args = context.args or []
    sub = args[0].lower() if args else None

    if sub == "help":
        await update.message.reply_text(MONEY_HELP)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    try:
        if sub and sub in MONEY_SUBCOMMANDS:
            flag = MONEY_SUBCOMMANDS[sub]
            extra_args = args[1:] if len(args) > 1 else []
            if sub == "networth" and extra_args:
                text = await asyncio.to_thread(_run_portfolio_sync_text, flag, extra_args[0])
            else:
                text = await asyncio.to_thread(_run_portfolio_sync_text, flag)
        elif sub:
            # Unknown subcommand — try as scenario
            scenario = " ".join(args)
            text = await asyncio.to_thread(_run_portfolio_sync_text, "--scenario", scenario)
        else:
            # Default: master dashboard
            text = await asyncio.to_thread(_run_portfolio_sync_text)

        # Publish to mdpubs and send link only (no full text in Telegram)
        publish_info = MONEY_PUBLISH_TITLES.get(sub)
        if publish_info:
            key, title = publish_info
            md = _text_to_markdown(text, title)
            url, _ = await publish_or_fallback(key, title, md, tags=["finance", key])
            if url:
                await update.message.reply_text(f"📄 {url}")
            else:
                await send_long_text(update, context, text, "money.txt")
        else:
            # No mdpubs for this subcommand (e.g. ad-hoc scenarios) — send text
            await send_long_text(update, context, text, "money.txt")
    except Exception as e:
        log.error("Money command failed: %s", e)
        await update.message.reply_text(f"Error: {e}")


async def handle_command(msg, reply) -> bool:
    """Cross-platform command handler for FIRE tracking."""
    cmd = msg.command
    args = msg.args or []

    if cmd == "money":
        try:
            sub = args[0].lower() if args else None

            if sub == "help":
                help_text = (
                    "/money -- Financial dashboard\n\n"
                    "/money          Master dashboard\n"
                    "/money fire     FIRE detail\n"
                    "/money balances All balances by bank\n"
                    "/money tiers    Cash tier breakdown\n"
                    "/money budget   Expense budget\n"
                    "/money growth   Net worth growth chart\n"
                    "/money scenarios FIRE what-if scenarios\n"
                    "/money networth Net worth (both ledgers)"
                )
                await reply.reply_text(help_text)
                return True

            if sub and sub in MONEY_SUBCOMMANDS:
                flag = MONEY_SUBCOMMANDS[sub]
                extra_args = args[1:] if len(args) > 1 else []
                if sub == "networth" and extra_args:
                    text = _run_portfolio_sync_text(flag, extra_args[0])
                else:
                    text = _run_portfolio_sync_text(flag)
            elif sub:
                scenario = " ".join(args)
                text = _run_portfolio_sync_text("--scenario", scenario)
            else:
                text = _run_portfolio_sync_text()

            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            await reply.reply_text(text)
        except Exception as e:
            log.error("Money command failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "networth":
        try:
            as_of = args[0] if args else date.today().isoformat()
            result_types, result_rows = run_bql_combined_raw(
                f"SELECT root(account, 2) AS category, sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
                f"WHERE (account ~ 'Assets' OR account ~ 'Liabilities') AND date <= {as_of} "
                f"GROUP BY category ORDER BY category"
            )

            total_assets = Decimal("0")
            total_liabilities = Decimal("0")
            asset_lines = []
            liability_lines = []
            for row in result_rows:
                category = str(row[0])
                amount = _sum_eur(row[1])
                if category.startswith("Assets"):
                    asset_lines.append((category, amount))
                    total_assets += amount
                elif category.startswith("Liabilities"):
                    liability_lines.append((category, amount))
                    total_liabilities += amount

            net_worth = total_assets + total_liabilities

            lines = [
                f"Net Worth -- as of {as_of}",
                "=" * 55, "",
                "ASSETS", "-" * 55,
            ]
            for cat, amt in sorted(asset_lines):
                lines.append(f"  {cat:<40} {amt:>12,.2f}")
            lines.append(f"  {'TOTAL ASSETS':<40} {total_assets:>12,.2f} {OPERATING_CURRENCY}")

            if liability_lines:
                lines.extend(["", "LIABILITIES", "-" * 55])
                for cat, amt in sorted(liability_lines):
                    lines.append(f"  {cat:<40} {amt:>12,.2f}")
                lines.append(f"  {'TOTAL LIABILITIES':<40} {total_liabilities:>12,.2f} {OPERATING_CURRENCY}")

            lines.extend([
                "", "=" * 55,
                f"  {'NET WORTH':<40} {net_worth:>12,.2f} {OPERATING_CURRENCY}",
            ])
            text = "\n".join(lines)
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            await reply.reply_text(text)
        except Exception as e:
            log.error("Net worth failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "savings":
        try:
            from plugins._finance_helpers import parse_date_range, run_bql_raw
            start, end, _ = parse_date_range(args)
            result_types, result_rows = run_bql_raw(
                "personal",
                f"SELECT root(account, 1) AS type, sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
                f"WHERE (account ~ 'Income' OR account ~ 'Expenses') "
                f"AND date >= {start} AND date < {end} "
                f"GROUP BY type ORDER BY type",
            )

            income = Decimal("0")
            expenses = Decimal("0")
            for row in result_rows:
                acct_type = str(row[0])
                amount = _sum_eur(row[1])
                if acct_type == "Income":
                    income = -amount
                elif acct_type == "Expenses":
                    expenses = amount

            savings = income - expenses
            rate = (savings / income * 100) if income > 0 else Decimal("0")

            lines = [
                f"Savings Rate (personal) -- {start} to {end}",
                "=" * 45, "",
                f"  Income:     {income:>12,.2f} {OPERATING_CURRENCY}",
                f"  Expenses:   {expenses:>12,.2f} {OPERATING_CURRENCY}",
                f"  Savings:    {savings:>12,.2f} {OPERATING_CURRENCY}",
                "",
                f"  Rate:       {rate:>11.1f}%",
            ]
            await reply.reply_text("\n".join(lines))
        except Exception as e:
            log.error("Savings rate failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "portfolio":
        try:
            data = _run_portfolio_sync()
            total = data["total_usd"]

            lines = [
                f"Portfolio -- ${total:,.0f} USD",
                f"as of {data['as_of']}",
                "=" * 50, "",
                "ALLOCATION", "-" * 50,
            ]
            for cls, info in sorted(data["by_asset_class"].items()):
                bar = "#" * int(info["pct"] / 5)
                lines.append(f"  {cls:<12} ${info['value_usd']:>10,.0f}  {info['pct']:>5.1f}%  {bar}")

            lines.extend(["", "BY CUSTODIAN", "-" * 50])
            for cust, val in data["by_custodian"].items():
                pct = val / total * 100 if total > 0 else 0
                lines.append(f"  {cust:<12} ${val:>10,.0f}  {pct:>5.1f}%")

            lines.extend(["", "TOP POSITIONS", "-" * 50])
            sorted_pos = sorted(
                [p for p in data["positions"] if not p.get("ticker", "").startswith(("CASH", "FD_"))],
                key=lambda p: -p["value_usd"],
            )
            for p in sorted_pos[:15]:
                pct = p["value_usd"] / total * 100 if total > 0 else 0
                lines.append(
                    f"  {p['ticker']:<12} {p['shares']:>8.2f} x ${p['price']:>8.2f}  "
                    f"${p['value_usd']:>9,.0f}  {pct:>4.1f}%"
                )

            text = "\n".join(lines)
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            await reply.reply_text(text)
        except Exception as e:
            log.error("Portfolio failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "fire":
        try:
            data = _run_portfolio_sync("--fire")

            total = data["total_assets_usd"]
            portfolio = data["portfolio"]["total_usd"]
            other = data["other_assets_usd"]
            fire_target = data["fire_target"]
            fi_pct = data["fi_progress_pct"]

            lines = [
                "FIRE Dashboard",
                "=" * 50, "",
                f"  Portfolio:           ${portfolio:>12,.0f}",
                f"  Other Assets:        ${other:>12,.0f}",
                f"  Total Assets:        ${total:>12,.0f}",
                "",
                f"  Annual Income:       ${data['annual_income_usd']:>12,.0f}",
                f"  Annual Expenses:     ${data['annual_expense_usd']:>12,.0f}",
                f"  Annual Savings:      ${data['annual_savings_usd']:>12,.0f}",
                f"  Savings Rate:        {data['savings_rate']:>11.1f}%",
                "",
                f"  FIRE Target:         ${fire_target:>12,.0f}",
                f"  Lean FIRE (80%):     ${data['lean_fire']:>12,.0f}",
                f"  Fat FIRE (120%):     ${data['fat_fire']:>12,.0f}",
                f"  FI Progress:         {fi_pct:>11.1f}%",
                "",
                f"  Years to Lean FIRE:  {data['years_to_lean']:>11}",
                f"  Years to FIRE:       {data['years_to_fire']:>11}",
                f"  Years to Fat FIRE:   {data['years_to_fat']:>11}",
                "",
                f"  Real Return:         {data['real_return']*100:>10.1f}%",
                f"  Withdrawal Rate:     {data['withdrawal_rate']*100:>10.1f}%",
            ]
            await reply.reply_text("\n".join(lines))
        except Exception as e:
            log.error("FIRE dashboard failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "cashtiers":
        try:
            data = _run_portfolio_sync("--tiers")

            lines = ["Cash Tiers", "=" * 50, ""]
            for tier_name, tier in data.items():
                target = tier["target_usd"]
                actual = tier["actual_usd"]
                excess = tier["excess_usd"]
                status = "OK" if excess >= 0 else "LOW"

                lines.append(f"{tier['description']} ({tier_name})")
                lines.append(f"  Target:  ${target:>10,.0f}")
                lines.append(f"  Actual:  ${actual:>10,.0f}")
                lines.append(f"  Excess:  ${excess:>10,.0f}  [{status}]")
                for p in tier.get("positions", []):
                    lines.append(f"    - {p['name']}: ${p['value_usd']:,.0f}")
                lines.append("")

            text = "\n".join(lines)
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            await reply.reply_text(text)
        except Exception as e:
            log.error("Cash tiers failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "firebudget":
        try:
            data = _run_portfolio_sync("--fire")
            breakdown = data["expense_breakdown"]

            lines = [
                "FIRE Expense Budget",
                "=" * 55,
                f"{'Category':<22} {'Annual':>10} {'x':>3} {'FIRE':>12}",
                "-" * 55,
            ]

            total_annual = 0
            total_fire = 0
            current_cat = ""
            for item in breakdown:
                if item["category"] != current_cat:
                    current_cat = item["category"]
                    lines.append(f"\n{current_cat.upper()}")
                lines.append(
                    f"  {item['name']:<20} ${item['annual_usd']:>8,.0f} {item['fire_multiplier']:>3}x ${item['fire_usd']:>10,.0f}"
                )
                total_annual += item["annual_usd"]
                total_fire += item["fire_usd"]

            lines.extend([
                "", "-" * 55,
                f"  {'TOTAL':<20} ${total_annual:>8,.0f}      ${total_fire:>10,.0f}",
                f"  {'Monthly':<20} ${total_annual/12:>8,.0f}      ${total_fire/12:>10,.0f}",
            ])

            text = "\n".join(lines)
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            await reply.reply_text(text)
        except Exception as e:
            log.error("FIRE budget failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "firescenario":
        if not args:
            await reply.reply_text(
                "Usage: /firescenario <change>\n"
                "Examples:\n"
                "  /firescenario +200000 exit\n"
                "  /firescenario +20000 income\n"
                "  /firescenario +2 return\n"
                "  /firescenario -10000 expense"
            )
            return True

        try:
            scenario = " ".join(args)
            data = _run_portfolio_sync("--scenario", scenario)

            if "error" in data:
                await reply.reply_error(f"Error: {data['error']}")
                return True

            lines = [
                "Scenario Analysis",
                "=" * 40, "",
                f"  Scenario:      {data['scenario']}",
                f"  Base years:    {data['base_years']}",
                f"  New years:     {data['new_years']}",
                f"  Years saved:   {data['years_saved']}",
                f"  Reduction:     {data['pct_reduction']}%",
            ]
            await reply.reply_text("\n".join(lines))
        except Exception as e:
            log.error("Scenario failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    return False


def register(app: Application):
    """Register FIRE tracking commands."""
    tid = get_finance_thread_id()

    # Primary unified command
    app.add_handler(topic_command("money", cmd_money, thread_id=tid))

    # Legacy commands (still work)
    app.add_handler(topic_command("networth", cmd_networth, thread_id=tid))
    app.add_handler(topic_command("savings", cmd_savings, thread_id=tid))
    app.add_handler(topic_command("portfolio", cmd_portfolio, thread_id=tid))
    app.add_handler(topic_command("fire", cmd_fire, thread_id=tid))
    app.add_handler(topic_command("cashtiers", cmd_cashtiers, thread_id=tid))
    app.add_handler(topic_command("firebudget", cmd_firebudget, thread_id=tid))
    app.add_handler(topic_command("firescenario", cmd_firescenario, thread_id=tid))

    log.info("Finance FIRE plugin loaded (thread: %s)", tid or "any")
