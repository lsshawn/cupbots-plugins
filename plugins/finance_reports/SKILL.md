# finance_reports

Beancount financial reports.

## Commands
- `/pnl`
- `/bs`
- `/cashflow`
- `/fxgain`
- `/receivables`
- `/payables`
- `/annualreport`
- `/taxrelief`
- `/taxsummary`

## Primitives
```
/pnl [personal] [period] — Profit & Loss (jan, q1, ytd, last-3m, 2025, 2025-03...)
/bs [personal] [date] — Balance Sheet
/cashflow [personal] [period] — Cash flow summary (same periods as /pnl)
/fxgain [personal] [date] — FX gain/loss report
/receivables — Outstanding receivables
/payables [personal] — Outstanding liabilities
/annualreport [personal] [year] — Annual report (balance sheet + P&L)
/taxrelief [year] — Malaysian tax relief summary (personal ledger, LHDN)
/taxsummary [year] — Full income tax summary for BE form filing (personal)
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
