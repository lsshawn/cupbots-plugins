# Finance

Double-entry bookkeeping over WhatsApp. Send a receipt photo or type a description — the bot parses it into beancount journal entries, validates with `bean-check`, and files the document.

## How It Works

1. User sends `/finance expense lunch at cafe` (or attaches a receipt image)
2. LLM parses into a beancount transaction (payee, amount, account, date, receipt ID)
3. Duplicate check runs against existing journal (see below)
4. Entry is appended to `journal.beancount`, formatted, and validated
5. Receipt/invoice is moved to the correct documents folder (e.g. `Expenses/Food/Restaurants/2026-04-15-cafe.jpg`)
6. Journal summary is regenerated

Steps 3-6 are automatic — no confirmation buttons needed.

## Duplicate Detection

Every recording command (`expense`, `income`, `transfer`, `ar`, `payment`, and batch `scan`) checks for duplicates before writing. No LLM call — pure regex fingerprinting against the journal in one pass.

Three tiers, from strongest to weakest signal:

| Tier | Signal | Catches |
|------|--------|---------|
| **1. Exact ID** | Receipt/invoice number already in journal | Same receipt submitted twice, forwarded invoice re-processed |
| **2. Same payee** | Same payee + similar amount + ±3 day window | "Rimbarapi" RM65.05 on Mar 30 vs re-submitted on Mar 31 |
| **3. Similar payee/narration** | Word overlap in payee or narration + similar amount + ±3 days | "Shell" vs "Shell Station Ampang", or different payee but narration both say "petrol" |

Amount tolerance: within 1% or ±0.50 (handles rounding, FX conversion differences). Common filler words ("sdn", "bhd", "the") are excluded from word matching to avoid false positives on Malaysian company names.

Things it correctly **won't** flag:
- Different vendors, same amount, same day (RM50 TNG reload vs RM50 groceries)
- Same vendor, different amounts, same day (two gas station trips)
- Same vendor, same amount, months apart (recurring subscription)

## Ledgers

Two independent ledgers, each with its own `journal.beancount`:

- **cupbots** (default) — business expenses, income, invoicing
- **personal** — personal finance, Malaysian tax relief accounts

Add `personal` after the subcommand to target the personal ledger: `/finance expense personal groceries`.

## Receipt/Invoice Filing

When an attachment is provided, the file is moved to the beancount documents folder after recording:

```
finances/<ledger>/Expenses/Food/Restaurants/2026-04-15-cafe.jpg
finances/<ledger>/Income/Clients/Acme/2026-04-15-invoice.pdf
```

The LLM suggests the destination path and filename. If it doesn't, the plugin derives one from the posting date, expense account, and narration.

## Batch Scan

`/finance` (no subcommand) scans the `Income/` and `Expenses/` root folders for unprocessed files (PDF, JPG, PNG) and processes each one through OCR, duplicate check, journal entry, validation, and filing.

## Commands

```
/finance [cupbots|personal] [income|expenses]  — Scan & process unprocessed invoices
/finance expense [personal] <desc>     — Add expense (attach receipt)
/finance income [personal] <desc>      — Record income (attach invoice)
/finance transfer [personal] <desc>    — Inter-account transfer
/finance ar [personal] <desc>          — Record invoice sent (AR entry)
/finance payment [personal] <desc>     — Record payment received (clear AR)
/finance void [personal] <search>      — Void an entry (reversing entry)
/finance validate [personal]           — Run bean-check
/finance fxsync                        — Fetch missing FX rates
/finance summary [personal]            — Regenerate journal summary
/finance reconcile [personal] <acct> <bal> <cur> — Reconcile account
/finance duplicates [personal]         — Scan for duplicate entries
/finance invoice <client> <items> [--account NAME] [--proposal REF] — Create & send Stripe invoice
/finance invoice list [client]         — List invoices
/finance invoice status <id>           — Check invoice status
/finance invoice accounts              — List Stripe accounts
```

## Config

All settings live in `plugin_settings.finance` in config.yaml:

```yaml
plugin_settings:
  finance:
    # Stripe invoicing (optional)
    stripe_accounts:
      default: "My Company"
```

The `allowed_paths.finances` config key points to the beancount root directory (default: `/home/ss/projects/note/finances`). Each ledger is a subdirectory with `journal.beancount`, `journal_summary.beancount`, and document folders.
