"""
Finance Reports — Beancount financial reports.

Commands:
  /pnl [personal] [period]        — Profit & Loss (jan, q1, ytd, last-3m, 2025, 2025-03...)
  /bs [personal] [date]           — Balance Sheet
  /cashflow [personal] [period]   — Cash flow summary (same periods as /pnl)
  /fxgain [personal] [date]       — FX gain/loss report
  /receivables                    — Outstanding receivables
  /payables [personal]            — Outstanding liabilities
  /annualreport [personal] [year] — Annual report (balance sheet + P&L)
  /taxrelief [year]               — Malaysian tax relief summary (personal ledger, LHDN)
  /taxsummary [year]              — Full income tax summary for BE form filing (personal)
"""

import json
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from cupbots.helpers.logger import get_logger
from cupbots.helpers.access import is_admin
from plugins._finance_helpers import (
    FINANCES_DIR,
    OPERATING_CURRENCY,
    PERIOD_HELP,
    load_beancount,
    parse_date_range,
    parse_ledger_and_args,
    run_bql,
    run_bql_raw,
    send_long_text,
)

log = get_logger("finance.reports")

COMMANDS = ("pnl", "bs", "cashflow", "fxgain", "receivables", "payables", "annualreport", "taxrelief", "taxsummary")


# --- Helpers ---

def _parse_position(position) -> tuple[Decimal, str]:
    """Parse a beancount position/inventory into (amount, currency)."""
    if position is None:
        return Decimal("0"), ""
    if isinstance(position, Decimal):
        return position, ""
    import re
    s = str(position).strip().strip("()")
    # Handle multiple positions like "1234.00 MYR, 500.00 USD" — take first non-zero
    for part in s.split(","):
        tokens = part.strip().split()
        if len(tokens) >= 2:
            try:
                clean = re.sub(r"[^0-9.\-]", "", tokens[0])
                amt = Decimal(clean)
                cur = tokens[1].strip("()")
                if amt != 0:
                    return amt, cur
            except Exception:
                pass
    return Decimal("0"), ""


def _sum_eur_from_inventory(inventory) -> Decimal:
    """Sum EUR amounts from a beancount inventory/position result."""
    total = Decimal("0")
    if inventory is None:
        return total
    if hasattr(inventory, '__iter__') and not isinstance(inventory, str):
        for item in inventory:
            s = str(item)
            # Parse "123.45 EUR" or "-123.45 EUR"
            parts = s.strip().split()
            if len(parts) >= 1:
                try:
                    total += Decimal(parts[0])
                except Exception:
                    pass
    elif isinstance(inventory, Decimal):
        total = inventory
    return total


def _format_inventory(inventory) -> str:
    """Format a beancount inventory as a readable string."""
    if inventory is None:
        return "0"
    if hasattr(inventory, '__iter__') and not isinstance(inventory, str):
        parts = [str(item) for item in inventory]
        return ", ".join(parts) if parts else "0"
    return str(inventory)


def _report_to_markdown(report: str, company: str, year: int, notes: dict | None = None) -> str:
    """Convert plain-text annual report to clean markdown for mdpubs."""
    import re

    lines = report.splitlines()

    # Parse sections
    sections = {"balance_sheet": [], "income_statement": [], "breakdown": []}
    current = None
    warning = ""
    for line in lines:
        if "BALANCE SHEET" in line:
            current = "balance_sheet"
            continue
        elif "INCOME STATEMENT" in line:
            current = "income_statement"
            continue
        elif line.strip() == "BREAKDOWN":
            current = "breakdown"
            continue
        elif line.startswith("Source:") or line.startswith("==="):
            continue
        elif "off by" in line:
            warning = line.strip()
            continue
        if current:
            sections[current].append(line)

    def _parse_line(line):
        stripped = line.strip()
        match = re.match(r'^(\s*)(.*?)\s{2,}(-?[\d,.]+)$', line)
        if match:
            indent = len(match.group(1)) // 4
            return indent, match.group(2).strip(), match.group(3).strip()
        indent = (len(line) - len(line.lstrip())) // 4
        return indent, stripped, ""

    md = []
    md.append(f"---\ntitle: \"{company} — Annual Report {year}\"\ndescription: \"Balance Sheet & Income Statement as at 31.12.{year}\"\nmdpubs-hide-meta: true\n---\n")

    # TOC
    md.append("<!-- toc -->")
    md.append(f"- [Balance Sheet](#balance-sheet)")
    md.append(f"- [Income Statement](#income-statement)")
    md.append(f"- [Detailed Breakdown](#detailed-breakdown)")
    md.append("<!-- /toc -->\n")

    # --- Balance Sheet ---
    md.append(f"## Balance Sheet")
    md.append(f"*As at 31.12.{year} — In Euros*\n")

    skip_bs = {"Financial investments", "Inventories", "Biological assets",
               "Investments in subsidiaries and associates", "Investment property",
               "Property, plant and equipment", "Intangible assets",
               "Loan liabilities", "Provisions", "Government grants",
               "Unregistered equity", "Unpaid capital", "Share premium",
               "Treasury shares", "Statutory reserve capital", "Other reserves",
               "Other equity"}

    md.append("| | EUR |")
    md.append("|:---|---:|")
    for line in sections["balance_sheet"]:
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        indent, label, amount = _parse_line(line)
        if label in skip_bs and not amount:
            continue
        if label in ("Non-current assets", "Non-current liabilities"):
            continue
        if "Total non-current" in label and amount == "0.00":
            continue

        is_total = "Total" in label
        pad = "&nbsp;&nbsp;&nbsp;" * indent
        if is_total:
            md.append(f"| **{pad}{label}** | **{amount}** |")
        elif amount:
            md.append(f"| {pad}{label} | {amount} |")
        else:
            md.append(f"| {pad}{label} | |")

    md.append("")

    # --- Income Statement ---
    md.append(f"## Income Statement")
    md.append(f"*Year ended 31.12.{year} — In Euros*\n")

    skip_is = {"Changes in inventories of agricultural production",
               "Profit (loss) from biological assets",
               "Changes in inventories of finished goods and WIP",
               "Work performed by entity and capitalised",
               "Depreciation and impairment loss (reversal)",
               "Significant impairment of current assets",
               "Profit (loss) from subsidiaries",
               "Profit (loss) from associates",
               "Gain (loss) from financial investments",
               "Interest income", "Interest expenses",
               "Other financial income and expense",
               "Income tax expense"}

    md.append("| | EUR |")
    md.append("|:---|---:|")
    for line in sections["income_statement"]:
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        if stripped in skip_is:
            continue
        indent, label, amount = _parse_line(line)
        is_bold = label in {"Revenue", "Operating profit (loss)",
                            "Profit (loss) before tax",
                            "Annual period profit (loss)"}
        if is_bold:
            md.append(f"| **{label}** | **{amount}** |")
        else:
            md.append(f"| {label} | {amount} |")

    md.append("")

    # --- Breakdown ---
    md.append("## Detailed Breakdown\n")

    current_category = None
    breakdown_rows = []
    for line in sections["breakdown"]:
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        indent, label, amount = _parse_line(line)

        # Category headers
        if indent <= 1 and not amount:
            if breakdown_rows:
                md.append("| | EUR |")
                md.append("|:---|---:|")
                md.extend(breakdown_rows)
                md.append("")
                breakdown_rows = []
            md.append(f"### {label}\n")
            continue

        label = label.replace(":", " › ")
        breakdown_rows.append(f"| {label} | {amount} |")

    if breakdown_rows:
        md.append("| | EUR |")
        md.append("|:---|---:|")
        md.extend(breakdown_rows)
        md.append("")

    if warning:
        md.append(f"\n> Note: Balance sheet variance of {warning.split('off by')[1].strip() if 'off by' in warning else ''} is expected — prior-year equity is from the accountant's filed figures while current-year assets are from beancount. This resolves when the accountant finalizes the {year} balance sheet.")

    # Notes section
    if notes:
        md.append("\n---\n")
        md.append("## Notes\n")

        # Note 2 — Related parties
        md.append("### Note 2 — Related parties\n")
        md.append("Balances with management and individuals with material ownership interest.\n")
        md.append(f"| | {year} | {year - 1} |")
        md.append("|:---|---:|---:|")
        rp = notes["related_parties"]
        md.append(f"| Payables to owner | {rp['curr_year']:,} | {rp['prev_year']:,} |")
        md.append("")

        # Note 3 — Labor expense
        md.append("### Note 3 — Labor expense\n")
        md.append(f"| | {year} | {year - 1} |")
        md.append("|:---|---:|---:|")
        le = notes["labor_expense"]
        md.append(f"| Total labor expense | {le['curr_year']:,} | {le['prev_year']:,} |")
        md.append("")

        # Profit distribution proposal
        md.append("### Profit distribution proposal\n")
        pd = notes["profit_distribution"]
        md.append("| | EUR |")
        md.append("|:---|---:|")
        md.append(f"| Retained earnings (loss) | {pd['retained_earnings']:,} |")
        md.append(f"| Annual period profit (loss) | {pd['annual_profit']:,} |")
        md.append(f"| **Total** | **{pd['total']:,}** |")
        md.append("")

        # Revenue breakdown
        md.append("### Breakdown of revenue\n")
        rb = notes["revenue_breakdown"]
        md.append(f"| Field of activity | EMTAK | {year} | {year - 1} |")
        md.append("|:---|:---|---:|---:|")
        md.append(f"| Computer programming activities | 62101 | {rb['curr_year']:,} | {rb['prev_year']:,} |")

    return "\n".join(md)


def _report_to_telegraph_html(report: str, company: str, year: int) -> str:
    """Convert plain-text annual report to clean Telegraph HTML.

    Telegraph only supports: a, aside, b, blockquote, br, code, em,
    figcaption, figure, h3, h4, hr, i, img, li, ol, p, pre, s, strong, u, ul
    """
    import html as html_mod
    import re

    lines = report.splitlines()

    # Parse sections
    sections = {"balance_sheet": [], "income_statement": [], "breakdown": []}
    current = None
    for line in lines:
        if "BALANCE SHEET" in line:
            current = "balance_sheet"
            continue
        elif "INCOME STATEMENT" in line:
            current = "income_statement"
            continue
        elif line.strip() == "BREAKDOWN":
            current = "breakdown"
            continue
        elif line.startswith("Source:") or line.startswith("==="):
            continue
        if current:
            sections[current].append(line)

    def _parse_line(line):
        """Parse a report line into (indent, label, amount)."""
        stripped = line.strip()
        match = re.match(r'^(\s*)(.*?)\s{2,}(-?[\d,.]+)$', line)
        if match:
            indent = len(match.group(1)) // 4
            return indent, match.group(2).strip(), match.group(3).strip()
        indent = (len(line) - len(line.lstrip())) // 4
        return indent, stripped, ""

    def _row(label, amount="", bold=False, indent=0):
        """Build a line item as a <p> with dots leader."""
        e_label = html_mod.escape(label)
        e_amount = html_mod.escape(amount)
        pad = "\u2003" * indent  # em-space for indentation
        if bold and amount:
            return f"<p><strong>{pad}{e_label} {'·' * max(1, 45 - len(label) - indent * 3 - len(amount))} {e_amount}</strong></p>"
        elif bold:
            return f"<p><strong>{pad}{e_label}</strong></p>"
        elif amount:
            return f"<p>{pad}{e_label} {'·' * max(1, 45 - len(label) - indent * 3 - len(amount))} {e_amount}</p>"
        else:
            return f"<p>{pad}{e_label}</p>"

    html_parts = []

    # --- Balance Sheet ---
    html_parts.append(f"<h3>Balance Sheet — 31.12.{year}</h3>")
    html_parts.append("<p><em>In Euros</em></p>")

    skip_bs = {"Financial investments", "Inventories", "Biological assets",
               "Investments in subsidiaries and associates", "Investment property",
               "Property, plant and equipment", "Intangible assets",
               "Loan liabilities", "Provisions", "Government grants",
               "Unregistered equity", "Unpaid capital", "Share premium",
               "Treasury shares", "Statutory reserve capital", "Other reserves",
               "Other equity"}
    for line in sections["balance_sheet"]:
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        indent, label, amount = _parse_line(line)
        if label in skip_bs and not amount:
            continue
        is_total = "Total" in label
        # Only show non-current section if it has value
        if label == "Non-current assets" or label == "Non-current liabilities":
            continue
        if "Total non-current" in label and amount == "0.00":
            continue
        html_parts.append(_row(label, amount, bold=is_total, indent=indent))

    html_parts.append("<hr>")

    # --- Income Statement ---
    html_parts.append(f"<h3>Income Statement — {year}</h3>")
    html_parts.append("<p><em>In Euros</em></p>")

    skip_is = {"Changes in inventories of agricultural production",
               "Profit (loss) from biological assets",
               "Changes in inventories of finished goods and WIP",
               "Work performed by entity and capitalised",
               "Depreciation and impairment loss (reversal)",
               "Significant impairment of current assets",
               "Profit (loss) from subsidiaries",
               "Profit (loss) from associates",
               "Gain (loss) from financial investments",
               "Interest income", "Interest expenses",
               "Other financial income and expense",
               "Income tax expense"}
    for line in sections["income_statement"]:
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        if stripped in skip_is:
            continue
        indent, label, amount = _parse_line(line)
        is_bold = label in {"Revenue", "Operating profit (loss)",
                            "Profit (loss) before tax",
                            "Annual period profit (loss)"}
        # Add spacing before operating profit
        if label == "Operating profit (loss)":
            html_parts.append("<br>")
        html_parts.append(_row(label, amount, bold=is_bold))
        if label == "Operating profit (loss)" or label == "Annual period profit (loss)":
            html_parts.append("<br>")

    html_parts.append("<hr>")

    # --- Breakdown ---
    html_parts.append("<h3>Detailed Breakdown</h3>")

    for line in sections["breakdown"]:
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue

        indent, label, amount = _parse_line(line)

        # Category headers
        if indent <= 1 and not amount:
            html_parts.append(f"<h4>{html_mod.escape(label)}</h4>")
            continue

        # Clean account names
        label = label.replace(":", " \u203a ")  # ›
        html_parts.append(_row(label, amount, indent=0))

    # Warning
    for line in lines:
        if "off by" in line:
            html_parts.append(f"<aside><em>{html_mod.escape(line.strip())}</em></aside>")
            break

    return "\n".join(html_parts)


def _build_annual_notes(year: int, ledger_type: str = "cupbots") -> dict:
    """Build the notes section for the annual report.

    Returns dict with keys: related_parties, labor_expense, profit_distribution, revenue_breakdown
    """
    entries, errors, options = _load_journal_for_year(year, ledger_type)
    OC = OPERATING_CURRENCY
    year_start = f"{year}-01-01"
    year_end = f"{year + 1}-01-01"
    prev_start = f"{year - 1}-01-01"

    # Note 2: Related parties — Liabilities:Out-of-pocket
    curr_liab = abs(_bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Liabilities:Out-of-pocket' AND date < {year_end}"))
    prev_liab = abs(_bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Liabilities:Out-of-pocket' AND date < {year_start}"))

    # Note 3: Labor expense (Contractors + Salary)
    curr_labor = _bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE (account ~ 'Expenses:Contractors' OR account ~ 'Expenses:Salary') "
        f"AND date >= {year_start} AND date < {year_end}")
    prev_labor = _bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE (account ~ 'Expenses:Contractors' OR account ~ 'Expenses:Salary') "
        f"AND date >= {prev_start} AND date < {year_start}")

    # Profit distribution
    config = _load_balance_sheet_config(ledger_type)
    prev_config = config.get("years", {}).get(str(year - 1))
    if prev_config:
        retained = Decimal(str(prev_config["retained_earnings"])) + Decimal(str(prev_config.get("annual_period_profit", 0)))
    else:
        retained = Decimal("0")

    # Annual profit from P&L
    annual_income = -_bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Income' AND date >= {year_start} AND date < {year_end}")
    annual_expenses = _bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Expenses' AND date >= {year_start} AND date < {year_end}")
    annual_profit = annual_income - annual_expenses

    # Revenue breakdown (Clients + Software)
    revenue = -_bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE (account ~ 'Income:Clients' OR account ~ 'Income:Software') "
        f"AND date >= {year_start} AND date < {year_end}")
    prev_revenue = -_bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE (account ~ 'Income:Clients' OR account ~ 'Income:Software') "
        f"AND date >= {prev_start} AND date < {year_start}")

    return {
        "related_parties": {
            "curr_year": round(curr_liab),
            "prev_year": round(prev_liab),
        },
        "labor_expense": {
            "curr_year": round(curr_labor),
            "prev_year": round(prev_labor),
        },
        "profit_distribution": {
            "retained_earnings": round(retained),
            "annual_profit": round(annual_profit),
            "total": round(retained + annual_profit),
        },
        "revenue_breakdown": {
            "curr_year": round(revenue),
            "prev_year": round(prev_revenue),
        },
    }


def _load_balance_sheet_config(ledger_type: str) -> dict:
    """Load accountant-approved balance sheet figures."""
    config_path = FINANCES_DIR / ledger_type / "annual-report" / "balance_sheet.json"
    if config_path.exists():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return {"company": ledger_type.upper(), "issued_capital": 0, "years": {}}


def _load_journal_for_year(year: int, ledger_type: str = "cupbots"):
    """Load the right journal based on year and ledger type.

    cupbots: journal-2024.beancount has full history (2017-2024),
             journal.beancount starts fresh from 2025.
    personal: single journal.beancount with all history.
    """
    from beancount import loader

    if ledger_type == "personal":
        return loader.load_file(str(FINANCES_DIR / "personal" / "journal.beancount"))

    archive = FINANCES_DIR / "cupbots" / "journal-2024.beancount"
    current = FINANCES_DIR / "cupbots" / "journal.beancount"

    if year <= 2024 and archive.exists():
        return loader.load_file(str(archive))
    return loader.load_file(str(current))


def _bql_sum_eur(entries, options, bql: str) -> Decimal:
    """Run BQL and return a single EUR sum."""
    from beanquery import query as bq
    _, rows = bq.run_query(entries, options, bql)
    total = Decimal("0")
    for row in rows:
        total += _sum_eur_from_inventory(row[-1])
    return total


def _bql_account_balances(entries, options, bql: str) -> list[tuple[str, Decimal]]:
    """Run BQL and return list of (account, EUR amount)."""
    from beanquery import query as bq
    _, rows = bq.run_query(entries, options, bql)
    results = []
    for row in rows:
        acct = str(row[0])
        amt = _sum_eur_from_inventory(row[1])
        results.append((acct, amt))
    return results


def _get_yearend_fx_rates(year: int) -> dict[str, Decimal]:
    """Read FX rates for year-end date from fx.beancount.

    Returns {currency: rate} where rate is EUR per 1 unit of currency.
    Falls back to closest earlier date if exact date not found.
    """
    import re as _re
    fx_path = FINANCES_DIR / "cupbots" / "fx.beancount"
    target = f"{year}-12-31"
    rates: dict[str, Decimal] = {}

    if not fx_path.exists():
        return rates

    # Collect all rates, keep the one closest to (but not after) year-end per currency
    all_rates: dict[str, list[tuple[str, Decimal]]] = {}
    for line in fx_path.read_text().splitlines():
        m = _re.match(r"^(\d{4}-\d{2}-\d{2})\s+price\s+EUR\s+([\d.]+)\s+(\w+)", line.strip())
        if m:
            dt, rate_str, curr = m.group(1), m.group(2), m.group(3)
            if dt <= target:
                all_rates.setdefault(curr, []).append((dt, Decimal(rate_str)))

    for curr, entries in all_rates.items():
        # Pick the latest date <= year-end
        entries.sort(key=lambda x: x[0], reverse=True)
        rates[curr] = entries[0][1]

    return rates


def _convert_native_to_eur(native_balances: list[tuple[str, str]], fx_rates: dict[str, Decimal]) -> Decimal:
    """Convert list of (amount_str, currency) to EUR using year-end rates.

    native_balances: list of (amount_str, currency_str) from inventory parsing.
    fx_rates: {currency: EUR_per_unit} from _get_yearend_fx_rates.
    """
    total = Decimal("0")
    for amt_str, curr in native_balances:
        amt = Decimal(amt_str)
        if curr == "EUR":
            total += amt
        elif curr in fx_rates and fx_rates[curr] != 0:
            total += amt / fx_rates[curr]
    return total


def _bql_sum_eur_yearend(entries, options, bql: str, fx_rates: dict[str, Decimal]) -> Decimal:
    """Run BQL returning native positions, then convert using year-end FX rates."""
    from beanquery import query as bq
    _, rows = bq.run_query(entries, options, bql)
    total = Decimal("0")
    for row in rows:
        inv = row[-1]
        if inv is None:
            continue
        if hasattr(inv, '__iter__') and not isinstance(inv, str):
            for item in inv:
                parts = str(item).strip().split()
                if len(parts) >= 2:
                    amt_str, curr = parts[0], parts[1]
                    try:
                        amt = Decimal(amt_str)
                        if curr == "EUR":
                            total += amt
                        elif curr in fx_rates and fx_rates[curr] != 0:
                            total += amt / fx_rates[curr]
                    except Exception:
                        pass
    return total


def _build_annual_report(year: int, ledger_type: str = "cupbots") -> str:
    """Build annual report with balance sheet + P&L.

    Balance sheet uses accountant-approved figures from balance_sheet.json
    for prior years. The current/requested year's annual period profit is
    always computed from the beancount journal. If no config exists for the
    prior year, falls back to beancount-computed figures.
    """
    config = _load_balance_sheet_config(ledger_type)
    entries, errors, options = _load_journal_for_year(year, ledger_type)

    year_start = f"{year}-01-01"
    year_end_exclusive = f"{year + 1}-01-01"
    as_of = f"31.12.{year}"
    OC = OPERATING_CURRENCY
    ISSUED_CAPITAL = Decimal(str(config.get("issued_capital", 2500)))

    # --- Annual period profit (always from beancount) ---
    annual_income = -_bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Income' AND date >= {year_start} AND date < {year_end_exclusive}")
    annual_expenses = _bql_sum_eur(entries, options,
        f"SELECT sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Expenses' AND date >= {year_start} AND date < {year_end_exclusive}")
    annual_profit = annual_income - annual_expenses

    # --- Balance sheet: use prior-year config if available ---
    prev_year = str(year - 1)
    prev_config = config.get("years", {}).get(prev_year)

    # --- Balance sheet ---
    # Check if we have config for THIS year (finalized by accountant)
    this_config = config.get("years", {}).get(str(year))

    if this_config:
        # Accountant-finalized balance sheet — use as-is
        cash = Decimal(str(this_config["cash_and_equivalents"]))
        investments = Decimal(str(this_config.get("noncurrent_financial_investments", 0)))
        receivables = Decimal(str(this_config.get("receivables_and_prepayments", 0)))
        payables = Decimal(str(this_config.get("payables_and_prepayments", 0)))
        retained_earnings = Decimal(str(this_config["retained_earnings"]))
        source = f"balance_sheet.json ({year} finalized)"
    elif prev_config:
        # Prior year finalized — carry forward retained earnings, compute rest from beancount
        prev_retained = Decimal(str(prev_config["retained_earnings"]))
        prev_annual_profit = Decimal(str(prev_config.get("annual_period_profit", 0)))
        retained_earnings = prev_retained + prev_annual_profit

        # Balance sheet from beancount using year-end FX rates
        fx = _get_yearend_fx_rates(year)
        cash = _bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE (account ~ 'Assets:Cash' OR account ~ 'Assets:Jar' OR account ~ 'Assets:Stripe') "
            f"AND date < {year_end_exclusive}", fx)
        investments = _bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE account ~ 'Assets:Investments' AND date < {year_end_exclusive}", fx)
        receivables = _bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE (account ~ 'Assets:Receivables' OR account ~ 'Assets:Refund') "
            f"AND date < {year_end_exclusive}", fx)
        payables = -_bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE account ~ 'Liabilities:Out-of-pocket' AND date < {year_end_exclusive}", fx)
        source = f"retained earnings from balance_sheet.json ({year - 1}), assets from beancount (year-end FX)"
    else:
        # No config at all — pure beancount with year-end FX rates
        fx = _get_yearend_fx_rates(year)
        cash = _bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE (account ~ 'Assets:Cash' OR account ~ 'Assets:Jar' OR account ~ 'Assets:Stripe') "
            f"AND date < {year_end_exclusive}", fx)
        investments = _bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE account ~ 'Assets:Investments' AND date < {year_end_exclusive}", fx)
        receivables = _bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE (account ~ 'Assets:Receivables' OR account ~ 'Assets:Refund') "
            f"AND date < {year_end_exclusive}", fx)
        payables = -_bql_sum_eur_yearend(entries, options,
            f"SELECT sum(position) "
            f"WHERE account ~ 'Liabilities:Out-of-pocket' AND date < {year_end_exclusive}", fx)

        if year <= 2024:
            prior_income = -_bql_sum_eur(entries, options,
                f"SELECT sum(convert(position, '{OC}')) "
                f"WHERE account ~ 'Income' AND date < {year_start}")
            prior_expenses = _bql_sum_eur(entries, options,
                f"SELECT sum(convert(position, '{OC}')) "
                f"WHERE account ~ 'Expenses' AND date < {year_start}")
            retained_earnings = prior_income - prior_expenses
        else:
            opening = _bql_sum_eur(entries, options,
                f"SELECT sum(convert(position, '{OC}')) "
                f"WHERE account ~ 'Equity:Opening-Balances'")
            retained_earnings = -opening - ISSUED_CAPITAL
            if year > 2025:
                extra_income = -_bql_sum_eur(entries, options,
                    f"SELECT sum(convert(position, '{OC}')) "
                    f"WHERE account ~ 'Income' AND date >= 2025-01-01 AND date < {year_start}")
                extra_expenses = _bql_sum_eur(entries, options,
                    f"SELECT sum(convert(position, '{OC}')) "
                    f"WHERE account ~ 'Expenses' AND date >= 2025-01-01 AND date < {year_start}")
                retained_earnings += extra_income - extra_expenses
        source = "beancount (no config, year-end FX)"

    total_current = cash + receivables
    total_noncurrent = investments
    total_assets = total_current + total_noncurrent

    total_current_liab = payables
    total_liabilities = total_current_liab

    total_equity = ISSUED_CAPITAL + retained_earnings + annual_profit
    total_liab_equity = total_liabilities + total_equity

    # --- P&L breakdown ---
    income_accounts = _bql_account_balances(entries, options,
        f"SELECT account, sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Income' AND date >= {year_start} AND date < {year_end_exclusive} "
        f"GROUP BY account ORDER BY account")
    expense_accounts = _bql_account_balances(entries, options,
        f"SELECT account, sum(convert(position, '{OC}')) "
        f"WHERE account ~ 'Expenses' AND date >= {year_start} AND date < {year_end_exclusive} "
        f"GROUP BY account ORDER BY account")

    # --- Estonian income statement groupings ---
    # Revenue: Income:Clients + Income:Software
    # Other income: Income:Wise:Interest + Income:Other:Currency + Income:Selling + etc.
    # Raw materials & consumables: Expenses:Office:*
    # Other operating expense: Expenses:Travel:* + Expenses:Fee:* + Expenses:Admin:* + Expenses:Benefits:* + Expenses:Marketing:*
    # Employee expense: Expenses:Contractors + Expenses:Salary
    # Other expense: Expenses:Other:*
    revenue = Decimal("0")
    other_income = Decimal("0")
    for acct, amt in income_accounts:
        val = -amt  # Income is negative in beancount
        if acct.startswith("Income:Clients") or acct.startswith("Income:Software"):
            revenue += val
        else:
            # Wise interest, currency gains — all go to "Other income"
            other_income += val

    raw_materials = Decimal("0")
    other_operating = Decimal("0")
    employee_expense = Decimal("0")
    other_expense = Decimal("0")
    for acct, amt in expense_accounts:
        if acct.startswith("Expenses:Office"):
            raw_materials += amt
        elif acct.startswith("Expenses:Contractors") or acct.startswith("Expenses:Salary"):
            employee_expense += amt
        elif acct.startswith("Expenses:Other"):
            other_expense += amt
        else:
            # Travel, Fee, Admin, Benefits, Marketing
            other_operating += amt

    # FX gain/loss comes from booked journal entries (Income:Other:Currency / Expenses:Other:Currency)
    # Book year-end FX recalculation with: python finances/scripts/calculate_currency_gain.py

    operating_profit = revenue + other_income - raw_materials - other_operating - employee_expense - other_expense
    profit_before_tax = operating_profit

    # --- Format report ---
    W = 58
    def fmtline(label: str, amount: Decimal | None = None, indent: int = 0) -> str:
        prefix = "    " * indent
        lbl = f"{prefix}{label}"
        if amount is not None:
            return f"{lbl:<{W}}{amount:>10,.2f}"
        return lbl

    source = "balance_sheet.json" if prev_config else "beancount (no prior config)"

    lines = [
        f"{config.get('company', ledger_type.upper())} — Annual Report {year}",
        "=" * (W + 10),
        "",
        f"BALANCE SHEET                                              {as_of}",
        "-" * (W + 10),
        fmtline("Assets"),
        fmtline("Current assets", indent=1),
        fmtline("Cash and cash equivalents", cash, indent=2),
        fmtline("Financial investments", indent=2),
        fmtline("Receivables and prepayments", receivables, indent=2),
        fmtline("Inventories", indent=2),
        fmtline("Biological assets", indent=2),
        fmtline("Total current assets", total_current, indent=2),
        fmtline("Non-current assets", indent=1),
        fmtline("Investments in subsidiaries and associates", indent=2),
        fmtline("Financial investments", investments if investments else None, indent=2),
        fmtline("Receivables and prepayments", indent=2),
        fmtline("Investment property", indent=2),
        fmtline("Property, plant and equipment", indent=2),
        fmtline("Biological assets", indent=2),
        fmtline("Intangible assets", indent=2),
        fmtline("Total non-current assets", total_noncurrent, indent=2),
        fmtline("Total assets", total_assets),
        "",
        fmtline("Liabilities and equity"),
        fmtline("Liabilities", indent=1),
        fmtline("Current liabilities", indent=2),
        fmtline("Loan liabilities", indent=3),
        fmtline("Payables and prepayments", payables, indent=3),
        fmtline("Provisions", indent=3),
        fmtline("Government grants", indent=3),
        fmtline("Total current liabilities", total_current_liab, indent=3),
        fmtline("Non-current liabilities", indent=2),
        fmtline("Loan liabilities", indent=3),
        fmtline("Payables and prepayments", indent=3),
        fmtline("Provisions", indent=3),
        fmtline("Government grants", indent=3),
        fmtline("Total non-current liabilities", Decimal("0"), indent=3),
        fmtline("Total liabilities", total_liabilities, indent=1),
        fmtline("Equity", indent=1),
        fmtline("Issued capital", ISSUED_CAPITAL, indent=3),
        fmtline("Unregistered equity", indent=3),
        fmtline("Unpaid capital", indent=3),
        fmtline("Share premium", indent=3),
        fmtline("Treasury shares", indent=3),
        fmtline("Statutory reserve capital", indent=3),
        fmtline("Other reserves", indent=3),
        fmtline("Other equity", indent=3),
        fmtline("Retained earnings (loss)", retained_earnings, indent=3),
        fmtline("Annual period profit (loss)", annual_profit, indent=3),
        fmtline("Total equity", total_equity, indent=1),
        fmtline("Total liabilities and equity", total_liab_equity),
        "",
        "",
        f"INCOME STATEMENT                                           {year}",
        "-" * (W + 10),
        fmtline("Revenue", revenue),
        fmtline("Other income", other_income),
        fmtline("Changes in inventories of agricultural production"),
        fmtline("Profit (loss) from biological assets"),
        fmtline("Changes in inventories of finished goods and WIP"),
        fmtline("Work performed by entity and capitalised"),
        fmtline("Raw materials and consumables used", raw_materials),
        fmtline("Other operating expense", other_operating),
        fmtline("Employee expense", employee_expense),
        fmtline("Depreciation and impairment loss (reversal)"),
        fmtline("Significant impairment of current assets"),
        fmtline("Other expense", other_expense),
        fmtline("Operating profit (loss)", operating_profit),
        "",
        fmtline("Profit (loss) before tax", profit_before_tax),
        fmtline("Income tax expense"),
        fmtline("Annual period profit (loss)", annual_profit),
        "",
        "-" * (W + 10),
        "",
        "BREAKDOWN",
    ]

    # Revenue detail
    lines.append(fmtline("Revenue", indent=1))
    for acct, amt in income_accounts:
        if acct.startswith("Income:Clients") or acct.startswith("Income:Software"):
            lines.append(fmtline(acct.replace("Income:", ""), -amt, indent=2))

    # Other income detail
    other_inc_items = [(a, -v) for a, v in income_accounts
                       if not a.startswith("Income:Clients") and not a.startswith("Income:Software")]
    if other_inc_items:
        lines.append(fmtline("Other income / Interest", indent=1))
        for acct, amt in other_inc_items:
            lines.append(fmtline(acct.replace("Income:", ""), amt, indent=2))

    # Expense detail
    lines.append(fmtline("Raw materials & consumables", indent=1))
    for acct, amt in expense_accounts:
        if acct.startswith("Expenses:Office"):
            lines.append(fmtline(acct.replace("Expenses:", ""), amt, indent=2))

    lines.append(fmtline("Other operating expense", indent=1))
    for acct, amt in expense_accounts:
        if not acct.startswith("Expenses:Office") and not acct.startswith("Expenses:Contractors") \
           and not acct.startswith("Expenses:Salary") and not acct.startswith("Expenses:Other"):
            lines.append(fmtline(acct.replace("Expenses:", ""), amt, indent=2))

    lines.append(fmtline("Employee expense", indent=1))
    for acct, amt in expense_accounts:
        if acct.startswith("Expenses:Contractors") or acct.startswith("Expenses:Salary"):
            lines.append(fmtline(acct.replace("Expenses:", ""), amt, indent=2))

    if other_expense:
        lines.append(fmtline("Other expense", indent=1))
        for acct, amt in expense_accounts:
            if acct.startswith("Expenses:Other"):
                lines.append(fmtline(acct.replace("Expenses:", ""), amt, indent=2))

    lines.extend([
        "",
        f"Source: {source}",
    ])

    # Sanity check
    diff = total_assets - total_liab_equity
    if abs(diff) > Decimal("1"):
        lines.append(f"Note: Balance sheet variance of {diff:,.2f} {OC} — prior-year equity from accountant's filed figures, current assets from beancount. Safe to ignore until journals are combined.")

    return "\n".join(lines)


LHDN_RELIEF_LIMITS = {
    "Expenses:Tax:Parents": ("Parents medical/dental/carer", Decimal("8000")),
    "Expenses:Tax:DisabledEquipment": ("Disabled equipment", Decimal("6000")),
    "Expenses:Tax:Education": ("Education fees (self)", Decimal("7000")),
    "Expenses:Tax:Medical": ("Medical (serious/fertility/dental)", Decimal("10000")),
    "Expenses:Tax:MedicalCheckup": ("Medical checkup/test/monitoring", Decimal("1000")),
    "Expenses:Tax:Lifestyle": ("Lifestyle (books/PC/internet/skill)", Decimal("2500")),
    "Expenses:Tax:Sports": ("Sports/gym", Decimal("1000")),
    "Expenses:Tax:SSPN": ("Education savings (SSPN)", Decimal("8000")),
    "Expenses:Tax:Insurance": ("Life insurance + EPF", Decimal("7000")),
    "Expenses:Tax:PRS": ("PRS / deferred annuity", Decimal("3000")),
    "Expenses:Tax:MedicalInsurance": ("Education & medical insurance", Decimal("4000")),
    "Expenses:Tax:SOCSO": ("SOCSO", Decimal("350")),
    "Expenses:Tax:EV": ("EV charging / composting", Decimal("2500")),
    "Expenses:Tax:HousingLoan": ("Housing loan interest (1st home)", Decimal("7000")),
}


# Malaysian tax brackets (2025 YA)
MY_TAX_BRACKETS = [
    (Decimal("5000"), Decimal("0")),
    (Decimal("15000"), Decimal("0.01")),
    (Decimal("15000"), Decimal("0.03")),
    (Decimal("15000"), Decimal("0.06")),     # 35001-50000 (chargeable after deductions)
    (Decimal("20000"), Decimal("0.11")),
    (Decimal("30000"), Decimal("0.19")),
    (Decimal("150000"), Decimal("0.25")),
    (Decimal("150000"), Decimal("0.26")),
    (Decimal("200000"), Decimal("0.28")),
    (Decimal("400000"), Decimal("0.30")),
]


def _calc_my_tax(chargeable: Decimal) -> Decimal:
    """Calculate Malaysian income tax from chargeable income."""
    tax = Decimal("0")
    remaining = chargeable
    for bracket_size, rate in MY_TAX_BRACKETS:
        if remaining <= 0:
            break
        taxable = min(remaining, bracket_size)
        tax += taxable * rate
        remaining -= bracket_size
    if remaining > 0:
        tax += remaining * Decimal("0.30")
    return tax


def _sum_myr_from_inventory(inventory) -> Decimal:
    """Extract MYR total from a beancount inventory object."""
    total = Decimal("0")
    if inventory is None:
        return total
    if isinstance(inventory, Decimal):
        return inventory
    try:
        for pos in inventory:
            s = str(pos)
            # e.g. "2500.00 MYR"
            parts = s.strip().split()
            if len(parts) >= 1:
                total += Decimal(parts[0].replace(",", ""))
    except (TypeError, ValueError):
        pass
    return total


async def handle_command(msg, reply) -> bool:
    """Cross-platform command handler for finance reports."""
    cmd = msg.command
    args = msg.args or []

    if cmd not in COMMANDS:
        return False

    if args and args[0] in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    if not is_admin(msg.platform, msg.sender_id) and not msg.sender_role:
        await reply.reply_text("Finance commands are restricted.")
        return True

    if cmd == "pnl":
        try:
            ledger_type, rest = parse_ledger_and_args(args)
            start, end, _ = parse_date_range(rest)
            await reply.send_typing()
            bql = (
                f"SELECT account, sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
                f"WHERE (account ~ 'Income' OR account ~ 'Expenses') "
                f"AND date >= {start} AND date < {end} "
                f"GROUP BY account ORDER BY account"
            )
            result_types, result_rows = run_bql_raw(ledger_type, bql)

            income_lines = []
            expense_lines = []
            total_income = Decimal("0")
            total_expenses = Decimal("0")
            for row in result_rows:
                account = str(row[0])
                amount = _sum_eur_from_inventory(row[1])
                if account.startswith("Income"):
                    income_lines.append((account, amount))
                    total_income += amount
                elif account.startswith("Expenses"):
                    expense_lines.append((account, amount))
                    total_expenses += amount

            net = -total_income - total_expenses

            lines = [
                f"P&L ({ledger_type}) -- {start} to {end}",
                "=" * 55, "",
                "INCOME", "-" * 55,
            ]
            for acct, amt in income_lines:
                lines.append(f"  {acct:<45} {-amt:>10,.2f}")
            lines.append(f"  {'TOTAL INCOME':<45} {-total_income:>10,.2f} {OPERATING_CURRENCY}")
            lines.extend(["", "EXPENSES", "-" * 55])
            for acct, amt in expense_lines:
                lines.append(f"  {acct:<45} {amt:>10,.2f}")
            lines.append(f"  {'TOTAL EXPENSES':<45} {total_expenses:>10,.2f} {OPERATING_CURRENCY}")
            lines.extend([
                "", "=" * 55,
                f"  {'NET INCOME':<45} {net:>10,.2f} {OPERATING_CURRENCY}",
                "", PERIOD_HELP,
            ])
            await send_long_text(reply, "\n".join(lines), "pnl.txt")
        except Exception as e:
            log.error("P&L failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "bs":
        try:
            ledger_type, rest = parse_ledger_and_args(args)
            as_of = rest[0] if rest else date.today().isoformat()
            await reply.send_typing()
            bql = (
                f"SELECT account, sum(position) "
                f"WHERE date <= {as_of} "
                f"GROUP BY account ORDER BY account"
            )
            result_types, result_rows = run_bql_raw(ledger_type, bql)

            sections = {"Assets": [], "Liabilities": [], "Equity": [], "Income": [], "Expenses": []}
            for row in result_rows:
                account = str(row[0])
                inv_str = _format_inventory(row[1])
                if not inv_str or inv_str == "0":
                    continue
                for prefix in sections:
                    if account.startswith(prefix):
                        sections[prefix].append((account, inv_str))
                        break

            lines = [
                f"Balance Sheet ({ledger_type}) -- as of {as_of}",
                "=" * 60,
            ]
            for section in ["Assets", "Liabilities", "Equity"]:
                lines.extend(["", section.upper(), "-" * 60])
                if sections[section]:
                    for acct, bal in sections[section]:
                        lines.append(f"  {acct:<45} {bal:>12}")
                else:
                    lines.append("  (none)")

            await send_long_text(reply, "\n".join(lines), "bs.txt")
        except Exception as e:
            log.error("Balance sheet failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "cashflow":
        try:
            ledger_type, rest = parse_ledger_and_args(args)
            start, end, _ = parse_date_range(rest)
            await reply.send_typing()
            bql = (
                f"SELECT account, sum(convert(position, '{OPERATING_CURRENCY}')) AS total "
                f"WHERE account ~ 'Assets:Cash' "
                f"AND date >= {start} AND date < {end} "
                f"GROUP BY account ORDER BY account"
            )
            result_types, result_rows = run_bql_raw(ledger_type, bql)

            total = Decimal("0")
            lines = [
                f"Cash Flow ({ledger_type}) -- {start} to {end}",
                "=" * 55, "",
            ]
            for row in result_rows:
                account = str(row[0])
                amount = _sum_eur_from_inventory(row[1])
                if abs(amount) > Decimal("0.01"):
                    lines.append(f"  {account:<45} {amount:>10,.2f}")
                    total += amount

            lines.extend([
                "", "-" * 55,
                f"  {'NET CASH MOVEMENT':<45} {total:>10,.2f} {OPERATING_CURRENCY}",
                "", PERIOD_HELP,
            ])
            await send_long_text(reply, "\n".join(lines), "cashflow.txt")
        except Exception as e:
            log.error("Cashflow failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "fxgain":
        try:
            ledger_type, rest = parse_ledger_and_args(args)
            target_date = rest[0] if rest else date.today().isoformat()
            await reply.send_typing()

            sys.path.insert(0, str(FINANCES_DIR / "scripts"))
            try:
                from calculate_currency_gain import get_final_aggregated_report
                import io
                from contextlib import redirect_stdout

                journal = str(FINANCES_DIR / ledger_type / "journal.beancount")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    get_final_aggregated_report(journal, target_date)
                output = buf.getvalue()

                await send_long_text(reply, output, "fxgain.txt")
            finally:
                scripts_path = str(FINANCES_DIR / "scripts")
                if scripts_path in sys.path:
                    sys.path.remove(scripts_path)
        except Exception as e:
            log.error("FX gain report failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "receivables":
        try:
            from collections import defaultdict
            ledger_type, _ = parse_ledger_and_args(args)
            await reply.send_typing()

            bal_bql = (
                "SELECT payee, sum(position) AS balance "
                "WHERE account ~ 'Receivable' "
                "GROUP BY payee ORDER BY sum(position)"
            )
            _, bal_rows = run_bql_raw(ledger_type, bal_bql)

            detail_bql = (
                "SELECT date, payee, narration, position "
                "WHERE account ~ 'Receivable' "
                "ORDER BY payee, date"
            )
            _, detail_rows = run_bql_raw(ledger_type, detail_bql)

            client_net: dict[str, Decimal] = {}
            client_currency: dict[str, str] = {}
            for row in bal_rows:
                payee = str(row[0]) if row[0] else "Unknown"
                amt, cur = _parse_position(row[1])
                if amt != 0:
                    client_net[payee] = amt
                    client_currency[payee] = cur

            if not client_net:
                await reply.reply_text(f"No outstanding receivables ({ledger_type}).")
                return True

            by_client: dict[str, list[tuple]] = defaultdict(list)
            for row in detail_rows:
                tx_date = str(row[0])
                payee = str(row[1]) if row[1] else "Unknown"
                narration = str(row[2]) if row[2] else ""
                amt, cur = _parse_position(row[3])
                if payee in client_net and amt > 0:
                    by_client[payee].append((tx_date, amt, cur, narration))

            for payee in list(by_client.keys()):
                entries = sorted(by_client[payee], key=lambda e: e[0], reverse=True)
                net = client_net[payee]
                kept = []
                running = Decimal("0")
                for entry in entries:
                    if running >= net:
                        break
                    kept.append(entry)
                    running += entry[1]
                by_client[payee] = sorted(kept, key=lambda e: e[0])

            lines = [f"Outstanding Receivables ({ledger_type})", "=" * 40, ""]
            grand_total = Decimal("0")
            for payee in sorted(client_net.keys(), key=lambda c: client_net[c], reverse=True):
                net = client_net[payee]
                cur = client_currency[payee]
                grand_total += net
                lines.append(f"{payee}  ({net:,.2f} {cur})")
                for tx_date, amt, c, narr in by_client.get(payee, []):
                    age = (date.today() - date.fromisoformat(tx_date)).days
                    lines.append(f"  {tx_date}  {amt:>10,.2f} {c}  {narr}  ({age}d)")
                lines.append("")
            lines.append(f"Grand total: {grand_total:,.2f}")

            await send_long_text(reply, "\n".join(lines), "receivables.txt")
        except Exception as e:
            log.error("Receivables query failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "payables":
        try:
            ledger_type, _ = parse_ledger_and_args(args)
            await reply.send_typing()
            result = run_bql(
                ledger_type,
                "SELECT account, sum(position) "
                "WHERE account ~ 'Liabilities' "
                "GROUP BY account ORDER BY account",
            )
            header = f"Payables ({ledger_type})"
            await send_long_text(reply, f"{header}\n{'=' * len(header)}\n\n{result}", "payables.txt")
        except Exception as e:
            log.error("Payables query failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "annualreport":
        try:
            ledger_type, rest = parse_ledger_and_args(args)
            year = int(rest[0]) if rest and rest[0].isdigit() else date.today().year - 1
            await reply.send_typing()
            report = _build_annual_report(year, ledger_type)
            notes = _build_annual_notes(year, ledger_type)

            # Build markdown and publish to mdpubs (with fallback to inline)
            try:
                from plugins.mdpubs.mdpubs_plugin import publish_or_fallback
                company = ledger_type.upper() if ledger_type != "cupbots" else "CUPBOTS OÜ"
                title = f"{company} — Annual Report {year}"
                md = _report_to_markdown(report, company, year, notes=notes)
                key = f"annual-report-{ledger_type}-{year}"
                url, content = await publish_or_fallback(key, title, md, tags=["finance", f"annual-report-{year}"])

                if url:
                    await reply.reply_text(f"{title}\n{url}")
                else:
                    await send_long_text(reply, f"{title}\n\n{content}", "annualreport.txt")
            except ImportError:
                await send_long_text(reply, report, "annualreport.txt")
        except Exception as e:
            log.error("Annual report failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "taxrelief":
        try:
            year = date.today().year
            if args and args[0].isdigit() and len(args[0]) == 4:
                year = int(args[0])
            await reply.send_typing()

            bql = (
                f"SELECT account, sum(convert(position, 'MYR')) AS total "
                f"WHERE account ~ 'Expenses:Tax' "
                f"AND date >= {year}-01-01 AND date < {year + 1}-01-01 "
                f"GROUP BY account ORDER BY account"
            )
            result_types, result_rows = run_bql_raw("personal", bql)

            totals: dict[str, Decimal] = {}
            for row in result_rows:
                account = str(row[0])
                amount = _sum_myr_from_inventory(row[1])
                totals[account] = amount

            socso_bql = (
                f"SELECT sum(convert(position, 'MYR')) AS total "
                f"WHERE account = 'Assets:SocialSecurity:SOCSO' "
                f"AND date >= {year}-01-01 AND date < {year + 1}-01-01 "
                f"AND narration ~ 'Salary'"
            )
            socso_types, socso_rows = run_bql_raw("personal", socso_bql)
            if socso_rows and socso_rows[0][0]:
                socso_from_salary = _sum_myr_from_inventory(socso_rows[0][0])
                existing_socso = totals.get("Expenses:Tax:SOCSO", Decimal("0"))
                if socso_from_salary > existing_socso:
                    totals["Expenses:Tax:SOCSO"] = socso_from_salary

            lines = [f"LHDN Tax Relief -- {year}", ""]
            grand_total = Decimal("0")
            for account, (label, limit) in LHDN_RELIEF_LIMITS.items():
                spent = totals.get(account, Decimal("0"))
                grand_total += spent
                if limit:
                    remaining = max(Decimal("0"), limit - spent)
                    pct = min(spent / limit * 100, Decimal("100"))
                    lines.append(
                        f"  {label}\n"
                        f"    RM {spent:,.0f} / RM {limit:,.0f} ({pct:.0f}%) -- RM {remaining:,.0f} left"
                    )
                else:
                    lines.append(f"  {label}\n    RM {spent:,.0f} (no cap)")
                lines.append("")
            lines.append(f"Total claimed: RM {grand_total:,.0f}")

            await send_long_text(reply, "\n".join(lines), "taxrelief.txt")
        except Exception as e:
            log.error("Tax relief report failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    if cmd == "taxsummary":
        try:
            year = date.today().year
            if args and args[0].isdigit() and len(args[0]) == 4:
                year = int(args[0])
            await reply.send_typing()

            date_filter = f"date >= {year}-01-01 AND date < {year + 1}-01-01"

            emp_bql = (
                f"SELECT sum(convert(position, 'MYR')) AS total "
                f"WHERE account = 'Income:Clients:CGPT' AND {date_filter}"
            )
            _, emp_rows = run_bql_raw("personal", emp_bql)
            gross_employment = abs(_sum_myr_from_inventory(emp_rows[0][0])) if emp_rows and emp_rows[0][0] else Decimal("0")

            ben_bql = (
                f"SELECT sum(convert(position, 'MYR')) AS total "
                f"WHERE account = 'Income:Employer:Benefits' AND {date_filter}"
            )
            _, ben_rows = run_bql_raw("personal", ben_bql)
            employer_benefits = abs(_sum_myr_from_inventory(ben_rows[0][0])) if ben_rows and ben_rows[0][0] else Decimal("0")

            other_bql = (
                f"SELECT account, sum(convert(position, 'MYR')) AS total "
                f"WHERE account ~ 'Income' AND account != 'Income:Clients:CGPT' "
                f"AND account != 'Income:Employer:Benefits' AND {date_filter} "
                f"GROUP BY account ORDER BY account"
            )
            other_types, other_rows = run_bql_raw("personal", other_bql)
            other_income_lines = []
            total_other = Decimal("0")
            for row in other_rows:
                acc = str(row[0])
                amt = abs(_sum_myr_from_inventory(row[1]))
                if amt > 0:
                    short_name = acc.replace("Income:", "")
                    other_income_lines.append((short_name, amt))
                    total_other += amt

            epf_bql = (
                f"SELECT sum(convert(position, 'MYR')) AS total "
                f"WHERE account = 'Assets:Retirement:EPF' AND {date_filter} "
                f"AND narration ~ 'Salary'"
            )
            _, epf_rows = run_bql_raw("personal", epf_bql)
            epf_employee = _sum_myr_from_inventory(epf_rows[0][0]) if epf_rows and epf_rows[0][0] else Decimal("0")

            socso_bql = (
                f"SELECT sum(convert(position, 'MYR')) AS total "
                f"WHERE account = 'Assets:SocialSecurity:SOCSO' AND {date_filter} "
                f"AND narration ~ 'Salary'"
            )
            _, socso_rows = run_bql_raw("personal", socso_bql)
            socso_employee = _sum_myr_from_inventory(socso_rows[0][0]) if socso_rows and socso_rows[0][0] else Decimal("0")

            eis_bql = (
                f"SELECT sum(convert(position, 'MYR')) AS total "
                f"WHERE account = 'Assets:SocialSecurity:EIS' AND {date_filter} "
                f"AND narration ~ 'Salary'"
            )
            _, eis_rows = run_bql_raw("personal", eis_bql)
            eis_employee = _sum_myr_from_inventory(eis_rows[0][0]) if eis_rows and eis_rows[0][0] else Decimal("0")

            pcb_bql = (
                f"SELECT sum(convert(position, 'MYR')) AS total "
                f"WHERE account = 'Liabilities:Tax' AND {date_filter}"
            )
            _, pcb_rows = run_bql_raw("personal", pcb_bql)
            pcb_withheld = _sum_myr_from_inventory(pcb_rows[0][0]) if pcb_rows and pcb_rows[0][0] else Decimal("0")

            relief_bql = (
                f"SELECT account, sum(convert(position, 'MYR')) AS total "
                f"WHERE account ~ 'Expenses:Tax' AND {date_filter} "
                f"GROUP BY account ORDER BY account"
            )
            _, relief_rows = run_bql_raw("personal", relief_bql)
            relief_items = []
            total_relief = Decimal("0")
            for row in relief_rows:
                acc = str(row[0])
                amt = _sum_myr_from_inventory(row[1])
                if amt > 0:
                    label = LHDN_RELIEF_LIMITS.get(acc, (acc.replace("Expenses:Tax:", ""), None))[0]
                    relief_items.append((label, amt))
                    total_relief += amt

            socso_relief_explicit = any("SOCSO" in r[0] for r in relief_items)
            if not socso_relief_explicit and socso_employee > 0:
                socso_capped = min(socso_employee, Decimal("350"))
                relief_items.append(("SOCSO (auto from salary)", socso_capped))
                total_relief += socso_capped

            individual_relief = Decimal("9000")
            total_relief += individual_relief

            total_income = gross_employment + total_other
            chargeable = max(Decimal("0"), total_income - total_relief)
            estimated_tax = _calc_my_tax(chargeable)
            tax_balance = estimated_tax - pcb_withheld

            lines = [
                f"LHDN Tax Summary -- {year}", "",
                "INCOME",
                f"  Employment (CGPT)        RM {gross_employment:>12,.2f}",
            ]
            for name, amt in other_income_lines:
                lines.append(f"  {name:<26} RM {amt:>12,.2f}")
            lines.append(f"  {'─' * 42}")
            lines.append(f"  Total income             RM {total_income:>12,.2f}")
            lines.append("")

            lines.append("RELIEFS & DEDUCTIONS")
            lines.append(f"  Individual (auto)        RM {individual_relief:>12,.2f}")
            for label, amt in relief_items:
                lines.append(f"  {label:<26} RM {amt:>12,.2f}")
            lines.append(f"  {'─' * 42}")
            lines.append(f"  Total relief             RM {total_relief:>12,.2f}")
            lines.append("")

            lines.append("TAX CALCULATION")
            lines.append(f"  Chargeable income        RM {chargeable:>12,.2f}")
            lines.append(f"  Estimated tax            RM {estimated_tax:>12,.2f}")
            lines.append(f"  PCB withheld             RM {pcb_withheld:>12,.2f}")
            if tax_balance > 0:
                lines.append(f"  Balance to pay           RM {tax_balance:>12,.2f}")
            else:
                lines.append(f"  Refund due               RM {abs(tax_balance):>12,.2f}")
            lines.append("")

            lines.append("INFO (non-taxable)")
            lines.append(f"  Employer benefits (EPF/SOCSO/EIS)  RM {employer_benefits:>8,.2f}")
            lines.append(f"  EPF employee contribution          RM {epf_employee:>8,.2f}")

            await send_long_text(reply, "\n".join(lines), "taxsummary.txt")
        except Exception as e:
            log.error("Tax summary failed: %s", e)
            await reply.reply_error(f"Error: {e}")
        return True

    return False
