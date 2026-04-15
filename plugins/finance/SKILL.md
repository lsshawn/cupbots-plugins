# finance

/finance — unified command for all write/recording/maintenance operations.

## Commands
- `/finance`

## Primitives

### Recording
```
/finance [cupbots|personal] [income|expenses] — Scan & process unprocessed invoices
/finance expense [personal] <desc> — Add expense (attach receipt image)
/finance income [personal] <desc> — Record income
/finance transfer [personal] <desc> — Inter-account transfer
/finance ar [personal] <desc> — Record invoice sent (AR entry)
/finance payment [personal] <desc> — Record payment received (clear AR)
```

### Maintenance
```
/finance void [personal] <search> — Void an entry (creates reversing entry)
/finance validate [personal] — Run bean-check validation
/finance fxsync — Fetch missing FX rates
/finance summary [personal] — Regenerate journal summary
```

### Audit
```
/finance reconcile [personal] <account> <balance> <currency> — Reconcile account
/finance duplicates [personal] — Scan for duplicate entries
```

### Invoicing
```
/finance invoice <client> <line-items> [--account NAME] [--proposal REF] — Create & send
/finance invoice list [client] — List recent invoices
/finance invoice status <id> — Check invoice status
/finance invoice accounts — List Stripe accounts
```

## Examples
```
/finance                            Scan all unprocessed invoices
/finance personal expenses          Scan personal expense invoices only
/finance expense lunch at cafe      Add expense (attach receipt image for OCR)
/finance expense personal groceries Personal expense
/finance income consulting Acme     Record income
/finance transfer moved 5000 to savings
/finance ar sent invoice to Acme for 3000 USD
/finance payment received 3000 from Acme
/finance void lunch at cafe 2025-03-15
/finance validate                   Validate cupbots ledger
/finance validate personal          Validate personal ledger
/finance fxsync                     Fetch missing FX rates
/finance reconcile Assets:Bank:Maybank 15000 MYR
/finance duplicates                 Check for duplicate entries
/finance invoice "Acme Corp" Web development 3000 USD --proposal PROP-2024-003
/finance invoice list               Recent invoices
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Default ledger is `cupbots`; add `personal` for personal ledger
- Attach receipt/invoice images when available — OCR extracts amount, date, payee
- `/finance void` creates a reversing entry — to correct, void then re-add via `/finance expense`
- For invoice write commands, use exact flag syntax: `--account`, `--proposal` (double-dash)
