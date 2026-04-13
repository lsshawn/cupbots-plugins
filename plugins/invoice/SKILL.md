# invoice

Stripe invoicing — create and send invoices to clients

## Commands
- `/invoice` (WRITE)
- `/invoice list`
- `/invoice status`

## Intent
USE FOR: Create Stripe invoices, find or create clients, send invoices with line items

## Primitives
```
/invoice <client> <line-items> [--account NAME] [--proposal REF] — Create & send invoice
/invoice list [client] — List recent invoices
/invoice status <invoice-id> — Check invoice status
/invoice accounts — List configured Stripe accounts
/invoice "Acme Corp" Web development 3000 USD, Design 1500 USD — proposal PROP-2024-003
/invoice acme@example.com Web dev 5000 — account agency
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
