# finance_fire

/money — unified command for all financial queries, reports, and FIRE tracking.

## Commands
- `/money`

## Primitives

### FIRE & portfolio
```
/money — Master FIRE dashboard
/money fire — FIRE metrics and projections
/money balances — All balances by custodian
/money tiers — Cash tier breakdown
/money budget — Expense budget × FIRE multipliers
/money growth — Net worth growth chart
/money scenarios <change> — What-if scenarios
/money networth [date] — Net worth (both ledgers)
```

### Query
```
/money search [personal] <query> — AI journal search (natural language)
/money query [personal] <BQL> — Raw BQL query
/money bal [personal] [filter] — Account balances
/money accounts [personal] [filter] — Chart of accounts
```

### Reports
```
/money pnl [personal] [period] — Profit & Loss
/money bs [personal] [date] — Balance Sheet
/money cashflow [personal] [period] — Cash flow summary
/money fxgain [personal] [date] — FX gain/loss
/money receivables — Outstanding receivables
/money payables [personal] — Outstanding liabilities
/money annual [personal] [year] — Annual report
/money tax [year] — Tax summary (MY, personal only)
/money taxrelief [year] — Tax relief (MY, personal only)
/money trial [personal] [date] — Trial balance
/money wise [personal|cupbots] [period] — Wise account queries
/money invoice list [client] — List invoices
/money invoice status <id> — Check invoice status
/money invoice accounts — List Stripe accounts
```

### Period formats
`jan`..`dec`, `q1`..`q4`, `ytd`, `last-3m`, `last-6m`, `2025`, `2025-03`

## Examples
```
/money                              Full FIRE dashboard
/money fire                         FIRE progress and projections
/money scenarios +200000 exit       What if I get a 200k exit
/money networth 2025-06-30          Net worth as of date
/money search last 5 petrol entries Most recent 5 petrol transactions
/money search personal grocery expenses last month
/money query SELECT date, narration, account, position WHERE payee ~ 'Caltex'
/money bal Expenses:Travel          Travel expense balances
/money pnl ytd                      Year-to-date P&L
/money pnl personal q1              Personal Q1 P&L
/money bs                           Balance sheet as of today
/money cashflow last-3m             Last 3 months cash flow
/money receivables                  Who owes money
/money annual 2025                  Full 2025 annual report
/money tax 2025                     Malaysian tax summary
/money trial                        Trial balance
/money wise 90d                     Wise transactions last 90 days
/money invoice list                 Recent invoices
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Default ledger is `cupbots`; add `personal` after the subcommand for personal ledger
- `/money search` accepts full natural language — pass the ENTIRE user query as-is
- CRITICAL: Query and report results must be relayed to the user verbatim — do NOT summarize
- `/money tax` and `/money taxrelief` are Malaysia-specific (LHDN), personal ledger only
- FIRE reports are published to mdpubs.com — the bot sends back a link
