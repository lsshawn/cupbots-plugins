"""
Bill — Track billable work per client from chat.

Commands:
  /bill <amount> <description> [--client <name>]  — Log billable item
  /bill list [--client <name>] [--all]             — Show pending items + total
  /bill edit <id> [<amount>] [<description>]       — Update an item
  /bill delete <id>                                — Remove an item
  /bill clients                                    — List clients from wiki
  /bill invoice --client <name> [--account <name>] [--note <text>] [--footer <text>]  — Create Stripe invoice
  /bill history [--client <name>] [--limit N]      — Show invoiced history

Amount is in your local currency unit (no decimals needed).
Client defaults to the chat's company_id. Use --client for explicit override.
Invoice creates a real Stripe invoice via the invoice plugin and sends it to the client.

Examples:
  /bill 50 Redesign blog page
  /bill 35 Fix login bug --client acme
  /bill list
  /bill edit 2 75
  /bill edit 2 75 Redesign blog page v2
  /bill delete 3
  /bill invoice --client acme
  /bill invoice --client acme --account agency
  /bill invoice --client acme --note "April 2026 consulting"
  /bill invoice --client acme --footer "Payment due within 30 days"
  /bill history --limit 10
"""

import shlex
from datetime import datetime

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.events import emit
from cupbots.helpers.logger import get_logger

log = get_logger("bill")

PLUGIN_NAME = "bill"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS billable_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id  TEXT NOT NULL DEFAULT '',
            client      TEXT NOT NULL DEFAULT '',
            amount      REAL NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_by  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            invoiced_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_bill_company
            ON billable_items(company_id, invoiced_at);
        CREATE INDEX IF NOT EXISTS idx_bill_client
            ON billable_items(company_id, client, invoiced_at);
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Flag parser
# ---------------------------------------------------------------------------

def _parse_flags(args: list[str]) -> dict:
    """Parse --flag value pairs from args list."""
    try:
        tokens = shlex.split(" ".join(args))
    except ValueError:
        tokens = list(args)

    out: dict = {}
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].lower()
            values = []
            i += 1
            while i < len(tokens) and not tokens[i].startswith("--"):
                values.append(tokens[i])
                i += 1
            out[key] = " ".join(values).strip()
        else:
            positional.append(tok)
            i += 1
    if positional:
        out["_positional"] = positional
    return out


# ---------------------------------------------------------------------------
# Wiki client lookup
# ---------------------------------------------------------------------------

def _get_wiki_clients(company_id: str) -> list[str]:
    """Pull client/company entity names from wiki DB."""
    try:
        wiki_db = get_plugin_db("wiki")
        rows = wiki_db.execute(
            "SELECT DISTINCT name FROM entities WHERE company_id = ? AND entity_type IN ('company', 'organization', 'client') ORDER BY name",
            (company_id,)
        ).fetchall()
        return [r["name"] for r in rows]
    except Exception:
        return []


def _fuzzy_match_client(name: str, company_id: str) -> str:
    """Fuzzy match a client name against wiki entities. Returns best match or original."""
    clients = _get_wiki_clients(company_id)
    if not clients:
        return name

    name_lower = name.lower()

    # Exact match
    for c in clients:
        if c.lower() == name_lower:
            return c

    # Prefix match
    matches = [c for c in clients if c.lower().startswith(name_lower)]
    if len(matches) == 1:
        return matches[0]

    # Substring match
    matches = [c for c in clients if name_lower in c.lower()]
    if len(matches) == 1:
        return matches[0]

    return name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_client(flags: dict, company_id: str) -> str:
    """Resolve client from --client flag or company_id."""
    raw = flags.get("client", "").strip()
    if raw:
        return _fuzzy_match_client(raw, company_id)
    return company_id or "default"


def _format_items(rows, title: str) -> str:
    """Format billable items as a readable list."""
    if not rows:
        return f"*{title}*\nNo items."

    lines = [f"*{title}*\n"]
    total = 0
    for r in rows:
        total += r["amount"]
        amt = int(r["amount"]) if r["amount"] == int(r["amount"]) else r["amount"]
        lines.append(f"{r['id']}. {amt}: {r['description']}")

    total_display = int(total) if total == int(total) else f"{total:.2f}"
    lines.append(f"\n*Total: {total_display}*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def _cmd_add(args: list[str], sender_name: str, company_id: str) -> str:
    """Add a billable item."""
    flags = _parse_flags(args)
    positional = flags.get("_positional", [])

    if not positional or len(positional) < 2:
        return "Usage: /bill <amount> <description> [--client <name>]"

    try:
        amount = float(positional[0])
    except ValueError:
        return f"Invalid amount: {positional[0]}"

    description = " ".join(positional[1:])
    client = _resolve_client(flags, company_id)

    _db().execute(
        "INSERT INTO billable_items (company_id, client, amount, description, created_by) VALUES (?, ?, ?, ?, ?)",
        (company_id, client, amount, description, sender_name)
    )
    _db().commit()

    amt = int(amount) if amount == int(amount) else amount
    return f"Logged: {amt} — {description} ({client})"


async def _cmd_list(args: list[str], company_id: str) -> str:
    """List pending billable items."""
    flags = _parse_flags(args)
    show_all = "all" in flags

    client_filter = flags.get("client", "").strip()
    if client_filter:
        client_filter = _fuzzy_match_client(client_filter, company_id)

    if client_filter:
        rows = _db().execute(
            "SELECT * FROM billable_items WHERE company_id = ? AND client = ? AND invoiced_at IS NULL ORDER BY created_at",
            (company_id, client_filter)
        ).fetchall()
        title = f"Pending — {client_filter}"
    elif show_all:
        rows = _db().execute(
            "SELECT * FROM billable_items WHERE company_id = ? AND invoiced_at IS NULL ORDER BY client, created_at",
            (company_id,)
        ).fetchall()
        title = "Pending — All clients"
    else:
        rows = _db().execute(
            "SELECT * FROM billable_items WHERE company_id = ? AND invoiced_at IS NULL ORDER BY created_at",
            (company_id,)
        ).fetchall()
        title = "Pending"

    if show_all and rows:
        # Group by client
        by_client: dict[str, list] = {}
        for r in rows:
            by_client.setdefault(r["client"], []).append(r)

        parts = []
        grand_total = 0
        for client, items in sorted(by_client.items()):
            parts.append(_format_items(items, client))
            grand_total += sum(r["amount"] for r in items)

        gt = int(grand_total) if grand_total == int(grand_total) else f"{grand_total:.2f}"
        parts.append(f"\n*Grand total: {gt}*")
        return "\n\n".join(parts)

    return _format_items(rows, title)


async def _cmd_edit(args: list[str], company_id: str) -> str:
    """Edit a billable item's amount and/or description."""
    flags = _parse_flags(args)
    positional = flags.get("_positional", [])

    if not positional:
        return "Usage: /bill edit <id> [<amount>] [<description>]"

    try:
        item_id = int(positional[0])
    except ValueError:
        return f"Invalid ID: {positional[0]}"

    row = _db().execute(
        "SELECT * FROM billable_items WHERE id = ? AND company_id = ? AND invoiced_at IS NULL",
        (item_id, company_id)
    ).fetchone()

    if not row:
        return f"Item {item_id} not found or already invoiced."

    new_amount = row["amount"]
    new_desc = row["description"]

    if len(positional) >= 2:
        try:
            new_amount = float(positional[1])
            if len(positional) >= 3:
                new_desc = " ".join(positional[2:])
        except ValueError:
            # Not a number — treat everything after ID as description
            new_desc = " ".join(positional[1:])

    _db().execute(
        "UPDATE billable_items SET amount = ?, description = ? WHERE id = ?",
        (new_amount, new_desc, item_id)
    )
    _db().commit()

    amt = int(new_amount) if new_amount == int(new_amount) else new_amount
    return f"Updated #{item_id}: {amt} — {new_desc}"


async def _cmd_delete(args: list[str], company_id: str) -> str:
    """Delete a billable item."""
    if not args:
        return "Usage: /bill delete <id>"

    try:
        item_id = int(args[0])
    except ValueError:
        return f"Invalid ID: {args[0]}"

    result = _db().execute(
        "DELETE FROM billable_items WHERE id = ? AND company_id = ? AND invoiced_at IS NULL",
        (item_id, company_id)
    )
    _db().commit()

    if result.rowcount == 0:
        return f"Item {item_id} not found or already invoiced."
    return f"Deleted #{item_id}."


async def _cmd_clients(company_id: str) -> str:
    """List available clients from wiki."""
    clients = _get_wiki_clients(company_id)
    if not clients:
        return "No clients found in wiki. Add company/organization entities to wiki first."

    lines = ["*Clients (from wiki)*\n"]
    for c in clients:
        # Check pending count
        count = _db().execute(
            "SELECT COUNT(*) as n FROM billable_items WHERE company_id = ? AND client = ? AND invoiced_at IS NULL",
            (company_id, c)
        ).fetchone()["n"]
        suffix = f" ({count} pending)" if count else ""
        lines.append(f"  - {c}{suffix}")
    return "\n".join(lines)


async def _cmd_invoice(args: list[str], company_id: str, reply=None) -> str:
    """Create Stripe invoice from pending items, then mark as invoiced."""
    flags = _parse_flags(args)
    client_filter = flags.get("client", "").strip()
    account_name = flags.get("account", "").strip()

    note = flags.get("note", "").strip()
    footer = flags.get("footer", "").strip()

    if not client_filter:
        return "Usage: /bill invoice --client <name> [--account <name>] [--note <text>] [--footer <text>]"

    client_filter = _fuzzy_match_client(client_filter, company_id)

    rows = _db().execute(
        "SELECT * FROM billable_items WHERE company_id = ? AND client = ? AND invoiced_at IS NULL ORDER BY created_at",
        (company_id, client_filter)
    ).fetchall()

    if not rows:
        return f"No pending items for {client_filter}."

    # Build line items for Stripe
    line_items = []
    for r in rows:
        amt = int(r["amount"]) if r["amount"] == int(r["amount"]) else r["amount"]
        line_items.append({"description": r["description"], "amount": r["amount"], "currency": "usd"})

    total = sum(r["amount"] for r in rows)
    total_display = int(total) if total == int(total) else f"{total:.2f}"

    # Import invoice plugin internals
    try:
        from plugins.invoice.invoice import (
            _find_customer_by_name,
            _init_stripe_account,
            _find_or_create_customer,
            _resolve_client_account,
            _get_accounts_config,
        )
        import stripe as stripe_mod
    except ImportError:
        return "Invoice plugin not available. Install it first."

    # Resolve client email from invoice plugin's client DB
    existing = _find_customer_by_name(client_filter, company_id)
    if not existing:
        return (
            f"No email found for '{client_filter}'. "
            f"First create a Stripe invoice manually so the client is registered:\n"
            f"  /invoice {client_filter}@example.com <desc> <amount>"
        )

    client_email = existing["email"]
    client_name = existing["name"]

    # Resolve Stripe account
    if not account_name:
        account_name = _resolve_client_account(client_email, company_id) or "default"

    accounts = _get_accounts_config()
    if account_name not in accounts:
        available = ", ".join(sorted(accounts.keys()))
        return f"Unknown Stripe account '{account_name}'. Available: {available}"

    if not _init_stripe_account(account_name):
        return f"Stripe account '{account_name}' not configured."

    if reply:
        await reply.send_typing()

    # Find or create Stripe customer
    try:
        customer_id = _find_or_create_customer(client_name, client_email, company_id, account_name)
    except Exception as e:
        return f"Failed to find/create Stripe customer: {e}"

    # Create Stripe invoice with all pending items
    try:
        invoice_params = {
            "customer": customer_id,
            "collection_method": "send_invoice",
            "days_until_due": 30,
            "auto_advance": True,
        }
        if note:
            invoice_params["description"] = note
        if footer:
            invoice_params["footer"] = footer

        inv = stripe_mod.Invoice.create(**invoice_params)

        currency = line_items[0]["currency"]
        for item in line_items:
            stripe_mod.InvoiceItem.create(
                customer=customer_id,
                invoice=inv.id,
                description=item["description"],
                amount=int(item["amount"] * 100),  # Stripe uses cents
                currency=item.get("currency", currency),
            )

        # Finalize and send
        inv = stripe_mod.Invoice.finalize_invoice(inv.id)
        stripe_mod.Invoice.send_invoice(inv.id)

    except Exception as e:
        log.error("Stripe invoice creation failed: %s", e, exc_info=True)
        return f"Stripe invoice failed: {e}"

    # Store in invoice plugin's DB for tracking
    try:
        from plugins.invoice.invoice import _db as _invoice_db
        _invoice_db().execute(
            "INSERT INTO invoices (company_id, stripe_invoice_id, stripe_account, client_name, client_email, amount_total, currency, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, inv.id, account_name, client_name, client_email, inv.amount_due, currency, inv.status),
        )
        _invoice_db().commit()
    except Exception:
        log.warning("Could not log to invoice plugin DB — invoice was still created")

    # Mark bill items as invoiced
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ids = [r["id"] for r in rows]
    _db().execute(
        f"UPDATE billable_items SET invoiced_at = ? WHERE id IN ({','.join('?' * len(ids))})",
        [now] + ids
    )
    _db().commit()

    # Emit event (for AgentMail or other listeners)
    await emit("invoice.created", {
        "company_id": company_id,
        "stripe_invoice_id": inv.id,
        "stripe_account": account_name,
        "client_name": client_name,
        "client_email": client_email,
        "amount": inv.amount_due / 100,
        "currency": currency.upper(),
        "hosted_url": inv.hosted_invoice_url,
        "source": "bill",
        "line_items": [{"description": i["description"], "amount": i["amount"]} for i in line_items],
    })

    # Response
    result_lines = [
        f"Invoice sent to {client_name} ({client_email})",
        f"Amount: {total_display}",
        f"Account: {account_name}",
        f"Items: {len(rows)}",
        f"ID: {inv.id}",
    ]
    if inv.hosted_invoice_url:
        result_lines.append(f"Link: {inv.hosted_invoice_url}")

    return "\n".join(result_lines)


async def _cmd_history(args: list[str], company_id: str) -> str:
    """Show invoiced items history."""
    flags = _parse_flags(args)
    limit = int(flags.get("limit", "20"))
    client_filter = flags.get("client", "").strip()
    if client_filter:
        client_filter = _fuzzy_match_client(client_filter, company_id)

    if client_filter:
        rows = _db().execute(
            "SELECT * FROM billable_items WHERE company_id = ? AND client = ? AND invoiced_at IS NOT NULL ORDER BY invoiced_at DESC LIMIT ?",
            (company_id, client_filter, limit)
        ).fetchall()
        title = f"Invoiced history — {client_filter}"
    else:
        rows = _db().execute(
            "SELECT * FROM billable_items WHERE company_id = ? AND invoiced_at IS NOT NULL ORDER BY invoiced_at DESC LIMIT ?",
            (company_id, limit)
        ).fetchall()
        title = "Invoiced history"

    if not rows:
        return f"*{title}*\nNo invoiced items."

    lines = [f"*{title}*\n"]
    for r in rows:
        amt = int(r["amount"]) if r["amount"] == int(r["amount"]) else r["amount"]
        inv_date = r["invoiced_at"][:10]
        lines.append(f"  {amt}: {r['description']} ({r['client']}, invoiced {inv_date})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    """Command handler. Return True if handled, False to pass."""
    if msg.command != "bill":
        return False

    args = msg.args or []
    if not args:
        await reply.reply_text(__doc__.strip())
        return True

    sub = args[0].lower()

    if sub == "--help":
        await reply.reply_text(__doc__.strip())
        return True

    if sub == "list":
        result = await _cmd_list(args[1:], msg.company_id)
        await reply.reply_text(result)
        return True

    if sub == "edit":
        result = await _cmd_edit(args[1:], msg.company_id)
        await reply.reply_text(result)
        return True

    if sub == "delete":
        result = await _cmd_delete(args[1:], msg.company_id)
        await reply.reply_text(result)
        return True

    if sub == "clients":
        result = await _cmd_clients(msg.company_id)
        await reply.reply_text(result)
        return True

    if sub == "invoice":
        result = await _cmd_invoice(args[1:], msg.company_id, reply)
        await reply.reply_text(result)
        return True

    if sub == "history":
        result = await _cmd_history(args[1:], msg.company_id)
        await reply.reply_text(result)
        return True

    # Default: treat as add (first arg should be amount)
    result = await _cmd_add(args, msg.sender_name, msg.company_id)
    await reply.reply_text(result)
    return True
