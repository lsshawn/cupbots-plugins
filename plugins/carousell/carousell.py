"""
Carousell Price Tracker — Search & track deals on Carousell MY/SG.

Commands (works in any topic):
  /carousell <query>         — Search Carousell for listings (best match)
  /carousell track <query>   — Add keyword to daily price alerts
  /carousell untrack <query> — Remove keyword from daily alerts
  /carousell list            — Show tracked keywords
  /carousell recent <query>  — Search by most recent (instead of best match)

Usage examples:
  /carousell fujifilm x-pro 2
  /carousell track fujifilm x-pro 2
  /carousell untrack fujifilm x-pro 2
  /carousell list
"""

import json
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.llm import ask_llm
from cupbots.helpers.logger import get_logger

log = get_logger("carousell")

PLUGIN_NAME = "carousell"
PLUGIN_DIR = Path(__file__).parent
TZ = ZoneInfo("Asia/Kuala_Lumpur")

# Region configs
REGIONS = {
    "my": {
        "domain": "https://www.carousell.com.my",
        "countryCode": "MY",
        "countryId": "1733045",
        "ccid": "6003",
        "label": "🇲🇾",
    },
    "sg": {
        "domain": "https://www.carousell.sg",
        "countryCode": "SG",
        "countryId": "1880251",
        "ccid": "5727",
        "label": "🇸🇬",
    },
}

# Sort modes
SORT_BEST_MATCH = "3"
SORT_RECENT = "1"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracked_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '_default',
            chat_id TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'telegram',
            keyword TEXT NOT NULL,
            regions TEXT NOT NULL DEFAULT '["my","sg"]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, chat_id, keyword)
        );
        CREATE TABLE IF NOT EXISTS seen_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '_default',
            keyword TEXT NOT NULL,
            listing_id TEXT NOT NULL,
            region TEXT NOT NULL,
            first_seen TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, keyword, listing_id, region)
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Cookie loading (Netscape format from browser extension)
# ---------------------------------------------------------------------------

_cookie_cache: dict[str, tuple[str, str, float]] = {}  # domain -> (cookie_str, csrf, timestamp)
_COOKIE_TTL = 3600  # refresh from file every hour


def _load_cookies(domain: str) -> tuple[str, str]:
    """Load cookies from Netscape cookie file for a domain.

    Returns (cookie_header_string, csrf_token).
    Looks for <domain>_cookies.txt in the plugin directory.
    """
    cached = _cookie_cache.get(domain)
    if cached:
        cookie_str, csrf, ts = cached
        if (datetime.now().timestamp() - ts) < _COOKIE_TTL:
            return cookie_str, csrf

    # Find cookie file: try domain-specific, then wildcard
    host = domain.replace("https://", "").replace("http://", "")
    cookie_file = PLUGIN_DIR / f"{host}_cookies.txt"
    if not cookie_file.exists():
        # Try any *_cookies.txt file
        files = list(PLUGIN_DIR.glob("*_cookies.txt"))
        cookie_file = files[0] if files else None

    if not cookie_file or not cookie_file.exists():
        log.warning("No cookie file found in %s", PLUGIN_DIR)
        return "", ""

    pairs = []
    csrf = ""
    for line in cookie_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5], parts[6]
        pairs.append(f"{name}={value}")
        if name == "_csrf":
            csrf = value

    cookie_str = "; ".join(pairs)
    _cookie_cache[domain] = (cookie_str, csrf, datetime.now().timestamp())
    log.info("Loaded %d cookies from %s (csrf=%s)", len(pairs), cookie_file.name, bool(csrf))
    return cookie_str, csrf


# ---------------------------------------------------------------------------
# Carousell API
# ---------------------------------------------------------------------------

async def _search(query: str, region: str = "my",
                  sort: str = SORT_BEST_MATCH, count: int = 20) -> list[dict]:
    """Search Carousell and return listing cards."""
    cfg = REGIONS[region]
    cookies, csrf = _load_cookies(cfg["domain"])
    if not cookies:
        log.warning("No cookies for %s — add a cookie file", region)
        return []

    payload = {
        "bestMatchEnabled": True,
        "canChangeKeyword": False,
        "ccid": cfg["ccid"],
        "count": count,
        "countryCode": cfg["countryCode"],
        "countryId": cfg["countryId"],
        "filters": [],
        "includeBpEducationBanner": False,
        "includeListingDescription": False,
        "includePopularLocations": False,
        "includeSuggestions": "false",
        "isCertifiedSpotlightEnabled": False,
        "locale": "en",
        "prefill": {"prefill_sort_by": sort},
        "query": query,
        "sortParam": {"fieldName": sort},
    }

    headers = {
        "Cookie": cookies,
        "csrf-token": csrf,
        "Content-Type": "application/json",
        "Referer": f"{cfg['domain']}/search/{query}",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{cfg['domain']}/ds/filter/cf/4.0/search/",
                json=payload,
                headers=headers,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("Search failed for %s/%s: %s", region, query, e)
        return []

    results = data.get("data", {}).get("results", [])
    listings = []
    for item in results:
        card = item.get("listingCard")
        if not card:
            continue
        listings.append({
            "id": card.get("id", ""),
            "title": card.get("title", ""),
            "price": card.get("price", ""),
            "seller": card.get("seller", {}).get("username", ""),
            "photo": (card.get("photoUrls") or [""])[0],
            "url": f"{cfg['domain']}/p/{card.get('id', '')}/",
            "region": region,
            "tags": [t.get("content", "") for t in card.get("tags", [])],
        })
    return listings


async def _search_both(query: str, sort: str = SORT_BEST_MATCH,
                       count: int = 10) -> list[dict]:
    """Search both MY and SG, return combined results."""
    import asyncio
    my_task = _search(query, "my", sort, count)
    sg_task = _search(query, "sg", sort, count)
    my_results, sg_results = await asyncio.gather(my_task, sg_task)
    return my_results + sg_results


# ---------------------------------------------------------------------------
# AI keyword expansion
# ---------------------------------------------------------------------------

async def _expand_keywords(query: str) -> list[str]:
    """Use LLM to generate 2 alternative search keywords."""
    prompt = (
        f"I'm searching for '{query}' on Carousell (a marketplace like eBay).\n"
        f"Generate exactly 2 alternative search keywords that might find more "
        f"or better results for the same item. Return ONLY a JSON array of strings.\n"
        f'Example: ["fuji xpro2", "fujifilm xpro 2"]\n'
        f"No explanation, just the JSON array."
    )
    try:
        resp = await ask_llm(prompt, model="claude-haiku-4-5-20251001",
                             max_tokens=100, json_mode=True)
        if isinstance(resp, list):
            return [str(k) for k in resp[:2]]
        if isinstance(resp, str):
            parsed = json.loads(resp)
            if isinstance(parsed, list):
                return [str(k) for k in parsed[:2]]
    except Exception as e:
        log.debug("Keyword expansion failed: %s", e)
    return []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_listing(item: dict, idx: int) -> str:
    """Format a single listing for text output."""
    flag = REGIONS.get(item["region"], {}).get("label", "")
    price = item["price"] or "N/A"
    tags = f" [{', '.join(item['tags'])}]" if item.get("tags") else ""
    return (
        f"{idx}. {flag} *{item['title']}*\n"
        f"   💰 {price}{tags}\n"
        f"   👤 {item['seller']} — {item['url']}"
    )


def _format_results(listings: list[dict], query: str, sort_label: str) -> str:
    """Format search results as a text message."""
    if not listings:
        return f"No results found for *{query}*."

    lines = [f"🔍 *Carousell: {query}* ({sort_label}, {len(listings)} results)\n"]
    for i, item in enumerate(listings, 1):
        lines.append(_format_listing(item, i))
    return "\n\n".join(lines)


async def _format_results_mdpubs(listings: list[dict], query: str,
                                  sort_label: str,
                                  company_id: str | None = None) -> str | None:
    """Publish results to mdpubs if available, return URL or None."""
    try:
        from cupbots.plugins.mdpubs_plugin import publish_or_fallback
    except ImportError:
        return None

    if not listings:
        return None

    md_lines = [f"# Carousell: {query}\n", f"*{sort_label} · {len(listings)} results*\n"]
    for item in listings:
        flag = REGIONS.get(item["region"], {}).get("label", "")
        tags = f" `{', '.join(item['tags'])}`" if item.get("tags") else ""
        photo_md = f"![{item['title']}]({item['photo']})\n" if item.get("photo") else ""
        md_lines.append(
            f"---\n\n"
            f"{photo_md}"
            f"### [{item['title']}]({item['url']})\n\n"
            f"{flag} **{item['price'] or 'N/A'}**{tags}\n\n"
            f"Seller: {item['seller']}\n"
        )
    content = "\n".join(md_lines)
    key = f"carousell-{re.sub(r'[^a-z0-9]+', '-', query.lower()).strip('-')}"
    url, _ = await publish_or_fallback(key, f"Carousell: {query}", content,
                                        company_id=company_id, tags=["carousell"])
    return url


# ---------------------------------------------------------------------------
# Daily alert logic
# ---------------------------------------------------------------------------

def _add_tracking(company_id: str, chat_id: str, platform: str, keyword: str):
    conn = _db()
    cid = company_id or "_default"
    conn.execute(
        "INSERT OR IGNORE INTO tracked_keywords (company_id, chat_id, platform, keyword) "
        "VALUES (?, ?, ?, ?)",
        (cid, chat_id, platform, keyword),
    )
    conn.commit()


def _remove_tracking(company_id: str, chat_id: str, keyword: str) -> bool:
    conn = _db()
    cid = company_id or "_default"
    cur = conn.execute(
        "DELETE FROM tracked_keywords WHERE company_id = ? AND chat_id = ? AND keyword = ?",
        (cid, chat_id, keyword),
    )
    conn.commit()
    return cur.rowcount > 0


def _list_tracked(company_id: str, chat_id: str) -> list[dict]:
    conn = _db()
    cid = company_id or "_default"
    rows = conn.execute(
        "SELECT * FROM tracked_keywords WHERE company_id = ? AND chat_id = ? ORDER BY keyword",
        (cid, chat_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _all_tracked() -> list[dict]:
    conn = _db()
    rows = conn.execute("SELECT * FROM tracked_keywords ORDER BY keyword").fetchall()
    return [dict(r) for r in rows]


def _mark_seen(company_id: str, keyword: str, listing_id: str, region: str):
    conn = _db()
    cid = company_id or "_default"
    conn.execute(
        "INSERT OR IGNORE INTO seen_listings (company_id, keyword, listing_id, region) "
        "VALUES (?, ?, ?, ?)",
        (cid, keyword, listing_id, region),
    )
    conn.commit()


def _is_seen(company_id: str, keyword: str, listing_id: str, region: str) -> bool:
    conn = _db()
    cid = company_id or "_default"
    row = conn.execute(
        "SELECT 1 FROM seen_listings WHERE company_id = ? AND keyword = ? "
        "AND listing_id = ? AND region = ?",
        (cid, keyword, listing_id, region),
    ).fetchone()
    return row is not None


async def _send_daily_alerts(bot=None):
    """Check all tracked keywords and send alerts for new listings."""
    tracked = _all_tracked()
    if not tracked:
        return

    # Group by keyword to avoid duplicate searches
    keyword_chats: dict[str, list[dict]] = {}
    for t in tracked:
        keyword_chats.setdefault(t["keyword"], []).append(t)

    for keyword, chats in keyword_chats.items():
        try:
            listings = await _search_both(keyword, sort=SORT_BEST_MATCH, count=15)
        except Exception as e:
            log.error("Daily search failed for '%s': %s", keyword, e)
            continue

        for chat_info in chats:
            cid = chat_info["company_id"]
            new_items = [
                l for l in listings
                if not _is_seen(cid, keyword, l["id"], l["region"])
            ]

            if not new_items:
                continue

            # Mark as seen
            for item in new_items:
                _mark_seen(cid, keyword, item["id"], item["region"])

            # Format and send
            text = _format_results(new_items, keyword, f"{len(new_items)} new")

            platform = chat_info["platform"]
            chat_id = chat_info["chat_id"]

            if bot and platform == "telegram":
                try:
                    await bot.send_message(
                        chat_id=int(chat_id), text=text, parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    log.error("Send alert failed tg/%s: %s", chat_id, e)
            elif platform == "whatsapp":
                try:
                    from cupbots.helpers.channel import WhatsAppReplyContext
                    reply = WhatsAppReplyContext(chat_id)
                    # Strip markdown for WhatsApp
                    plain = text.replace("*", "").replace("`", "")
                    await reply.reply_text(plain)
                except Exception as e:
                    log.error("Send alert failed wa/%s: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Core search command
# ---------------------------------------------------------------------------

async def _do_search(query: str, sort: str = SORT_BEST_MATCH,
                     count: int = 10, expand: bool = True,
                     company_id: str | None = None) -> tuple[str, str | None]:
    """Run search with optional keyword expansion.

    Returns (text_result, mdpubs_url_or_none).
    """
    sort_label = "best match" if sort == SORT_BEST_MATCH else "recent"

    # Expand keywords with AI
    all_keywords = [query]
    if expand:
        extra = await _expand_keywords(query)
        all_keywords.extend(extra)

    # Search all keywords, deduplicate by listing ID
    seen_ids = set()
    all_listings = []
    for kw in all_keywords:
        results = await _search_both(kw, sort=sort, count=count)
        for item in results:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_listings.append(item)

    # Sort by price (try to extract numeric)
    def price_key(item):
        p = re.sub(r"[^\d.]", "", item.get("price", "") or "")
        return float(p) if p else 999999
    all_listings.sort(key=price_key)

    # Limit display
    display = all_listings[:20]
    text = _format_results(display, query, sort_label)

    if len(all_keywords) > 1:
        text += f"\n\n_Also searched: {', '.join(all_keywords[1:])}_"

    # Try mdpubs
    mdpubs_url = await _format_results_mdpubs(
        display, query, sort_label, company_id=company_id
    )

    return text, mdpubs_url


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "carousell":
        return False

    args = msg.args
    if not args:
        await reply.reply_text(
            "Usage:\n"
            "/carousell <query> — Search listings\n"
            "/carousell track <query> — Daily alerts\n"
            "/carousell untrack <query> — Remove alerts\n"
            "/carousell list — Show tracked keywords\n"
            "/carousell recent <query> — Search by recency"
        )
        return True

    sub = args[0].lower()

    # /carousell list
    if sub == "list":
        tracked = _list_tracked(msg.company_id, str(msg.chat_id))
        if not tracked:
            await reply.reply_text("No tracked keywords. Use /carousell track <query> to add one.")
            return True
        lines = ["Tracked keywords:\n"]
        for t in tracked:
            lines.append(f"• {t['keyword']}")
        await reply.reply_text("\n".join(lines))
        return True

    # /carousell track <query>
    if sub == "track" and len(args) > 1:
        keyword = " ".join(args[1:])
        _add_tracking(msg.company_id, str(msg.chat_id), msg.platform, keyword)
        await reply.reply_text(f"Tracking *{keyword}* — you'll get daily alerts for new listings.")
        return True

    # /carousell untrack <query>
    if sub == "untrack" and len(args) > 1:
        keyword = " ".join(args[1:])
        if _remove_tracking(msg.company_id, str(msg.chat_id), keyword):
            await reply.reply_text(f"Stopped tracking *{keyword}*.")
        else:
            await reply.reply_text(f"Not tracking *{keyword}*.")
        return True

    # /carousell recent <query>
    if sub == "recent" and len(args) > 1:
        query = " ".join(args[1:])
        await reply.send_typing()
        text, url = await _do_search(query, sort=SORT_RECENT, company_id=msg.company_id)
        if url:
            await reply.reply_text(f"🔍 Carousell results for *{query}*: {url}")
        else:
            await reply.reply_text(text)
        return True

    # /carousell <query> — default best match search
    query = " ".join(args)
    await reply.send_typing()
    text, url = await _do_search(query, sort=SORT_BEST_MATCH, company_id=msg.company_id)
    if url:
        await reply.reply_text(f"🔍 Carousell results for *{query}*: {url}")
    else:
        await reply.reply_text(text)
    return True


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_carousell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "`/carousell <query>` — Search listings\n"
            "`/carousell track <query>` — Daily alerts\n"
            "`/carousell untrack <query>` — Remove alerts\n"
            "`/carousell list` — Show tracked keywords\n"
            "`/carousell recent <query>` — Search by recency",
            parse_mode="Markdown",
        )
        return

    sub = args[0].lower()

    if sub == "list":
        chat_id = str(update.effective_chat.id)
        tracked = _list_tracked("_default", chat_id)
        if not tracked:
            await update.message.reply_text("No tracked keywords.")
            return
        lines = ["Tracked keywords:\n"]
        for t in tracked:
            lines.append(f"• {t['keyword']}")
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "track" and len(args) > 1:
        keyword = " ".join(args[1:])
        chat_id = str(update.effective_chat.id)
        _add_tracking("_default", chat_id, "telegram", keyword)
        await update.message.reply_text(
            f"Tracking *{keyword}* — daily alerts enabled.",
            parse_mode="Markdown",
        )
        return

    if sub == "untrack" and len(args) > 1:
        keyword = " ".join(args[1:])
        chat_id = str(update.effective_chat.id)
        if _remove_tracking("_default", chat_id, keyword):
            await update.message.reply_text(f"Stopped tracking *{keyword}*.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"Not tracking *{keyword}*.", parse_mode="Markdown")
        return

    sort = SORT_BEST_MATCH
    if sub == "recent" and len(args) > 1:
        sort = SORT_RECENT
        args = args[1:]

    query = " ".join(args)
    await update.effective_chat.send_action("typing")
    text, url = await _do_search(query, sort=sort)
    if url:
        await update.message.reply_text(
            f"🔍 Carousell results for *{query}*: {url}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(text, parse_mode="Markdown",
                                         disable_web_page_preview=True)


async def _daily_alert_job(context: ContextTypes.DEFAULT_TYPE):
    """Telegram job_queue callback for daily alerts."""
    await _send_daily_alerts(bot=context.bot)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(app: Application):
    app.add_handler(CommandHandler("carousell", cmd_carousell))
    app.job_queue.run_daily(
        _daily_alert_job,
        time=time(9, 0, tzinfo=TZ),
        name="carousell_daily_alert",
    )
