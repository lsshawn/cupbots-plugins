"""
Finance — /money command hub for all read/query/report operations.

Commands:
  /money                    Master FIRE dashboard
  /money fire               FIRE metrics and projections
  /money balances           All balances by custodian
  /money tiers              Cash tier breakdown
  /money budget             Expense budget with FIRE multipliers
  /money growth             Net worth growth chart
  /money scenarios <change> FIRE what-if scenarios
  /money networth [date]    Net worth (both ledgers)
  /money search <query>     AI-powered journal search
  /money query <BQL>        Raw BQL query
  /money bal [filter]       Account balances
  /money accounts [filter]  Chart of accounts
  /money pnl [period]       Profit & Loss
  /money bs [date]          Balance Sheet
  /money cashflow [period]  Cash flow summary
  /money fxgain [date]      FX gain/loss report
  /money receivables        Outstanding receivables
  /money payables           Outstanding liabilities
  /money annual [year]      Annual report
  /money tax [year]         Tax summary (MY)
  /money taxrelief [year]   Tax relief (MY)
  /money trial [date]       Trial balance
  /money wise [period]      Wise account queries
  /money invoice list       List invoices
  /money invoice status <id> Check invoice status
  /money invoice accounts   List Stripe accounts
"""

import asyncio
import subprocess
from copy import copy
from datetime import datetime
from pathlib import Path

from cupbots.helpers.logger import get_logger
from cupbots.helpers.access import is_admin
from plugins._finance_helpers import send_long_text

log = get_logger("finance.fire")

from cupbots.config import get_config as _get_cfg
_note_root = Path(_get_cfg().get("allowed_paths", {}).get("notes", "/home/ss/projects/note"))
VENV_PY = str(_note_root / "venv" / "bin" / "python3")
PORTFOLIO_SYNC = str(_note_root / "finances" / "scripts" / "portfolio_sync.py")

COMMANDS = ("money",)

FIRE_SUBCOMMANDS = {
    "fire": "--fire",
    "balances": "--balances",
    "tiers": "--tiers",
    "budget": "--budget",
    "growth": "--growth",
    "scenarios": "--scenarios",
    "networth": "--networth",
}

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

# Map /money subcommands to (target_plugin_module, rewritten_command)
# The target plugin's handle_command is called with msg.command rewritten.
_DELEGATED_SUBCOMMANDS = {
    # finance_query
    "search": ("finance_query", "fsearch"),
    "query": ("finance_query", "fquery"),
    "bal": ("finance_query", "fbal"),
    "accounts": ("finance_query", "faccount"),
    # finance_reports
    "pnl": ("finance_reports", "pnl"),
    "bs": ("finance_reports", "bs"),
    "cashflow": ("finance_reports", "cashflow"),
    "fxgain": ("finance_reports", "fxgain"),
    "receivables": ("finance_reports", "receivables"),
    "payables": ("finance_reports", "payables"),
    "annual": ("finance_reports", "annualreport"),
    "tax": ("finance_reports", "taxsummary"),
    "taxrelief": ("finance_reports", "taxrelief"),
    # finance_audit
    "trial": ("finance_audit", "trial"),
    # finance_wise
    "wise": ("finance_wise", "wise"),
    # invoice (read-only subcommands)
    "invoice": ("invoice", "invoice"),
}

MONEY_HELP = """/money — Financial dashboard & queries

FIRE & portfolio:
  /money                    Master dashboard
  /money fire               FIRE metrics and projections
  /money balances           All balances by custodian
  /money tiers              Cash tier breakdown
  /money budget             Expense budget × FIRE multipliers
  /money growth             Net worth growth chart
  /money scenarios <change> What-if (e.g. +200000 exit)
  /money networth [date]    Net worth (both ledgers)

Query:
  /money search <query>     AI journal search (natural language)
  /money query <BQL>        Raw BQL query
  /money bal [filter]       Account balances
  /money accounts [filter]  Chart of accounts

Reports:
  /money pnl [period]       Profit & Loss
  /money bs [date]          Balance Sheet
  /money cashflow [period]  Cash flow summary
  /money fxgain [date]      FX gain/loss
  /money receivables        Outstanding receivables
  /money payables           Outstanding liabilities
  /money annual [year]      Annual report
  /money tax [year]         Tax summary (MY)
  /money taxrelief [year]   Tax relief (MY)
  /money trial [date]       Trial balance
  /money wise [period]      Wise account queries
  /money invoice list       List invoices

Add [personal] after any subcommand for personal ledger."""


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


def _run_portfolio_sync_text(*args) -> str:
    """Run portfolio_sync.py and return formatted text output (no --json)."""
    import re
    cmd = [VENV_PY, PORTFOLIO_SYNC] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    text = re.sub(r'\033\[[0-9;]*m', '', result.stdout)
    return text.strip()


async def _delegate(sub: str, msg, reply, remaining_args: list) -> bool:
    """Delegate a /money subcommand to another plugin by rewriting msg.command/args."""
    target_module, target_cmd = _DELEGATED_SUBCOMMANDS[sub]

    try:
        import importlib
        mod = importlib.import_module(f"plugins.{target_module}.{target_module}")
    except ImportError:
        await reply.reply_error(f"Plugin {target_module} not available.")
        return True

    handler = getattr(mod, "handle_command", None)
    if not handler:
        await reply.reply_error(f"Plugin {target_module} has no handler.")
        return True

    # For /money invoice, pass subcommand args (list, status, accounts)
    # For /money wise, pass remaining as wise subcommands
    delegated_msg = copy(msg)
    delegated_msg.command = target_cmd
    delegated_msg.args = remaining_args
    return await handler(delegated_msg, reply)


async def handle_command(msg, reply) -> bool:
    """Hub command handler — routes /money subcommands to appropriate plugins."""
    cmd = msg.command
    if cmd not in COMMANDS:
        return False

    args = msg.args or []

    # --help support
    if args and args[0] in ("--help", "-h", "help"):
        await reply.reply_text(MONEY_HELP)
        return True

    # Access control
    if not is_admin(msg.platform, msg.sender_id) and not msg.sender_role:
        await reply.reply_text("Finance commands are restricted.")
        return True

    sub = args[0].lower() if args else None
    remaining = args[1:] if args else []

    # Delegate to other plugins
    if sub and sub in _DELEGATED_SUBCOMMANDS:
        return await _delegate(sub, msg, reply, remaining)

    # Native FIRE subcommands
    await _money_fire(reply, args)
    return True


async def _money_fire(reply, args):
    """FIRE dashboard + portfolio subcommands."""
    sub = args[0].lower() if args else None

    await reply.send_typing()
    try:
        if sub and sub in FIRE_SUBCOMMANDS:
            flag = FIRE_SUBCOMMANDS[sub]
            extra_args = args[1:] if len(args) > 1 else []
            if sub == "networth" and extra_args:
                text = await asyncio.to_thread(_run_portfolio_sync_text, flag, extra_args[0])
            else:
                text = await asyncio.to_thread(_run_portfolio_sync_text, flag)
        elif sub:
            scenario = " ".join(args)
            text = await asyncio.to_thread(_run_portfolio_sync_text, "--scenario", scenario)
        else:
            text = await asyncio.to_thread(_run_portfolio_sync_text)

        # Publish to mdpubs and send link
        publish_info = MONEY_PUBLISH_TITLES.get(sub)
        if publish_info:
            try:
                from plugins.mdpubs.mdpubs import publish_or_fallback
                key, title = publish_info
                md = _text_to_markdown(text, title)
                url, _ = await publish_or_fallback(key, title, md, tags=["finance", key])
                if url:
                    await reply.reply_text(f"{url}")
                    return
            except Exception:
                pass

        await send_long_text(reply, text, "money.txt")
    except Exception as e:
        log.error("Money command failed: %s", e)
        await reply.reply_error(f"Error: {e}")
