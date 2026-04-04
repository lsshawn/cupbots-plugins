"""
Launchpad — Deploy landing pages from WhatsApp.

Commands (works in any topic):
  /lp <description>       — Create a new landing page from a text prompt
  /lp sites               — List your deployed sites
  /lp edit <site> — <change> — Edit an existing page (e.g. change headline)
  /lp status <site>       — Check deploy status and analytics link
  /lp domain <site> <domain> — Connect a custom domain

Examples:
  /lp A coffee shop in Brooklyn, warm earthy tones, shows menu and hours
  /lp sites
  /lp edit my-coffee — change the headline to "Fresh Roasted Daily"
  /lp domain my-coffee mycoffeeshop.com
"""

import os
import json

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger

log = get_logger("launchpad")

PLUGIN_NAME = "launchpad"
API_TIMEOUT = 120  # deploys can take a minute


# ---------------------------------------------------------------------------
# Database — track sites per tenant
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            site_slug TEXT NOT NULL,
            site_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            prompt TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sites_company ON sites (company_id);
        CREATE INDEX IF NOT EXISTS idx_sites_slug ON sites (company_id, site_slug);
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# API client — all heavy lifting delegated to the Launchpad service
# ---------------------------------------------------------------------------

def _get_config():
    """Read API config. Returns (api_url, api_key) or raises."""
    api_url = os.environ.get("LAUNCHPAD_API_URL", "").rstrip("/")
    api_key = os.environ.get("LAUNCHPAD_API_KEY", "")
    if not api_url or not api_key:
        raise ValueError(
            "Launchpad not configured. Run:\n"
            "/plugin config launchpad LAUNCHPAD_API_URL <url>\n"
            "/plugin config launchpad LAUNCHPAD_API_KEY <key>"
        )
    return api_url, api_key


async def _api_call(method: str, path: str, payload: dict | None = None) -> dict:
    """Make an authenticated request to the Launchpad service."""
    api_url, api_key = _get_config()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        if method == "GET":
            resp = await client.get(f"{api_url}{path}", headers=headers)
        else:
            resp = await client.post(f"{api_url}{path}", headers=headers, json=payload or {})
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _create_site(prompt: str, company_id: str) -> str:
    """Send prompt to Launchpad service, store result, return message."""
    result = await _api_call("POST", "/sites", {
        "prompt": prompt,
        "company_id": company_id,
    })

    slug = result.get("slug", "")
    url = result.get("url", "")
    status = result.get("status", "pending")

    conn = _db()
    conn.execute(
        "INSERT INTO sites (company_id, site_slug, site_url, status, prompt) VALUES (?, ?, ?, ?, ?)",
        (company_id, slug, url, status, prompt),
    )
    conn.commit()

    if status == "live":
        return f"Your site is live!\n\n{url}\n\nTo edit: /lp edit {slug} — <your change>\nTo add your domain: /lp domain {slug} yourdomain.com"
    return f"Building your site ({slug})... I'll message you when it's ready."


async def _list_sites(company_id: str) -> str:
    """List all sites for this tenant."""
    conn = _db()
    rows = conn.execute(
        "SELECT site_slug, site_url, status, created_at FROM sites WHERE company_id = ? ORDER BY created_at DESC LIMIT 20",
        (company_id,),
    ).fetchall()

    if not rows:
        return "No sites yet. Create one with:\n/lp A landing page for my coffee shop"

    lines = ["Your sites:\n"]
    for slug, url, status, created in rows:
        icon = "\u2705" if status == "live" else "\u23f3"
        lines.append(f"{icon} {slug} — {url or 'building...'}")
    return "\n".join(lines)


async def _edit_site(slug: str, change: str, company_id: str) -> str:
    """Request an edit to an existing site."""
    conn = _db()
    row = conn.execute(
        "SELECT id FROM sites WHERE company_id = ? AND site_slug = ?",
        (company_id, slug),
    ).fetchone()
    if not row:
        return f"Site '{slug}' not found. Check /lp sites"

    result = await _api_call("POST", f"/sites/{slug}/edit", {
        "change": change,
        "company_id": company_id,
    })

    url = result.get("url", "")
    conn.execute(
        "UPDATE sites SET updated_at = datetime('now') WHERE company_id = ? AND site_slug = ?",
        (company_id, slug),
    )
    conn.commit()

    return f"Updated! {url}"


async def _site_status(slug: str, company_id: str) -> str:
    """Get deploy status and analytics for a site."""
    result = await _api_call("GET", f"/sites/{slug}?company_id={company_id}")
    status = result.get("status", "unknown")
    url = result.get("url", "")
    analytics = result.get("analytics_url", "")

    lines = [f"Site: {slug}", f"Status: {status}"]
    if url:
        lines.append(f"URL: {url}")
    if analytics:
        lines.append(f"Analytics: {analytics}")
    return "\n".join(lines)


async def _set_domain(slug: str, domain: str, company_id: str) -> str:
    """Connect a custom domain to a site."""
    result = await _api_call("POST", f"/sites/{slug}/domain", {
        "domain": domain,
        "company_id": company_id,
    })

    instructions = result.get("dns_instructions", "")
    return f"Domain setup started for {domain}\n\n{instructions}"


# ---------------------------------------------------------------------------
# Cross-platform handler (REQUIRED)
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "lp":
        return False

    args = msg.args
    company_id = msg.company_id or msg.sender_id

    try:
        # /lp sites
        if args and args[0] == "sites":
            result = await _list_sites(company_id)
            await reply.reply_text(result)
            return True

        # /lp edit <slug> — <change>
        if args and args[0] == "edit":
            raw = " ".join(args[1:])
            if "\u2014" in raw:
                slug, change = raw.split("\u2014", 1)
            elif " - " in raw:
                slug, change = raw.split(" - ", 1)
            else:
                await reply.reply_text("Usage: /lp edit <site-name> \u2014 <what to change>")
                return True
            result = await _edit_site(slug.strip(), change.strip(), company_id)
            await reply.reply_text(result)
            return True

        # /lp status <slug>
        if args and args[0] == "status":
            slug = args[1] if len(args) > 1 else ""
            if not slug:
                await reply.reply_text("Usage: /lp status <site-name>")
                return True
            result = await _site_status(slug, company_id)
            await reply.reply_text(result)
            return True

        # /lp domain <slug> <domain>
        if args and args[0] == "domain":
            if len(args) < 3:
                await reply.reply_text("Usage: /lp domain <site-name> yourdomain.com")
                return True
            result = await _set_domain(args[1], args[2], company_id)
            await reply.reply_text(result)
            return True

        # /lp <prompt> — create a new site
        if args:
            prompt = " ".join(args)
            await reply.reply_text("Brewing your landing page... this takes about a minute.")
            result = await _create_site(prompt, company_id)
            await reply.reply_text(result)
            return True

        # /lp (no args)
        await reply.reply_text(
            "Launchpad \u2014 deploy landing pages from chat\n\n"
            "/lp <description> \u2014 create a new site\n"
            "/lp sites \u2014 list your sites\n"
            "/lp edit <site> \u2014 <change> \u2014 tweak a page\n"
            "/lp status <site> \u2014 check deploy + analytics\n"
            "/lp domain <site> <domain> \u2014 connect your domain"
        )
        return True

    except ValueError as e:
        await reply.reply_text(str(e))
        return True
    except httpx.HTTPStatusError as e:
        log.error("Launchpad API error: %s", e)
        await reply.reply_text("Something went wrong with the deploy service. Try again in a moment.")
        return True
    except httpx.ConnectError:
        await reply.reply_text("Can't reach the Launchpad service. Is it running?")
        return True


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_lp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    # Telegram handler delegates to a minimal shim — real logic in handle_command
    from cupbots.helpers.channel import IncomingMessage, ReplyContext

    msg = IncomingMessage.from_telegram(update, context)
    ctx = ReplyContext.from_telegram(update)
    await handle_command(msg, ctx)


def register(app: Application):
    app.add_handler(CommandHandler("lp", cmd_lp))
