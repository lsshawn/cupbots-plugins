# finance

Beancount journal recording

## Commands
- `/finance`

## Primitives
```
/finance [cupbots|personal] [income|expenses] — Scan & process unprocessed invoices
/finance expense [personal] <desc> — Add expense (attach receipt)
/finance income [personal] <desc> — Record income (attach invoice)
/finance transfer [personal] <desc> — Inter-account transfer
/finance ar [personal] <desc> — Record invoice sent (AR entry)
/finance payment [personal] <desc> — Record payment received (clear AR)
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
