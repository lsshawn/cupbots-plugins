# invoice

Stripe invoicing. Create and send invoices from chat.

## Commands
- `/invoice <client> <line-items> [--account NAME] [--proposal REF] [--draft]` — Create invoice
- `/invoice send <invoice-id>` — Send a draft invoice email
- `/invoice void <invoice-id>` — Void an open invoice or delete a draft
- `/invoice list [draft|open|paid|void] [client]` — List invoices, filter by status and/or client
- `/invoice status <invoice-id>` — Check invoice status from Stripe
- `/invoice accounts` — List configured Stripe accounts

## Flags
- `--draft` — Create as finalized draft (not emailed). Use `/invoice send` to email later.
- `--account NAME` — Stripe account to use (default: client's previous account or 'default').
- `--proposal REF` — Attach a proposal reference to the invoice footer.

## Config (plugin_settings.invoice)
- `auto_send` — `true` (default) sends invoice email immediately. Set to `false` to create drafts by default.

## Line items format
Comma-separated: `<description> <amount> [currency]`

## Rules
- Use exact flag syntax with double-dash: `--account`, `--proposal`, `--draft`
- Client can be an email, a quoted name, or a single-word name
- When `auto_send: false`, all invoices are created as drafts unless explicitly sent with `/invoice send`
- `--draft` flag always creates a draft regardless of `auto_send` setting

## Examples
```
/invoice acme@example.com API integration 5000, Monthly hosting 200
/invoice acme@example.com API integration 5000 --draft
/invoice send inv_abc123
/invoice "Acme Corp" Web development 3000 USD --proposal PROP-2024-003
/invoice acme@example.com Web dev 5000 --account agency
/invoice list
/invoice list draft
/invoice list open
/invoice list open larry
/invoice void inv_abc123
/invoice status inv_abc123
```
