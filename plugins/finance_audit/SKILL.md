# finance_audit

Auditing and reconciliation tools.

## Commands
- `/reconcile`
- `/trial`
- `/duplicates`

## Primitives
```
/reconcile [personal] <account> <balance> <currency> — Compare book vs expected
/trial [personal] [date] — Trial balance
/duplicates [personal] — Scan for duplicate entries
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
