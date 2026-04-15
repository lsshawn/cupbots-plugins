# bill

Track billable work per client. Log items, edit amounts, tally pending, create Stripe invoices. Clients are pulled from wiki entities; invoicing chains into the invoice plugin for Stripe.

## Commands
- `bill`

## Intent
USE FOR: Log billable work, track hours/amounts, edit bill items, tally pending, create invoices from pending items, list clients
NOT FOR: Send standalone Stripe invoices without bill items (use /invoice), manage tasks (use /task), track expenses

## Primitives
/bill <amount> <description> [--client <name>]                                      Add a billable item
/bill list [--client <name>] [--all]                                                 Show pending items with total
/bill edit <id> [<amount>] [<description>]                                           Update amount and/or description
/bill delete <id>                                                                    Remove an item
/bill clients                                                                        List clients from wiki
/bill invoice --client <name> [--account <name>] [--note <text>] [--footer <text>]   Create Stripe invoice
/bill history [--client <name>] [--limit N]                                          Show invoiced history

## Examples
/bill 50 Redesign blog page
/bill 35 Fix login bug --client acme
/bill list
/bill list --client acme
/bill list --all
/bill edit 2 75
/bill edit 2 75 Redesign blog page v2
/bill delete 3
/bill clients
/bill invoice --client acme
/bill invoice --client acme --account agency
/bill invoice --client acme --note "April 2026 consulting"
/bill history --limit 10

## Rules
- Amount is first positional arg (number), description follows
- Client defaults to chat's company_id; use --client to override
- Client names are fuzzy-matched against wiki entities (company/organization/client types)
- /bill invoice requires --client; it creates a real Stripe invoice via the invoice plugin
- Client must have an email in the invoice plugin's client DB (created on first /invoice)
- --note sets the invoice description (shown at top of Stripe email)
- --footer sets the invoice footer (shown at bottom of Stripe email)
- /bill invoice marks items as invoiced after successful Stripe send
- /bill history shows previously invoiced items
- /bill list --all groups by client with grand total
