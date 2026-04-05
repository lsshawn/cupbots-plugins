"""
Notion — Read, update, and sync Notion pages and databases.

Commands:
  /notion                            — Show linked sources and status
  /notion link <name> <notion-url>   — Link a Notion page or database
  /notion unlink <name>              — Remove a linked source
  /notion sources                    — List all linked sources
  /notion read <name> [query]        — Read/search a source
  /notion add <name> <title> [-- k=v, ...] — Add entry to a database
  /notion update <name> <title> -- k=v, ... — Update an entry
  /notion sync <name>                — Pull changes, LLM-summarize, post

Setup:
  1. Create integration at notion.so/my-integrations
  2. /plugin config notion NOTION_API_KEY <your-token>
  3. Share your Notion page/database with the integration
  4. /notion link clients https://notion.so/xxx/your-database-id

Examples:
  /notion link meeting-notes https://notion.so/Meeting-Notes-abc123
  /notion read clients Acme Corp
  /notion add clients "Acme Corp" -- status=Active, contact=John
  /notion sync meeting-notes
  /schedule add "weekly fri 17:00" /notion sync weekly-huddle
"""

import json
import re
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import ask_llm

log = get_logger("notion")
PLUGIN_NAME = "notion"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notion_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            notion_id TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (source_type IN ('database', 'page')),
            chat_id TEXT NOT NULL DEFAULT '',
            last_synced TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_sources_company ON notion_sources(company_id);

        CREATE TABLE IF NOT EXISTS notion_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL,
            notion_object_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content_json TEXT NOT NULL DEFAULT '',
            last_edited TEXT NOT NULL DEFAULT '',
            cached_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, source_name, notion_object_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cache_source ON notion_cache(company_id, source_name);
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Notion client
# ---------------------------------------------------------------------------

def _get_client():
    """Return an AsyncClient or None if not configured."""
    from notion_client import AsyncClient
    token = resolve_plugin_setting(PLUGIN_NAME, "NOTION_API_KEY")
    if not token:
        return None
    return AsyncClient(auth=token)


def _ensure_configured() -> str | None:
    """Return setup instructions if not configured, None if OK."""
    token = resolve_plugin_setting(PLUGIN_NAME, "NOTION_API_KEY")
    if not token:
        return (
            "Notion not configured yet.\n\n"
            "Setup:\n"
            "1. Create an integration at notion.so/my-integrations\n"
            "2. Run: /plugin config notion NOTION_API_KEY <your-token>\n"
            "3. Share your Notion page/database with the integration\n"
            "4. Run: /notion link <name> <notion-url>"
        )
    return None


# ---------------------------------------------------------------------------
# URL parsing & type detection
# ---------------------------------------------------------------------------

def _parse_notion_url(url_or_id: str) -> str | None:
    """Extract UUID from a Notion URL or raw ID. Returns dashed UUID or None."""
    # Strip query params and fragments
    clean = url_or_id.split("?")[0].split("#")[0]
    # Find 32 hex chars (optionally with dashes)
    clean_nodash = clean.replace("-", "")
    m = re.search(r"([a-f0-9]{32})", clean_nodash)
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


async def _detect_source_type(client, notion_id: str) -> tuple[str | None, str]:
    """Detect if notion_id is a database or page. Returns (type, title)."""
    from notion_client import APIResponseError
    try:
        db = await client.databases.retrieve(database_id=notion_id)
        title = _extract_db_title(db)
        return "database", title
    except APIResponseError:
        pass
    try:
        page = await client.pages.retrieve(page_id=notion_id)
        title = _extract_title(page.get("properties", {}))
        return "page", title
    except APIResponseError:
        pass
    return None, ""


# ---------------------------------------------------------------------------
# Property helpers
# ---------------------------------------------------------------------------

def _extract_db_title(db: dict) -> str:
    """Extract title from a Notion database object."""
    title_parts = db.get("title", [])
    return "".join(t.get("plain_text", "") for t in title_parts) or "(untitled)"


def _extract_title(properties: dict) -> str:
    """Extract the title property from a Notion page's properties."""
    for prop in properties.values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return "(untitled)"


def _flatten_property(prop: dict) -> str:
    """Flatten a single Notion property value to a display string."""
    ptype = prop.get("type", "")
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "number":
        val = prop.get("number")
        return str(val) if val is not None else ""
    if ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    if ptype == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select", []))
    if ptype == "date":
        d = prop.get("date")
        if not d:
            return ""
        start = d.get("start", "")
        end = d.get("end")
        return f"{start} → {end}" if end else start
    if ptype == "checkbox":
        return "Yes" if prop.get("checkbox") else "No"
    if ptype == "url":
        return prop.get("url") or ""
    if ptype == "email":
        return prop.get("email") or ""
    if ptype == "phone_number":
        return prop.get("phone_number") or ""
    if ptype == "status":
        s = prop.get("status")
        return s.get("name", "") if s else ""
    if ptype == "people":
        return ", ".join(p.get("name", "") for p in prop.get("people", []))
    if ptype in ("created_time", "last_edited_time"):
        return prop.get(ptype, "")
    return ""


def _flatten_properties(properties: dict) -> dict[str, str]:
    """Flatten all Notion properties to {name: display_value}."""
    result = {}
    for name, prop in properties.items():
        val = _flatten_property(prop)
        if val:
            result[name] = val
    return result


def _format_row(properties: dict) -> str:
    """Format a database row's properties as a readable line."""
    flat = _flatten_properties(properties)
    title = ""
    fields = []
    for name, val in flat.items():
        # Find the title property
        prop = properties.get(name, {})
        if prop.get("type") == "title":
            title = val
        else:
            fields.append(f"{name}: {val}")
    field_str = " | ".join(fields)
    return f"*{title}*  {field_str}" if title else field_str


# ---------------------------------------------------------------------------
# Block content helpers (for pages)
# ---------------------------------------------------------------------------

def _blocks_to_text(blocks: list[dict]) -> str:
    """Convert Notion blocks to readable plain text (top-level only)."""
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})

        if btype in ("paragraph", "quote", "callout"):
            text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
            if text:
                lines.append(text)
        elif btype.startswith("heading_"):
            text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
            level = btype[-1]
            prefix = "#" * int(level)
            lines.append(f"{prefix} {text}")
        elif btype == "bulleted_list_item":
            text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
            lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
            lines.append(f"1. {text}")
        elif btype == "to_do":
            text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
            checked = data.get("checked", False)
            lines.append(f"[{'x' if checked else ' '}] {text}")
        elif btype == "code":
            text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
            lang = data.get("language", "")
            lines.append(f"```{lang}\n{text}\n```")
        elif btype == "divider":
            lines.append("---")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source CRUD
# ---------------------------------------------------------------------------

def _get_source(name: str, company_id: str) -> dict | None:
    """Look up a linked source by name."""
    row = _db().execute(
        "SELECT * FROM notion_sources WHERE company_id = ? AND name = ?",
        (company_id, name),
    ).fetchone()
    return dict(row) if row else None


def _list_sources(company_id: str) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM notion_sources WHERE company_id = ? ORDER BY name",
        (company_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _save_source(company_id: str, name: str, notion_id: str,
                 source_type: str, chat_id: str):
    _db().execute(
        """INSERT INTO notion_sources (company_id, name, notion_id, source_type, chat_id)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(company_id, name) DO UPDATE SET
             notion_id = excluded.notion_id,
             source_type = excluded.source_type,
             chat_id = excluded.chat_id""",
        (company_id, name, notion_id, source_type, chat_id),
    )
    _db().commit()


def _delete_source(name: str, company_id: str):
    _db().execute(
        "DELETE FROM notion_sources WHERE company_id = ? AND name = ?",
        (company_id, name),
    )
    _db().execute(
        "DELETE FROM notion_cache WHERE company_id = ? AND source_name = ?",
        (company_id, name),
    )
    _db().commit()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _get_cached(company_id: str, source_name: str) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM notion_cache WHERE company_id = ? AND source_name = ? ORDER BY title",
        (company_id, source_name),
    ).fetchall()
    return [dict(r) for r in rows]


def _update_cache(company_id: str, source_name: str, items: list[dict]):
    """Replace cache for a source with fresh items.
    Each item: {notion_object_id, title, content_json, last_edited}"""
    db = _db()
    db.execute(
        "DELETE FROM notion_cache WHERE company_id = ? AND source_name = ?",
        (company_id, source_name),
    )
    for item in items:
        db.execute(
            """INSERT INTO notion_cache
               (company_id, source_name, notion_object_id, title, content_json, last_edited, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (company_id, source_name, item["notion_object_id"],
             item["title"], item["content_json"], item["last_edited"]),
        )
    db.execute(
        "UPDATE notion_sources SET last_synced = datetime('now') WHERE company_id = ? AND name = ?",
        (company_id, source_name),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Read / query
# ---------------------------------------------------------------------------

async def _read_database(client, source: dict, query: str | None,
                         company_id: str) -> str:
    """Query a Notion database and return formatted results."""
    notion_id = source["notion_id"]

    kwargs = {"database_id": notion_id, "page_size": 10}
    if query:
        # Build a title-contains filter
        # First, get the database schema to find the title property name
        db_info = await client.databases.retrieve(database_id=notion_id)
        title_prop = None
        for pname, prop in db_info.get("properties", {}).items():
            if prop.get("type") == "title":
                title_prop = pname
                break
        if title_prop:
            kwargs["filter"] = {
                "property": title_prop,
                "title": {"contains": query},
            }

    result = await client.databases.query(**kwargs)
    pages = result.get("results", [])

    if not pages:
        return f"No results in *{source['name']}*" + (f" for \"{query}\"" if query else "") + "."

    lines = [f"*{source['name']}* ({len(pages)} result{'s' if len(pages) != 1 else ''}):\n"]
    for page in pages:
        lines.append(f"  {_format_row(page.get('properties', {}))}")

    return "\n".join(lines)


async def _read_page(client, source: dict) -> str:
    """Read a Notion page's content as text."""
    blocks_resp = await client.blocks.children.list(block_id=source["notion_id"])
    blocks = blocks_resp.get("results", [])

    if not blocks:
        return f"*{source['name']}* — (empty page)"

    text = _blocks_to_text(blocks)
    return f"*{source['name']}*\n\n{text}"


# ---------------------------------------------------------------------------
# Add / update
# ---------------------------------------------------------------------------

def _parse_key_values(text: str) -> dict[str, str]:
    """Parse 'key=value, key2=value2' into a dict."""
    result = {}
    for pair in text.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        result[key.strip()] = val.strip()
    return result


def _build_property_value(schema: dict, key: str, value: str) -> dict | None:
    """Convert a key=value pair into a Notion property payload using the DB schema."""
    prop_schema = schema.get(key)
    if not prop_schema:
        return None
    ptype = prop_schema.get("type", "")

    if ptype == "title":
        return {"title": [{"text": {"content": value}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"text": {"content": value}}]}
    if ptype == "number":
        try:
            return {"number": float(value)}
        except ValueError:
            return None
    if ptype == "select":
        return {"select": {"name": value}}
    if ptype == "multi_select":
        names = [n.strip() for n in value.split(",")]
        return {"multi_select": [{"name": n} for n in names]}
    if ptype == "date":
        return {"date": {"start": value}}
    if ptype == "checkbox":
        return {"checkbox": value.lower() in ("true", "yes", "1")}
    if ptype == "url":
        return {"url": value}
    if ptype == "email":
        return {"email": value}
    if ptype == "phone_number":
        return {"phone_number": value}
    if ptype == "status":
        return {"status": {"name": value}}
    return None


async def _add_to_database(client, source: dict, title: str,
                           extra_fields: dict[str, str]) -> str:
    """Add a new entry to a Notion database."""
    notion_id = source["notion_id"]
    db_info = await client.databases.retrieve(database_id=notion_id)
    schema = db_info.get("properties", {})

    # Find the title property
    title_prop = None
    for pname, prop in schema.items():
        if prop.get("type") == "title":
            title_prop = pname
            break
    if not title_prop:
        return "Could not find title property in database."

    properties = {
        title_prop: {"title": [{"text": {"content": title}}]},
    }

    # Add extra fields
    unknown = []
    for key, val in extra_fields.items():
        pval = _build_property_value(schema, key, val)
        if pval:
            properties[key] = pval
        else:
            unknown.append(key)

    await client.pages.create(
        parent={"database_id": notion_id},
        properties=properties,
    )

    msg = f"Added *{title}* to *{source['name']}*"
    if unknown:
        msg += f"\n(Unknown fields skipped: {', '.join(unknown)})"
    return msg


async def _update_in_database(client, source: dict, title: str,
                              fields: dict[str, str]) -> str:
    """Update a database entry matching the title."""
    notion_id = source["notion_id"]
    db_info = await client.databases.retrieve(database_id=notion_id)
    schema = db_info.get("properties", {})

    # Find the title property
    title_prop = None
    for pname, prop in schema.items():
        if prop.get("type") == "title":
            title_prop = pname
            break
    if not title_prop:
        return "Could not find title property in database."

    # Search for the entry
    result = await client.databases.query(
        database_id=notion_id,
        filter={"property": title_prop, "title": {"equals": title}},
    )
    pages = result.get("results", [])
    if not pages:
        return f"No entry found with title \"{title}\" in *{source['name']}*."

    page_id = pages[0]["id"]

    # Build update properties
    properties = {}
    unknown = []
    for key, val in fields.items():
        pval = _build_property_value(schema, key, val)
        if pval:
            properties[key] = pval
        else:
            unknown.append(key)

    if not properties:
        return "No valid fields to update." + (f" Unknown: {', '.join(unknown)}" if unknown else "")

    await client.pages.update(page_id=page_id, properties=properties)

    msg = f"Updated *{title}* in *{source['name']}*"
    if unknown:
        msg += f"\n(Unknown fields skipped: {', '.join(unknown)})"
    return msg


# ---------------------------------------------------------------------------
# Sync + summarize
# ---------------------------------------------------------------------------

async def _sync_source(client, source: dict, company_id: str) -> str:
    """Pull changes from Notion, diff with cache, LLM-summarize."""
    source_name = source["name"]

    if source["source_type"] == "database":
        return await _sync_database(client, source, company_id)
    else:
        return await _sync_page(client, source, company_id)


async def _sync_database(client, source: dict, company_id: str) -> str:
    """Sync a Notion database: diff entries, summarize changes."""
    notion_id = source["notion_id"]
    source_name = source["name"]

    # Fetch current state (up to 100 most recently edited)
    result = await client.databases.query(
        database_id=notion_id,
        sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
        page_size=100,
    )
    current_pages = result.get("results", [])

    # Build current snapshot
    current_items = []
    current_by_id = {}
    for page in current_pages:
        obj_id = page["id"]
        props = page.get("properties", {})
        title = _extract_title(props)
        flat = _flatten_properties(props)
        last_edited = page.get("last_edited_time", "")
        item = {
            "notion_object_id": obj_id,
            "title": title,
            "content_json": json.dumps(flat),
            "last_edited": last_edited,
        }
        current_items.append(item)
        current_by_id[obj_id] = {"title": title, "props": flat, "last_edited": last_edited}

    # Load cached state
    cached = _get_cached(company_id, source_name)
    cached_by_id = {}
    for c in cached:
        cached_by_id[c["notion_object_id"]] = {
            "title": c["title"],
            "props": json.loads(c["content_json"]) if c["content_json"] else {},
            "last_edited": c["last_edited"],
        }

    # Diff
    new_entries = []
    changed_entries = []
    for obj_id, cur in current_by_id.items():
        prev = cached_by_id.get(obj_id)
        if not prev:
            new_entries.append(cur)
        elif cur["last_edited"] != prev["last_edited"]:
            changed_entries.append({"before": prev, "after": cur})

    removed = [v for k, v in cached_by_id.items() if k not in current_by_id]

    if not new_entries and not changed_entries and not removed:
        # Update cache even if no changes (refreshes last_synced)
        _update_cache(company_id, source_name, current_items)
        return f"No changes in *{source_name}* since last sync."

    # Build diff text for LLM
    diff_parts = []
    if new_entries:
        diff_parts.append(f"New entries ({len(new_entries)}):")
        for e in new_entries:
            props_str = ", ".join(f"{k}: {v}" for k, v in e["props"].items())
            diff_parts.append(f"  + {e['title']} ({props_str})")
    if changed_entries:
        diff_parts.append(f"\nChanged entries ({len(changed_entries)}):")
        for c in changed_entries:
            diff_parts.append(f"  ~ {c['after']['title']}")
            for k, v in c["after"]["props"].items():
                old_v = c["before"]["props"].get(k, "")
                if v != old_v:
                    diff_parts.append(f"    {k}: {old_v} → {v}")
    if removed:
        diff_parts.append(f"\nRemoved entries ({len(removed)}):")
        for e in removed:
            diff_parts.append(f"  - {e['title']}")

    diff_text = "\n".join(diff_parts)

    summary = await ask_llm(
        f"Summarize these changes to the '{source_name}' database:\n\n{diff_text}",
        system=(
            "You are a concise business assistant. Summarize database changes "
            "as a short bullet-point digest for a WhatsApp group. "
            "Be specific about what changed. Keep it under 500 characters."
        ),
    )

    # Update cache
    _update_cache(company_id, source_name, current_items)

    last_synced = source.get("last_synced", "")
    header = f"*{source_name} — sync*"
    if last_synced:
        header += f" (since {last_synced[:16]})"
    return f"{header}\n\n{summary}"


async def _sync_page(client, source: dict, company_id: str) -> str:
    """Sync a Notion page: diff content, summarize changes."""
    source_name = source["name"]

    blocks_resp = await client.blocks.children.list(block_id=source["notion_id"])
    blocks = blocks_resp.get("results", [])
    current_text = _blocks_to_text(blocks)

    # Load cached content
    cached = _get_cached(company_id, source_name)
    cached_text = ""
    if cached:
        cached_text = cached[0].get("content_json", "")

    if current_text == cached_text:
        # Refresh cache timestamp
        _update_cache(company_id, source_name, [{
            "notion_object_id": source["notion_id"],
            "title": source_name,
            "content_json": current_text,
            "last_edited": datetime.now().isoformat(),
        }])
        return f"No changes in *{source_name}* since last sync."

    # Summarize with LLM
    if cached_text:
        prompt = (
            f"The page '{source_name}' was updated.\n\n"
            f"Previous content:\n{cached_text[:2000]}\n\n"
            f"Current content:\n{current_text[:2000]}\n\n"
            "Summarize what changed."
        )
    else:
        prompt = (
            f"Here is the current content of the page '{source_name}':\n\n"
            f"{current_text[:3000]}\n\n"
            "Provide a brief summary."
        )

    summary = await ask_llm(
        prompt,
        system=(
            "You are a concise business assistant. Summarize page changes "
            "as a short bullet-point digest for a WhatsApp group. "
            "Keep it under 500 characters."
        ),
    )

    # Update cache
    _update_cache(company_id, source_name, [{
        "notion_object_id": source["notion_id"],
        "title": source_name,
        "content_json": current_text,
        "last_edited": datetime.now().isoformat(),
    }])

    return f"*{source_name} — sync*\n\n{summary}"


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

def _split_title_and_fields(args: list[str]) -> tuple[str, dict[str, str]]:
    """Split args into title and key=value fields separated by '--'.
    Example: ["Acme Corp", "--", "status=Active,", "contact=John"]
    Returns: ("Acme Corp", {"status": "Active", "contact": "John"})
    """
    text = " ".join(args)
    if " -- " in text:
        title_part, fields_part = text.split(" -- ", 1)
        return title_part.strip().strip('"').strip("'"), _parse_key_values(fields_part)
    return text.strip().strip('"').strip("'"), {}


async def handle_command(msg, reply) -> bool:
    """Cross-platform command handler."""
    if msg.command != "notion":
        return False

    args = msg.args or []
    company_id = msg.company_id or ""

    # --help
    if args and args[0] == "--help":
        await reply.reply_text(__doc__.strip())
        return True

    # No subcommand — show status
    if not args:
        err = _ensure_configured()
        if err:
            await reply.reply_text(err)
            return True
        sources = _list_sources(company_id)
        if not sources:
            await reply.reply_text(
                "No Notion sources linked yet.\n\n"
                "Get started: /notion link <name> <notion-url>\n"
                "Example: /notion link clients https://notion.so/My-DB-abc123"
            )
        else:
            lines = [f"*Notion* — {len(sources)} source{'s' if len(sources) != 1 else ''} linked\n"]
            for s in sources:
                synced = f"synced {s['last_synced'][:16]}" if s["last_synced"] else "never synced"
                lines.append(f"  *{s['name']}* ({s['source_type']}) — {synced}")
            await reply.reply_text("\n".join(lines))
        return True

    sub = args[0].lower()

    # /notion link <name> <url>
    if sub == "link":
        if len(args) < 3:
            await reply.reply_text("Usage: /notion link <name> <notion-url>")
            return True
        err = _ensure_configured()
        if err:
            await reply.reply_text(err)
            return True

        name = args[1].lower()
        url = args[2]
        notion_id = _parse_notion_url(url)
        if not notion_id:
            await reply.reply_text("Could not extract a Notion ID from that URL. Paste the full Notion URL.")
            return True

        await reply.send_typing()
        client = _get_client()
        source_type, title = await _detect_source_type(client, notion_id)
        if not source_type:
            await reply.reply_text(
                "Could not access that Notion resource.\n"
                "Make sure you shared the page/database with your integration."
            )
            return True

        _save_source(company_id, name, notion_id, source_type, msg.chat_id)
        await reply.reply_text(f"Linked *{name}* — {source_type}: {title}")
        return True

    # /notion unlink <name>
    if sub == "unlink":
        if len(args) < 2:
            await reply.reply_text("Usage: /notion unlink <name>")
            return True
        name = args[1].lower()
        source = _get_source(name, company_id)
        if not source:
            await reply.reply_text(f"Source \"{name}\" not found. Run /notion sources.")
            return True
        _delete_source(name, company_id)
        await reply.reply_text(f"Unlinked *{name}*.")
        return True

    # /notion sources
    if sub == "sources":
        sources = _list_sources(company_id)
        if not sources:
            await reply.reply_text("No sources linked. Run /notion link <name> <url>.")
            return True
        lines = []
        for s in sources:
            synced = f"synced {s['last_synced'][:16]}" if s["last_synced"] else "never synced"
            lines.append(f"*{s['name']}* ({s['source_type']}) — {synced}")
        await reply.reply_text("\n".join(lines))
        return True

    # /notion read <name> [query]
    if sub == "read":
        if len(args) < 2:
            await reply.reply_text("Usage: /notion read <name> [query]")
            return True
        err = _ensure_configured()
        if err:
            await reply.reply_text(err)
            return True

        name = args[1].lower()
        query = " ".join(args[2:]) if len(args) > 2 else None
        source = _get_source(name, company_id)
        if not source:
            await reply.reply_text(f"Source \"{name}\" not found. Run /notion sources.")
            return True

        await reply.send_typing()
        client = _get_client()
        try:
            if source["source_type"] == "database":
                result = await _read_database(client, source, query, company_id)
            else:
                result = await _read_page(client, source)
            await reply.reply_text(result)
        except Exception as e:
            log.error("Notion read error: %s", e)
            await reply.reply_error(f"Failed to read from Notion: {e}")
        return True

    # /notion add <name> <title> [-- key=val, ...]
    if sub == "add":
        if len(args) < 3:
            await reply.reply_text("Usage: /notion add <name> <title> [-- key=val, ...]")
            return True
        err = _ensure_configured()
        if err:
            await reply.reply_text(err)
            return True

        name = args[1].lower()
        source = _get_source(name, company_id)
        if not source:
            await reply.reply_text(f"Source \"{name}\" not found. Run /notion sources.")
            return True
        if source["source_type"] != "database":
            await reply.reply_text("Can only add entries to databases, not pages.")
            return True

        title, fields = _split_title_and_fields(args[2:])
        if not title:
            await reply.reply_text("Usage: /notion add <name> <title> [-- key=val, ...]")
            return True

        await reply.send_typing()
        client = _get_client()
        try:
            result = await _add_to_database(client, source, title, fields)
            await reply.reply_text(result)
        except Exception as e:
            log.error("Notion add error: %s", e)
            await reply.reply_error(f"Failed to add to Notion: {e}")
        return True

    # /notion update <name> <title> -- key=val, ...
    if sub == "update":
        if len(args) < 3:
            await reply.reply_text("Usage: /notion update <name> <title> -- key=val, ...")
            return True
        err = _ensure_configured()
        if err:
            await reply.reply_text(err)
            return True

        name = args[1].lower()
        source = _get_source(name, company_id)
        if not source:
            await reply.reply_text(f"Source \"{name}\" not found. Run /notion sources.")
            return True
        if source["source_type"] != "database":
            await reply.reply_text("Can only update entries in databases, not pages.")
            return True

        title, fields = _split_title_and_fields(args[2:])
        if not title or not fields:
            await reply.reply_text("Usage: /notion update <name> <title> -- key=val, ...")
            return True

        await reply.send_typing()
        client = _get_client()
        try:
            result = await _update_in_database(client, source, title, fields)
            await reply.reply_text(result)
        except Exception as e:
            log.error("Notion update error: %s", e)
            await reply.reply_error(f"Failed to update Notion: {e}")
        return True

    # /notion sync <name>
    if sub == "sync":
        if len(args) < 2:
            await reply.reply_text("Usage: /notion sync <name>")
            return True
        err = _ensure_configured()
        if err:
            await reply.reply_text(err)
            return True

        name = args[1].lower()
        source = _get_source(name, company_id)
        if not source:
            await reply.reply_text(f"Source \"{name}\" not found. Run /notion sources.")
            return True

        await reply.send_typing()
        client = _get_client()
        try:
            result = await _sync_source(client, source, company_id)
            await reply.reply_text(result)
        except Exception as e:
            log.error("Notion sync error: %s", e)
            await reply.reply_error(f"Sync failed: {e}")
        return True

    await reply.reply_text(f"Unknown subcommand: {sub}. Try /notion --help")
    return True


# ---------------------------------------------------------------------------
# Telegram-specific handler
# ---------------------------------------------------------------------------

async def cmd_notion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    from cupbots.helpers.channel import IncomingMessage, TelegramReplyContext
    tg_reply = TelegramReplyContext(update)
    tg_msg = IncomingMessage(
        platform="telegram",
        chat_id=str(update.effective_chat.id),
        sender_id=str(update.effective_user.id),
        sender_name=update.effective_user.first_name or "",
        text=update.message.text or "",
        command="notion",
        args=context.args or [],
    )
    await handle_command(tg_msg, tg_reply)


def register(app: Application):
    app.add_handler(CommandHandler("notion", cmd_notion))
