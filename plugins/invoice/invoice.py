"""
Invoice — Create and send Stripe invoices from chat.

Commands:
  /invoice <client> <line-items> [--account NAME] [--proposal REF] [--draft]  — Create invoice
  /invoice send <invoice-id>                        — Send a draft invoice
  /invoice void <invoice-id>                        — Void an open invoice or delete a draft
  /invoice list [draft|open|paid|void] [client]      — List invoices, filter by status
  /invoice status <invoice-id>                      — Check invoice status
  /invoice accounts                                 — List configured Stripe accounts

Line items format (comma-separated):
  <description> <amount> [currency]

Flags:
  --draft      Create as draft (finalized but not emailed). Use /invoice send to email later.
  --account    Stripe account name (default: client's previous account or 'default').
  --proposal   Attach a proposal reference to the invoice footer.

Config (plugin_settings.invoice):
  auto_send: true/false   — Default send behavior. When false, invoices are created as drafts
                            unless you explicitly send them. (default: true)

Stripe accounts (config.yaml):
  invoice.accounts.default.secret_key: sk_live_...
  invoice.accounts.agency.secret_key: sk_live_...

If --account is omitted, uses the client's previously used account, or 'default'.

Examples:
  /invoice acme@example.com API integration 5000, Monthly hosting 200
  /invoice acme@example.com API integration 5000 --draft
  /invoice send inv_abc123
  /invoice "Acme Corp" Web development 3000 USD, Design 1500 USD --proposal PROP-2024-003
  /invoice acme@example.com Web dev 5000 --account agency
  /invoice list
  /invoice list acme
  /invoice status inv_abc123
  /invoice accounts
"""

import os
import re
from datetime import datetime

import stripe as stripe_mod

from cupbots.config import get_config
from cupbots.helpers.access import is_admin
from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.events import emit
from cupbots.helpers.logger import get_logger

log = get_logger("invoice")

PLUGIN_NAME = "invoice"


def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            stripe_customer_id TEXT NOT NULL,
            stripe_account TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_email_account
            ON clients(company_id, email, stripe_account);

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            stripe_invoice_id TEXT NOT NULL,
            stripe_account TEXT NOT NULL DEFAULT 'default',
            client_name TEXT NOT NULL DEFAULT '',
            client_email TEXT NOT NULL DEFAULT '',
            amount_total INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'usd',
            status TEXT NOT NULL DEFAULT 'draft',
            proposal_ref TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Multi-account Stripe helpers
# ---------------------------------------------------------------------------

def _get_accounts_config() -> dict:
    """Get invoice accounts from config.yaml.

    Falls back to a single 'default' account using stripe.mode logic.
    """
    cfg = get_config()
    accounts = cfg.get("invoice", {}).get("accounts", {})
    if accounts:
        return accounts
    # Fallback: use stripe.<mode>.secret_key as default account
    stripe_cfg = cfg.get("stripe", {})
    mode = stripe_cfg.get("mode", "live")
    key = stripe_cfg.get(mode, {}).get("secret_key", "")
    return {"default": {"secret_key": key}}


def _get_stripe_key_for_account(account_name: str) -> str | None:
    """Get the Stripe secret key for a named account."""
    accounts = _get_accounts_config()
    acct = accounts.get(account_name)
    if not acct:
        return None
    return acct.get("secret_key", "") or None


def _init_stripe_account(account_name: str) -> bool:
    """Set stripe API key for the given account. Returns False if not configured."""
    key = _get_stripe_key_for_account(account_name)
    if not key:
        return False
    stripe_mod.api_key = key
    return True


def _resolve_client_account(email: str, company_id: str) -> str | None:
    """Look up which Stripe account a client was previously invoiced from."""
    conn = _db()
    row = conn.execute(
        "SELECT stripe_account FROM clients WHERE company_id = ? AND email = ? ORDER BY created_at DESC LIMIT 1",
        (company_id, email),
    ).fetchone()
    return row[0] if row else None


def _find_or_create_customer(name: str, email: str, company_id: str, account_name: str) -> str:
    """Find existing Stripe customer by email on this account, or create one. Returns customer ID."""
    conn = _db()
    row = conn.execute(
        "SELECT stripe_customer_id FROM clients WHERE company_id = ? AND email = ? AND stripe_account = ?",
        (company_id, email, account_name),
    ).fetchone()
    if row:
        return row[0]

    # Search Stripe (key already set for this account)
    results = stripe_mod.Customer.search(query=f'email:"{email}"', limit=1)
    if results.data:
        cust = results.data[0]
        conn.execute(
            "INSERT OR REPLACE INTO clients (company_id, name, email, stripe_customer_id, stripe_account) VALUES (?, ?, ?, ?, ?)",
            (company_id, name, email, cust.id, account_name),
        )
        conn.commit()
        return cust.id

    # Create new customer
    cust = stripe_mod.Customer.create(name=name, email=email)
    conn.execute(
        "INSERT INTO clients (company_id, name, email, stripe_customer_id, stripe_account) VALUES (?, ?, ?, ?, ?)",
        (company_id, name, email, cust.id, account_name),
    )
    conn.commit()
    log.info("Created Stripe customer %s for %s (%s) on account '%s'", cust.id, name, email, account_name)
    return cust.id


def _find_customer_by_name(name: str, company_id: str) -> dict | None:
    """Fuzzy-find a customer by name from local cache."""
    conn = _db()
    row = conn.execute(
        "SELECT name, email, stripe_customer_id, stripe_account FROM clients WHERE company_id = ? AND LOWER(name) LIKE ?",
        (company_id, f"%{name.lower()}%"),
    ).fetchone()
    return dict(row) if row else None


def _search_stripe_customer_by_name(name: str, company_id: str, account_name: str) -> dict | None:
    """Search Stripe for a customer by name, cache locally if found.

    Stripe API key must already be set via _init_stripe_account().
    """
    try:
        results = stripe_mod.Customer.search(query=f'name~"{name}"', limit=1)
        if not results.data:
            return None
        cust = results.data[0]
        if not cust.email:
            log.warning("Stripe customer %s (%s) has no email — skipping", cust.id, cust.name)
            return None
        # Cache locally
        conn = _db()
        conn.execute(
            "INSERT OR REPLACE INTO clients (company_id, name, email, stripe_customer_id, stripe_account) VALUES (?, ?, ?, ?, ?)",
            (company_id, cust.name or name, cust.email, cust.id, account_name),
        )
        conn.commit()
        log.info("Found Stripe customer %s (%s) by name search, cached locally", cust.id, cust.name)
        return {"name": cust.name or name, "email": cust.email, "stripe_customer_id": cust.id}
    except Exception as e:
        log.warning("Stripe customer name search failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Line-item parsing
# ---------------------------------------------------------------------------

def _parse_line_items(text: str) -> list[dict]:
    """Parse comma-separated line items: 'Description amount [currency]'.

    Examples:
      'API integration 5000, Monthly hosting 200'
      'Web dev 3000 USD, Design 1500 EUR'
    """
    items = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue

        # Match trailing amount and optional currency: ... 5000 [USD]
        m = re.match(r"^(.+?)\s+([\d,.]+)\s*([A-Za-z]{3})?$", part)
        if not m:
            items.append({"description": part, "amount": 0, "currency": "usd"})
            continue

        desc = m.group(1).strip().strip("'\"")
        amount = float(m.group(2).replace(",", ""))
        currency = (m.group(3) or "usd").lower()
        items.append({"description": desc, "amount": amount, "currency": currency})

    return items


# ---------------------------------------------------------------------------
# Parse the full command text
# ---------------------------------------------------------------------------

def _parse_invoice_args(args: list[str]) -> dict:
    """Parse: <client> <line-items> [--account NAME] [--proposal REF]

    Client can be an email or a quoted/unquoted name.
    Everything after the client up to flags is line items.
    """
    if not args:
        return {}

    text = " ".join(args)

    # Extract --draft
    draft = bool(re.search(r"--draft\b", text))
    text = re.sub(r"--draft\b\s*", "", text)

    # Extract --proposal
    proposal_ref = None
    proposal_match = re.search(r"--proposal\s+(\S+)", text)
    if proposal_match:
        proposal_ref = proposal_match.group(1)
        text = text[:proposal_match.start()].rstrip() + text[proposal_match.end():]

    # Extract --account
    account_name = None
    account_match = re.search(r"--account\s+(\S+)", text)
    if account_match:
        account_name = account_match.group(1)
        text = text[:account_match.start()].rstrip() + text[account_match.end():]

    text = text.strip()

    # Extract client (first token — email or quoted string or single word)
    client_name = ""
    client_email = ""
    remainder = text

    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            client_name = text[1:end]
            remainder = text[end + 1:].strip()
    elif "@" in text.split()[0]:
        client_email = text.split()[0]
        remainder = " ".join(text.split()[1:])
    else:
        client_name = text.split()[0]
        remainder = " ".join(text.split()[1:])

    line_items = _parse_line_items(remainder) if remainder else []

    return {
        "client_name": client_name,
        "client_email": client_email,
        "line_items": line_items,
        "proposal_ref": proposal_ref,
        "account": account_name,
        "draft": draft,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def _cmd_invoice_create(msg, reply) -> None:
    """Create and send a Stripe invoice."""
    parsed = _parse_invoice_args(msg.args)
    if not parsed or (not parsed["client_name"] and not parsed["client_email"]):
        await reply.reply_text(__doc__.strip())
        return

    if not parsed["line_items"]:
        await reply.reply_text("No line items found. Use: /invoice <client> <desc> <amount>, <desc> <amount>, ...")
        return

    # Validate amounts
    for item in parsed["line_items"]:
        if item["amount"] <= 0:
            await reply.reply_text(f"Invalid amount for '{item['description']}'. Each item needs a positive number.")
            return

    company_id = msg.company_id or "default"
    client_name = parsed["client_name"]
    client_email = parsed["client_email"]

    # Resolve client
    if client_email and not client_name:
        client_name = client_email.split("@")[0].title()

    # Resolve name from local DB first
    if client_name and not client_email:
        existing = _find_customer_by_name(client_name, company_id)
        if existing:
            client_email = existing["email"]
            client_name = existing["name"]

    # Resolve Stripe account: explicit flag > client history > default
    account_name = parsed["account"]
    if not account_name and client_email:
        account_name = _resolve_client_account(client_email, company_id) or "default"
    if not account_name:
        account_name = "default"

    # Validate account exists in config
    accounts = _get_accounts_config()
    if account_name not in accounts:
        available = ", ".join(sorted(accounts.keys()))
        await reply.reply_text(f"Unknown account '{account_name}'. Available: {available}")
        return

    if not _init_stripe_account(account_name):
        env_key = accounts[account_name].get("env_key", "?")
        await reply.reply_text(f"Stripe account '{account_name}' not configured. Set env var: {env_key}")
        return

    await reply.send_typing()

    # If name-only and local DB missed, search Stripe by name
    if client_name and not client_email:
        cust = _search_stripe_customer_by_name(client_name, company_id, account_name)
        if cust:
            client_email = cust["email"]
            client_name = cust["name"]
        else:
            await reply.reply_text(
                f"No client found matching '{client_name}' in local DB or Stripe. "
                f"Use their email: /invoice client@example.com <items>"
            )
            return

    # Find or create Stripe customer
    try:
        customer_id = _find_or_create_customer(client_name, client_email, company_id, account_name)
    except Exception as e:
        await reply.reply_text(f"Failed to find/create customer: {e}")
        return

    # Determine currency from first line item
    currency = parsed["line_items"][0]["currency"]

    # Determine send behavior before creating
    cfg = get_config()
    auto_send = cfg.get("plugin_settings", {}).get("invoice", {}).get("auto_send", True)
    should_send = not parsed["draft"] and auto_send

    # Create invoice
    try:
        invoice_params = {
            "customer": customer_id,
            "collection_method": "send_invoice",
            "days_until_due": 30,
            "auto_advance": should_send,
        }
        if parsed["proposal_ref"]:
            invoice_params["footer"] = f"Ref: {parsed['proposal_ref']}"

        inv = stripe_mod.Invoice.create(**invoice_params)

        # Add line items
        for item in parsed["line_items"]:
            stripe_mod.InvoiceItem.create(
                customer=customer_id,
                invoice=inv.id,
                description=item["description"],
                amount=int(item["amount"] * 100),  # Stripe uses cents
                currency=item.get("currency", currency),
            )

        # Finalize — auto_advance handles sending when should_send is true
        inv = stripe_mod.Invoice.finalize_invoice(inv.id)

        # Store locally
        conn = _db()
        conn.execute(
            "INSERT INTO invoices (company_id, stripe_invoice_id, stripe_account, client_name, client_email, amount_total, currency, status, proposal_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, inv.id, account_name, client_name, client_email, inv.amount_due, currency, inv.status, parsed["proposal_ref"]),
        )
        conn.commit()

        # Format response
        total = inv.amount_due / 100
        if should_send:
            lines = [f"Invoice sent to {client_name} ({client_email})"]
        else:
            lines = [f"Invoice created as draft for {client_name} ({client_email})", "Use `/invoice send {inv_id}` to email it.".format(inv_id=inv.id)]
        lines += [
            f"Amount: {total:,.2f} {currency.upper()}",
            f"Account: {account_name}",
            f"ID: {inv.id}",
            f"Due: {datetime.fromtimestamp(inv.due_date).strftime('%Y-%m-%d')}" if inv.due_date else "Due: 30 days",
        ]
        if parsed["proposal_ref"]:
            lines.append(f"Proposal: {parsed['proposal_ref']}")
        if inv.hosted_invoice_url:
            lines.append(f"Preview: {inv.hosted_invoice_url}")

        await reply.reply_text("\n".join(lines))

        # Emit event for finance plugin / AgentMail
        await emit("invoice.created", {
            "company_id": company_id,
            "stripe_invoice_id": inv.id,
            "stripe_account": account_name,
            "client_name": client_name,
            "client_email": client_email,
            "amount": total,
            "currency": currency.upper(),
            "proposal_ref": parsed["proposal_ref"],
            "hosted_url": inv.hosted_invoice_url,
            "line_items": [{"description": i["description"], "amount": i["amount"], "currency": i["currency"].upper()} for i in parsed["line_items"]],
        })

    except Exception as e:
        log.error("Invoice creation failed: %s", e, exc_info=True)
        await reply.reply_text(f"Failed to create invoice: {e}")


_VALID_STATUSES = {"draft", "open", "paid", "void", "uncollectible"}


async def _cmd_invoice_list(args: list[str], company_id: str) -> str:
    """List recent invoices, optionally filtered by status or client.

    Usage: /invoice list [--status draft|open|paid|void|uncollectible] [client]
    """
    conn = _db()
    status_filter = None
    remaining = []

    # Extract --status flag
    i = 0
    while i < len(args):
        if args[i] == "--status" and i + 1 < len(args):
            status_filter = args[i + 1].lower()
            i += 2
        elif args[i].lower() in _VALID_STATUSES and not remaining:
            # Allow bare status word as shorthand: /invoice list draft
            status_filter = args[i].lower()
            i += 1
        else:
            remaining.append(args[i])
            i += 1

    if status_filter and status_filter not in _VALID_STATUSES:
        return f"Unknown status '{status_filter}'. Valid: {', '.join(sorted(_VALID_STATUSES))}"

    query = "SELECT * FROM invoices WHERE company_id = ?"
    params: list = [company_id]

    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if remaining:
        query += " AND LOWER(client_name) LIKE ?"
        params.append(f"%{' '.join(remaining).lower()}%")

    query += " ORDER BY created_at DESC LIMIT 30"
    rows = conn.execute(query, params).fetchall()

    if not rows:
        label = f" ({status_filter})" if status_filter else ""
        return f"No invoices found{label}."

    status_icon_map = {"paid": "\u2705", "open": "\u23f3", "void": "\u274c", "draft": "\u270f\ufe0f", "uncollectible": "\u26a0\ufe0f"}
    label = f" — {status_filter}" if status_filter else ""
    lines = [f"Invoices{label}:\n"]
    for r in rows:
        r = dict(r)
        amount = r["amount_total"] / 100
        icon = status_icon_map.get(r["status"], "\u2753")
        acct = f" [{r['stripe_account']}]" if r.get("stripe_account", "default") != "default" else ""
        line = f"{icon} {r['client_name']} — {amount:,.2f} {r['currency'].upper()}{acct} — {r['stripe_invoice_id']}"
        if r.get("proposal_ref"):
            line += f" ({r['proposal_ref']})"
        lines.append(line)

    return "\n".join(lines)


async def _cmd_invoice_status(invoice_id: str, account_name: str) -> str:
    """Get current status of an invoice from Stripe."""
    if not _init_stripe_account(account_name):
        return f"Stripe account '{account_name}' not configured."

    try:
        inv = stripe_mod.Invoice.retrieve(invoice_id)
        amount = (inv.amount_due or 0) / 100
        paid = (inv.amount_paid or 0) / 100
        lines = [
            f"Invoice: {inv.id}",
            f"Customer: {inv.customer_name or inv.customer_email or inv.customer}",
            f"Status: {inv.status}",
            f"Amount: {amount:,.2f} {(inv.currency or 'usd').upper()}",
            f"Paid: {paid:,.2f} {(inv.currency or 'usd').upper()}",
        ]
        if inv.due_date:
            lines.append(f"Due: {datetime.fromtimestamp(inv.due_date).strftime('%Y-%m-%d')}")
        if inv.hosted_invoice_url:
            lines.append(f"Link: {inv.hosted_invoice_url}")
        return "\n".join(lines)
    except stripe_mod.error.InvalidRequestError:
        return f"Invoice not found: {invoice_id}"
    except Exception as e:
        return f"Error: {e}"


async def _cmd_invoice_send(invoice_id: str, company_id: str) -> str:
    """Send a finalized (draft) invoice."""
    conn = _db()
    row = conn.execute(
        "SELECT stripe_account FROM invoices WHERE stripe_invoice_id = ? AND company_id = ?",
        (invoice_id, company_id),
    ).fetchone()
    account_name = row[0] if row else "default"

    if not _init_stripe_account(account_name):
        return f"Stripe account '{account_name}' not configured."

    try:
        inv = stripe_mod.Invoice.retrieve(invoice_id)
        if inv.status not in ("open", "draft"):
            return f"Invoice {invoice_id} is already {inv.status} — cannot send."
        stripe_mod.Invoice.send_invoice(invoice_id)
        # Update local status
        conn.execute(
            "UPDATE invoices SET status = 'open' WHERE stripe_invoice_id = ?", (invoice_id,)
        )
        conn.commit()
        total = (inv.amount_due or 0) / 100
        return f"Invoice sent to {inv.customer_email or inv.customer_name or inv.customer}\nAmount: {total:,.2f} {(inv.currency or 'usd').upper()}\nID: {inv.id}"
    except stripe_mod.error.InvalidRequestError as e:
        return f"Failed to send invoice: {e}"
    except Exception as e:
        return f"Error: {e}"


async def _cmd_invoice_void(invoice_id: str, company_id: str) -> str:
    """Void an open invoice. Paid invoices cannot be voided."""
    conn = _db()
    row = conn.execute(
        "SELECT stripe_account FROM invoices WHERE stripe_invoice_id = ? AND company_id = ?",
        (invoice_id, company_id),
    ).fetchone()
    account_name = row[0] if row else "default"

    if not _init_stripe_account(account_name):
        return f"Stripe account '{account_name}' not configured."

    try:
        inv = stripe_mod.Invoice.retrieve(invoice_id)
        if inv.status == "void":
            return f"Invoice {invoice_id} is already voided."
        if inv.status == "paid":
            return f"Invoice {invoice_id} is paid — cannot void. Use Stripe Dashboard to issue a refund."
        if inv.status == "draft":
            stripe_mod.Invoice.delete(invoice_id)
            conn.execute("UPDATE invoices SET status = 'void' WHERE stripe_invoice_id = ?", (invoice_id,))
            conn.commit()
            return f"Draft invoice {invoice_id} deleted."
        inv = stripe_mod.Invoice.void_invoice(invoice_id)
        conn.execute("UPDATE invoices SET status = 'void' WHERE stripe_invoice_id = ?", (invoice_id,))
        conn.commit()
        return f"Invoice {invoice_id} voided ({inv.customer_name or inv.customer_email or inv.customer})."
    except stripe_mod.error.InvalidRequestError as e:
        return f"Failed to void invoice: {e}"
    except Exception as e:
        return f"Error: {e}"


async def _cmd_accounts() -> str:
    """List configured Stripe accounts."""
    accounts = _get_accounts_config()
    if not accounts:
        return "No invoice accounts configured."

    lines = ["Stripe accounts:\n"]
    for name, acct in accounts.items():
        has_key = bool(acct.get("secret_key", ""))
        status = "\u2705" if has_key else "\u274c"
        lines.append(f"  {status} {name}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "invoice":
        return False

    args = msg.args or []

    if args and args[0] in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    if not is_admin(msg.platform, msg.sender_id) and not msg.sender_role:
        await reply.reply_text("Invoice commands are restricted.")
        return True

    company_id = msg.company_id or "default"

    if args and args[0] == "list":
        result = await _cmd_invoice_list(args[1:], company_id)
        await reply.reply_text(result)
        return True

    if args and args[0] == "send":
        if len(args) < 2:
            await reply.reply_text("Usage: /invoice send <invoice-id>")
            return True
        result = await _cmd_invoice_send(args[1], company_id)
        await reply.reply_text(result)
        return True

    if args and args[0] == "void":
        if len(args) < 2:
            await reply.reply_text("Usage: /invoice void <invoice-id>")
            return True
        result = await _cmd_invoice_void(args[1], company_id)
        await reply.reply_text(result)
        return True

    if args and args[0] == "status":
        if len(args) < 2:
            await reply.reply_text("Usage: /invoice status <invoice-id>")
            return True
        # Look up which account this invoice belongs to
        conn = _db()
        row = conn.execute(
            "SELECT stripe_account FROM invoices WHERE stripe_invoice_id = ?", (args[1],)
        ).fetchone()
        account_name = row[0] if row else "default"
        result = await _cmd_invoice_status(args[1], account_name)
        await reply.reply_text(result)
        return True

    if args and args[0] == "accounts":
        result = await _cmd_accounts()
        await reply.reply_text(result)
        return True

    # Default: create invoice
    await _cmd_invoice_create(msg, reply)
    return True
