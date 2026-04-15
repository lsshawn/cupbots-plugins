"""
Wiki — AI-maintained second brain. Knowledge synthesis, personal CRM, notes,
ideas, bookmarks, FAQ, and WhatsApp chat digest.

Core:
  /wiki                      Show help
  /wiki workspaces           List configured workspaces with stats
  /wiki ingest               Scan all sources, ingest new/changed files
  /wiki search <query>       Semantic search across entities
  /wiki entities             List all entities with types
  /wiki show <entity>        Display entity markdown
  /wiki query <question>     Ask a question, get a synthesized answer
  /wiki lint                 Health check (orphans, stale, broken links)
  /wiki log [--limit N]      Show recent actions
  /wiki sync                 Re-sync workspaces from config.yaml

WhatsApp digest:
  /wiki digest               Run digest now (all chats, last 7 days)
  /wiki digest <name>        Run digest for one contact only
  /wiki digest on|off        Enable/disable weekly auto-digest
  /wiki digest status        Show last/next digest run

CRM (also available via /crm):
  /crm                       Overdue contacts
  /crm whois <name>          Look up a person entity
  /crm remember <name> -- <note>  Add a fact
  /crm touched <name> [note] Log interaction
  /crm birthday <name> <date> Set birthday
  /crm list                  List all people

Notes (also via /note):
  /note <title> [-- body]    Create a note (raw, no LLM)
  /note list [#tag]          List notes
  /idea <description>        Capture an idea
  /ideas                     List ideas
  /note save <url> [#tag]    Bookmark a link
  /bookmarks                 List bookmarks

FAQ (also via /faq):
  /faq <query>               Search FAQ
  /faq add <Q> | <A>         Add Q&A pair
  /faq remove <id>           Remove entry
  /faq list                  List entries
  /faq auto on|off           Toggle auto-answer

All wiki commands accept --workspace <name> to target a specific workspace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from cupbots.config import get_config, get_data_dir
from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.events import subscribe
from cupbots.helpers.jobs import register_handler, enqueue, get_pending_jobs, cancel_job
from cupbots.helpers.llm import ask_llm
from cupbots.paths import register_path

log = logging.getLogger("wiki")

PLUGIN_NAME = "wiki"
SUPPORTED_EXTENSIONS = {".md", ".txt", ".csv", ".eml", ".pdf"}
DEFAULT_EMBEDDING_DIM = 768  # Gemini text-embedding-004 dimension


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            sources TEXT NOT NULL DEFAULT '[]',
            schema_path TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(company_id, name)
        );

        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            workspace_id INTEGER NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'person',
            name TEXT NOT NULL,
            slug TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '[]',
            last_updated TEXT NOT NULL DEFAULT (datetime('now')),
            source_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(workspace_id, slug)
        );
        CREATE INDEX IF NOT EXISTS idx_entities_company
            ON entities (company_id);
        CREATE INDEX IF NOT EXISTS idx_entities_name
            ON entities (name);

        CREATE TABLE IF NOT EXISTS ingestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            workspace_id INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            source_hash TEXT NOT NULL DEFAULT '',
            entity_ids TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(workspace_id, source_file, source_hash)
        );

        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            workspace_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            entity_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)

    # FAQ tables (absorbed from faq plugin)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS faq_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            views INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS faq_config (
            chat_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            auto_answer INTEGER NOT NULL DEFAULT 0
        );
    """)

    # Try to create sqlite-vec virtual table
    _init_vec_table(conn)


def _init_vec_table(conn: sqlite3.Connection):
    """Initialize sqlite-vec virtual table. Gracefully skip if not available."""
    global _vec_available
    try:
        import sqlite_vec  # noqa: F401
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS entity_embeddings
            USING vec0(entity_id INTEGER PRIMARY KEY, embedding float[{DEFAULT_EMBEDDING_DIM}])
        """)
        conn.commit()
        _vec_available = True
        log.info("sqlite-vec initialized (dim=%d)", DEFAULT_EMBEDDING_DIM)
    except Exception as e:
        _vec_available = False
        log.warning("sqlite-vec not available, falling back to keyword search: %s", e)


_vec_available = False


def _db() -> sqlite3.Connection:
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Wiki output directory
# ---------------------------------------------------------------------------

def _wiki_dir(company_id: str) -> Path:
    """Central wiki output: data/wiki/<company_id>/"""
    d = get_data_dir() / "wiki" / (company_id or "default")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _entities_dir(company_id: str) -> Path:
    d = _wiki_dir(company_id) / "entities"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------

def _load_workspaces_from_config() -> dict[str, dict]:
    """Read wiki.workspaces from config.yaml."""
    cfg = get_config()
    return cfg.get("wiki", {}).get("workspaces", {})


def _sync_workspaces(company_id: str) -> list[dict]:
    """Sync config.yaml workspaces into DB. Returns list of workspace dicts."""
    conn = _db()
    ws_config = _load_workspaces_from_config()
    results = []

    for name, ws_cfg in ws_config.items():
        sources = ws_cfg.get("sources", [])
        schema_path = ws_cfg.get("schema", "")

        # Register source paths with the allowlist
        for i, src in enumerate(sources):
            register_path(f"wiki_{name}_src_{i}", src)

        # Register wiki output path
        wiki_out = str(_wiki_dir(company_id))
        register_path(f"wiki_{company_id}_out", wiki_out)

        try:
            conn.execute("""
                INSERT INTO workspaces (company_id, name, sources, schema_path, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(company_id, name)
                DO UPDATE SET sources=excluded.sources,
                              schema_path=excluded.schema_path,
                              updated_at=datetime('now')
            """, (company_id, name, json.dumps(sources), schema_path))
            conn.commit()
        except Exception as e:
            log.error("Failed to sync workspace %s: %s", name, e)

        row = conn.execute(
            "SELECT * FROM workspaces WHERE company_id = ? AND name = ?",
            (company_id, name)
        ).fetchone()
        if row:
            results.append(dict(row))

    return results


def _get_workspaces(company_id: str) -> list[dict]:
    """Get all active workspaces for a company."""
    rows = _db().execute(
        "SELECT * FROM workspaces WHERE company_id = ? AND active = 1",
        (company_id,)
    ).fetchall()
    if not rows:
        # Auto-sync from config on first access
        return _sync_workspaces(company_id)
    return [dict(r) for r in rows]


def _get_workspace(company_id: str, name: str) -> dict | None:
    """Get a specific workspace by name."""
    row = _db().execute(
        "SELECT * FROM workspaces WHERE company_id = ? AND name = ?",
        (company_id, name)
    ).fetchone()
    if row:
        return dict(row)
    # Try syncing
    _sync_workspaces(company_id)
    row = _db().execute(
        "SELECT * FROM workspaces WHERE company_id = ? AND name = ?",
        (company_id, name)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_text(file_path: Path) -> str:
    """Extract text from supported file types."""
    suffix = file_path.suffix.lower()

    if suffix in (".md", ".txt", ".csv"):
        return file_path.read_text(encoding="utf-8", errors="replace")

    if suffix == ".eml":
        with open(file_path, "rb") as f:
            msg = BytesParser(policy=policy.default).parse(f)
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        return f"From: {msg['from']}\nTo: {msg['to']}\nSubject: {msg['subject']}\nDate: {msg['date']}\n\n{text}"

    if suffix == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages[:50]]
                return "\n\n".join(pages)
        except ImportError:
            log.warning("pdfplumber not installed, skipping PDF: %s", file_path)
            return ""

    return ""


def _file_hash(file_path: Path) -> str:
    """SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """Convert entity name to a filename-safe slug."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unnamed"


# ---------------------------------------------------------------------------
# Entity file management
# ---------------------------------------------------------------------------

def _read_entity_file(company_id: str, slug: str) -> str | None:
    """Read an entity .md file if it exists."""
    path = _entities_dir(company_id) / f"{slug}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _write_entity_file(company_id: str, slug: str, content: str) -> Path:
    """Write an entity .md file."""
    path = _entities_dir(company_id) / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _parse_entity_frontmatter(content: str) -> dict:
    """Parse key: value lines between '# Title' and first '## Section'.

    Returns dict with 'title' plus any frontmatter keys (type, aliases,
    birthday, handles, last_contact, subtype, tags, url, etc.)."""
    result: dict[str, str] = {}
    lines = content.split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and "title" not in result:
            result["title"] = stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            break  # end of frontmatter
        if ":" in stripped and not stripped.startswith("-"):
            key, val = stripped.split(":", 1)
            key = key.strip().lower()
            if key:
                result[key] = val.strip()
    return result


def _update_entity_frontmatter(
    company_id: str, slug: str, updates: dict
) -> bool:
    """Update specific frontmatter fields in an entity .md without LLM.

    Inserts new keys after the last frontmatter line (before first '##').
    Returns True if file was updated, False if entity not found."""
    content = _read_entity_file(company_id, slug)
    if content is None:
        return False

    lines = content.split("\n")
    new_lines: list[str] = []
    updated_keys: set[str] = set()
    insert_idx = -1  # where to insert new keys

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            insert_idx = len(new_lines)
            new_lines.append(line)
            new_lines.extend(lines[i + 1:])
            break
        # Check if this line matches a key we want to update
        if ":" in stripped and not stripped.startswith("-") and not stripped.startswith("#"):
            key = stripped.split(":", 1)[0].strip().lower()
            if key in updates:
                new_lines.append(f"{key}: {updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)
    else:
        # No ## found — append at end
        insert_idx = len(new_lines)

    # Insert any keys that weren't already in the file
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.insert(insert_idx, f"{key}: {val}")
            insert_idx += 1

    _write_entity_file(company_id, slug, "\n".join(new_lines))
    return True


def _create_raw_entity(
    company_id: str,
    workspace_id: int,
    name: str,
    entity_type: str,
    frontmatter: dict | None = None,
    body: str = "",
) -> str:
    """Create an entity .md file directly without LLM.

    Used for notes, ideas, bookmarks, CRM entries — anything that should
    be stored as raw user text rather than AI-synthesized content.
    Returns the slug."""
    slug = _slugify(name)
    fm = frontmatter or {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [f"# {name}"]
    lines.append(f"type: {entity_type}")
    for k, v in fm.items():
        if k not in ("type",):
            lines.append(f"{k}: {v}")
    lines.append("")
    if body:
        lines.append(body)
        lines.append("")
    lines.append("---")
    lines.append(f"_Last updated: {now}_")

    content = "\n".join(lines) + "\n"
    _write_entity_file(company_id, slug, content)

    # Upsert entity in DB
    conn = _db()
    aliases = fm.get("aliases", "[]")
    if not aliases.startswith("["):
        aliases = json.dumps([a.strip() for a in aliases.split(",") if a.strip()])
    try:
        conn.execute("""
            INSERT INTO entities (company_id, workspace_id, entity_type, name, slug, aliases, source_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'))
            ON CONFLICT(workspace_id, slug)
            DO UPDATE SET last_updated=datetime('now'), aliases=excluded.aliases
        """, (company_id, workspace_id, entity_type, name, slug, aliases))
        conn.commit()
    except Exception as e:
        log.error("Failed to upsert raw entity %s: %s", slug, e)

    return slug


def _append_to_entity_section(
    company_id: str, slug: str, section: str, text: str
) -> bool:
    """Append text to a named section (e.g. '## Facts') in an entity .md.

    If the section doesn't exist, creates it before the '---' footer."""
    content = _read_entity_file(company_id, slug)
    if content is None:
        return False

    lines = content.split("\n")
    section_header = f"## {section}"
    section_idx = -1
    next_section_idx = -1

    for i, line in enumerate(lines):
        if line.strip() == section_header:
            section_idx = i
        elif section_idx >= 0 and (line.strip().startswith("## ") or line.strip() == "---"):
            next_section_idx = i
            break

    if section_idx >= 0:
        # Insert before the next section/footer
        insert_at = next_section_idx if next_section_idx > 0 else len(lines)
        lines.insert(insert_at, text)
    else:
        # Section doesn't exist — add before '---' footer
        footer_idx = len(lines)
        for i, line in enumerate(lines):
            if line.strip() == "---":
                footer_idx = i
                break
        lines.insert(footer_idx, f"\n{section_header}")
        lines.insert(footer_idx + 1, text)

    _write_entity_file(company_id, slug, "\n".join(lines))
    return True


def _rebuild_index(company_id: str):
    """Regenerate wiki/index.md from all entity files."""
    ent_dir = _entities_dir(company_id)
    wiki_root = _wiki_dir(company_id)

    entities_by_type: dict[str, list[str]] = {}
    for f in sorted(ent_dir.glob("*.md")):
        content = f.read_text(encoding="utf-8", errors="replace")
        # Parse type from frontmatter
        etype = "unknown"
        for line in content.split("\n")[:5]:
            if line.startswith("type:"):
                etype = line.split(":", 1)[1].strip()
                break
        entities_by_type.setdefault(etype, []).append(f.stem)

    lines = [f"# Wiki Index\n\n_Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n"]
    for etype, slugs in sorted(entities_by_type.items()):
        lines.append(f"\n## {etype.title()} ({len(slugs)})\n")
        for slug in slugs:
            lines.append(f"- [[{slug}]]")

    total = sum(len(v) for v in entities_by_type.values())
    lines.insert(1, f"\n**{total} entities** across {len(entities_by_type)} types.\n")

    (wiki_root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_log(company_id: str, action: str, summary: str):
    """Append a timestamped line to wiki/log.md."""
    wiki_root = _wiki_dir(company_id)
    log_file = wiki_root / "log.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"- [{ts}] **{action}**: {summary}\n"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

async def _get_embedding(text: str) -> list[float] | None:
    """Get text embedding via Gemini or Anthropic embedding API."""
    if not _vec_available:
        return None

    try:
        ai_cfg = get_config().get("ai", {})
        provider = ai_cfg.get("api_provider", "anthropic")

        if provider == "gemini":
            import httpx
            api_key = ai_cfg.get("gemini_api_key", "")
            if not api_key:
                return None
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={api_key}",
                    json={"model": "models/text-embedding-004",
                          "content": {"parts": [{"text": text[:8000]}]}}
                )
                r.raise_for_status()
                return r.json()["embedding"]["values"]
        else:
            # Anthropic doesn't have an embedding API; use Voyage via env var
            voyage_key = ai_cfg.get("voyage_api_key", "")
            if not voyage_key:
                return None
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {voyage_key}"},
                    json={"model": "voyage-3-lite", "input": [text[:8000]]}
                )
                r.raise_for_status()
                return r.json()["data"][0]["embedding"]
    except Exception as e:
        log.warning("Embedding failed: %s", e)
        return None


def _upsert_embedding(entity_id: int, embedding: list[float]):
    """Upsert entity embedding into sqlite-vec."""
    if not _vec_available or not embedding:
        return
    try:
        conn = _db()
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        # Delete existing, then insert (vec0 doesn't support ON CONFLICT)
        conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
        conn.execute(
            "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
            (entity_id, blob)
        )
        conn.commit()
    except Exception as e:
        log.warning("Embedding upsert failed for entity %d: %s", entity_id, e)


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

ENTITY_EXTRACTION_SYSTEM = """You are a wiki editor. Given a raw document, extract all named entities (people, companies, projects, events, topics) and facts about them.

Return JSON only:
{
  "entities": [
    {
      "type": "person|company|project|event|topic",
      "name": "Canonical Name",
      "aliases": ["alt names"],
      "facts": ["fact 1", "fact 2"],
      "relationships": [{"target": "Other Entity", "type": "works_at|knows|manages|client_of|etc"}]
    }
  ],
  "summary": "one-line summary of the document"
}

Rules:
- Use canonical names (full proper names, not abbreviations)
- Distinguish entity types carefully
- Extract concrete facts, not vague statements
- Include dates when mentioned
- Keep facts concise (one sentence each)"""

ENTITY_MERGE_SYSTEM = """You are a wiki editor. Merge new facts into an existing entity page.

Rules:
- Preserve ALL existing facts unless directly contradicted by newer info
- Add source attribution for new facts
- Keep the exact same markdown format
- Update the "Last updated" timestamp to {now}
- Do NOT remove information unless it is clearly wrong
- Add new relationships discovered from the source
- If an existing fact is updated, note the change

Return the full updated markdown page (no code fences)."""

ENTITY_CREATE_TEMPLATE = """# {name}
type: {entity_type}
aliases: {aliases}

## Facts
{facts}

## Relationships
{relationships}

## Sources
- {source_file} ({date})

---
_Last updated: {now}_
"""


# ---------------------------------------------------------------------------
# Ingest pipeline
# ---------------------------------------------------------------------------

async def _ingest_file(
    workspace: dict,
    file_path: Path,
    company_id: str,
    source: str = "manual",
    source_text: str | None = None,
) -> str:
    """Ingest a single file: extract entities, update wiki.

    If source_text is provided, use it directly instead of reading the file.
    Returns a summary string.
    """
    conn = _db()
    ws_id = workspace["id"]

    # Compute relative path for display
    rel_path = str(file_path)
    for src_dir in json.loads(workspace.get("sources", "[]")):
        try:
            rel_path = str(file_path.relative_to(src_dir))
            break
        except ValueError:
            continue

    # Hash check for dedup (skip for event-sourced text)
    if source_text is None:
        fhash = _file_hash(file_path)
        existing = conn.execute(
            "SELECT id FROM ingestions WHERE workspace_id = ? AND source_file = ? AND source_hash = ? AND status = 'done'",
            (ws_id, rel_path, fhash)
        ).fetchone()
        if existing:
            return f"Skipped (unchanged): {rel_path}"
        text = _extract_text(file_path)
    else:
        fhash = hashlib.sha256(source_text.encode()).hexdigest()
        text = source_text

    if not text.strip():
        return f"Skipped (empty): {rel_path}"

    # Truncate very large documents
    if len(text) > 30000:
        text = text[:30000] + "\n\n[truncated]"

    # Load optional workspace schema
    schema_path = workspace.get("schema_path", "")
    schema_text = ""
    if schema_path and Path(schema_path).exists():
        schema_text = Path(schema_path).read_text(encoding="utf-8", errors="replace")

    system = ENTITY_EXTRACTION_SYSTEM
    if schema_text:
        system = f"{schema_text}\n\n---\n\n{system}"

    # Extract entities via LLM
    prompt = f"Source file: {rel_path}\n\n---\n\n{text}"
    result = await ask_llm(prompt, system=system, json_mode=True, max_tokens=4096)

    if not result or not isinstance(result, dict):
        conn.execute(
            "INSERT OR REPLACE INTO ingestions (company_id, workspace_id, source_file, source_hash, status, error) VALUES (?, ?, ?, ?, 'failed', 'LLM returned invalid JSON')",
            (company_id, ws_id, rel_path, fhash)
        )
        conn.commit()
        return f"Failed to extract entities from: {rel_path}"

    entities_data = result.get("entities", [])
    doc_summary = result.get("summary", "")
    entity_ids = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for ent in entities_data:
        ent_name = ent.get("name", "").strip()
        if not ent_name:
            continue

        slug = _slugify(ent_name)
        ent_type = ent.get("type", "unknown")
        aliases = ent.get("aliases", [])
        facts = ent.get("facts", [])
        relationships = ent.get("relationships", [])

        # Check if entity .md exists
        existing_content = _read_entity_file(company_id, slug)

        if existing_content:
            # Merge via LLM
            new_facts = "\n".join(f"- {f} (source: {rel_path})" for f in facts)
            new_rels = "\n".join(
                f"- [[{_slugify(r['target'])}]] -- {r.get('type', 'related')}"
                for r in relationships
            )
            merge_prompt = (
                f"Existing entity page:\n\n{existing_content}\n\n"
                f"---\n\nNew information from source '{rel_path}':\n\n"
                f"Facts:\n{new_facts}\n\n"
                f"Relationships:\n{new_rels}"
            )
            merged = await ask_llm(
                merge_prompt,
                system=ENTITY_MERGE_SYSTEM.format(now=now),
                max_tokens=4096
            )
            if merged and isinstance(merged, str):
                _write_entity_file(company_id, slug, merged)
        else:
            # Create new entity
            facts_text = "\n".join(f"- {f} (source: {rel_path})" for f in facts)
            rels_text = "\n".join(
                f"- [[{_slugify(r['target'])}]] -- {r.get('type', 'related')}"
                for r in relationships
            ) or "_(none yet)_"
            aliases_str = ", ".join(aliases) if aliases else "_(none)_"

            content = ENTITY_CREATE_TEMPLATE.format(
                name=ent_name,
                entity_type=ent_type,
                aliases=aliases_str,
                facts=facts_text or "_(none yet)_",
                relationships=rels_text,
                source_file=rel_path,
                date=datetime.now().strftime("%Y-%m-%d"),
                now=now,
            )
            _write_entity_file(company_id, slug, content)

        # Upsert entity in DB
        try:
            conn.execute("""
                INSERT INTO entities (company_id, workspace_id, entity_type, name, slug, aliases, source_count, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'))
                ON CONFLICT(workspace_id, slug)
                DO UPDATE SET last_updated=datetime('now'),
                              source_count=source_count+1,
                              aliases=excluded.aliases
            """, (company_id, ws_id, ent_type, ent_name, slug, json.dumps(aliases)))
            conn.commit()
        except Exception as e:
            log.error("Failed to upsert entity %s: %s", slug, e)

        row = conn.execute(
            "SELECT id FROM entities WHERE workspace_id = ? AND slug = ?",
            (ws_id, slug)
        ).fetchone()
        if row:
            eid = row["id"]
            entity_ids.append(eid)

            # Generate and store embedding
            entity_content = _read_entity_file(company_id, slug) or ""
            embedding = await _get_embedding(f"{ent_name}\n{entity_content[:2000]}")
            if embedding:
                _upsert_embedding(eid, embedding)

    # Record ingestion
    conn.execute("""
        INSERT OR REPLACE INTO ingestions (company_id, workspace_id, source_file, source_hash, entity_ids, status)
        VALUES (?, ?, ?, ?, ?, 'done')
    """, (company_id, ws_id, rel_path, fhash, json.dumps(entity_ids)))
    conn.commit()

    # Rebuild index and log
    _rebuild_index(company_id)
    summary = f"Ingested {rel_path}: {len(entity_ids)} entities ({doc_summary})"
    _append_log(company_id, "ingest", summary)

    conn.execute("""
        INSERT INTO action_log (company_id, workspace_id, action, summary, source, entity_ids)
        VALUES (?, ?, 'ingest', ?, ?, ?)
    """, (company_id, ws_id, summary, source, json.dumps(entity_ids)))
    conn.commit()

    return summary


async def _scan_and_ingest(workspace: dict, company_id: str) -> list[str]:
    """Scan all source folders for new/changed files and ingest them."""
    sources = json.loads(workspace.get("sources", "[]"))
    results = []

    for src_dir_str in sources:
        src_dir = Path(src_dir_str)
        if not src_dir.is_dir():
            results.append(f"Source not found: {src_dir_str}")
            continue

        for ext in SUPPORTED_EXTENSIONS:
            for file_path in src_dir.rglob(f"*{ext}"):
                if file_path.name.startswith(".") or "/.wiki/" in str(file_path):
                    continue
                try:
                    summary = await _ingest_file(workspace, file_path, company_id)
                    results.append(summary)
                except Exception as e:
                    results.append(f"Error ingesting {file_path.name}: {e}")
                    log.error("Ingest error for %s: %s", file_path, e, exc_info=True)

    return results


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def _search_entities(company_id: str, query: str, limit: int = 5) -> list[dict]:
    """Search entities via sqlite-vec (semantic) or keyword fallback."""
    conn = _db()

    # Try semantic search first
    if _vec_available:
        embedding = await _get_embedding(query)
        if embedding:
            try:
                import struct
                blob = struct.pack(f"{len(embedding)}f", *embedding)
                rows = conn.execute("""
                    SELECT e.name, e.slug, e.entity_type, ee.distance
                    FROM entity_embeddings ee
                    JOIN entities e ON e.id = ee.entity_id
                    WHERE e.company_id = ?
                      AND ee.embedding MATCH ?
                    ORDER BY ee.distance
                    LIMIT ?
                """, (company_id, blob, limit)).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.warning("Vec search failed, falling back to keyword: %s", e)

    # Keyword fallback
    rows = conn.execute("""
        SELECT name, slug, entity_type
        FROM entities
        WHERE company_id = ? AND (name LIKE ? OR aliases LIKE ?)
        LIMIT ?
    """, (company_id, f"%{query}%", f"%{query}%", limit)).fetchall()

    if not rows:
        # Grep entity files
        ent_dir = _entities_dir(company_id)
        matches = []
        for f in ent_dir.glob("*.md"):
            content = f.read_text(encoding="utf-8", errors="replace")
            if query.lower() in content.lower():
                # Extract first line as name
                first_line = content.split("\n")[0].lstrip("# ").strip()
                matches.append({"name": first_line, "slug": f.stem, "entity_type": "unknown"})
                if len(matches) >= limit:
                    break
        return matches

    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _on_email_received(event: str, data: dict):
    """Handle email.received — ingest email content into wiki."""
    company_id = data.get("company_id", "")
    workspaces = _get_workspaces(company_id)
    if not workspaces:
        return

    subject = data.get("subject", "no subject")
    sender = data.get("sender", "unknown")
    body = data.get("body_text", "")
    if not body:
        return

    text = f"From: {sender}\nSubject: {subject}\n\n{body}"
    safe_subject = re.sub(r'[^\w\s-]', '', subject)[:50].strip()
    filename = f"email-{datetime.now().strftime('%Y%m%d-%H%M')}-{_slugify(safe_subject)}.txt"

    for ws in workspaces:
        try:
            await _ingest_file(
                ws,
                Path(filename),
                company_id,
                source="mailwatch",
                source_text=text,
            )
        except Exception as e:
            log.error("Wiki email ingest failed for workspace %s: %s",
                      ws.get("name"), e, exc_info=True)


async def _on_ics_received(event: str, data: dict):
    """Handle email.ics_received — extract meeting details into wiki."""
    company_id = data.get("company_id", "")
    workspaces = _get_workspaces(company_id)
    if not workspaces:
        return

    ics_text = data.get("ics_text", "")
    subject = data.get("subject", "meeting")
    sender = data.get("sender", "unknown")

    text = f"Calendar invite from {sender}\nSubject: {subject}\n\n{ics_text}"
    filename = f"invite-{datetime.now().strftime('%Y%m%d-%H%M')}-{_slugify(subject)[:30]}.txt"

    for ws in workspaces:
        try:
            await _ingest_file(
                ws, Path(filename), company_id,
                source="mailwatch-ics", source_text=text,
            )
        except Exception as e:
            log.error("Wiki ICS ingest failed for workspace %s: %s",
                      ws.get("name"), e, exc_info=True)


async def _on_calendar_event(event: str, data: dict):
    """Handle calendar.event_created — update entity files for attendees."""
    company_id = data.get("company_id", "")
    workspaces = _get_workspaces(company_id)
    if not workspaces:
        return

    summary = data.get("summary", "event")
    attendees = data.get("attendees", [])
    start = data.get("start", "")

    text = f"Calendar event: {summary}\nDate: {start}\nAttendees: {', '.join(attendees)}"
    filename = f"cal-{datetime.now().strftime('%Y%m%d-%H%M')}-{_slugify(summary)[:30]}.txt"

    for ws in workspaces:
        try:
            await _ingest_file(
                ws, Path(filename), company_id,
                source="calendar", source_text=text,
            )
        except Exception as e:
            log.error("Wiki calendar ingest failed: %s", e, exc_info=True)


# Register event subscriptions
subscribe("email.received", _on_email_received, plugin_name="wiki")
subscribe("email.ics_received", _on_ics_received, plugin_name="wiki")
subscribe("calendar.event_created", _on_calendar_event, plugin_name="wiki")


# ---------------------------------------------------------------------------
# WhatsApp API helper
# ---------------------------------------------------------------------------

def _wa_api_get(path: str) -> list | dict | None:
    """Query the WhatsApp bot HTTP API (localhost:3100)."""
    import urllib.request
    import json as _json
    try:
        url = f"http://127.0.0.1:3100{path}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return _json.loads(resp.read())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# WhatsApp digest
# ---------------------------------------------------------------------------

async def _run_digest(company_id: str, contact_filter: str = "", days: int = 7) -> str:
    """Pull WhatsApp chats and ingest into wiki as synthetic sources."""
    status = _wa_api_get("/status")
    if status is None:
        return "WhatsApp API not reachable."

    chats = _wa_api_get("/chats?limit=200")
    if not chats:
        return "No WhatsApp chats found."

    workspaces = _get_workspaces(company_id)
    if not workspaces:
        return "No wiki workspaces configured."
    ws = workspaces[0]  # digest goes to first workspace

    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_ts = int(cutoff.timestamp())
    processed = 0
    total_msgs = 0

    for chat in chats:
        chat_name = chat.get("name", "Unknown")
        chat_id = chat.get("id", "")
        is_group = chat.get("is_group", False)
        if is_group:
            continue  # digest only 1:1 chats

        # Filter by contact name if specified
        if contact_filter and contact_filter.lower() not in chat_name.lower():
            continue

        messages = _wa_api_get(f"/messages/{chat_id}?limit=500")
        if not messages:
            continue

        # Filter to recent messages
        recent = []
        for m in messages:
            ts = m.get("timestamp", 0)
            if isinstance(ts, str):
                try:
                    ts = int(datetime.fromisoformat(ts).timestamp())
                except Exception:
                    ts = 0
            if ts >= cutoff_ts:
                recent.append(m)

        if not recent:
            continue

        # Build synthetic source text
        lines = [f"WhatsApp chat with {chat_name}"]
        lines.append(f"Period: {cutoff.strftime('%Y-%m-%d')} to {datetime.now().strftime('%Y-%m-%d')}")
        lines.append("")
        for m in recent:
            ts = m.get("timestamp", 0)
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            else:
                dt = str(ts)[:16]
            sender = "Me" if m.get("is_from_me") else m.get("sender_name", chat_name)
            content = m.get("content", "")
            if content:
                lines.append(f"[{dt}] {sender}: {content}")

        if len(lines) <= 3:  # only header, no messages
            continue

        source_text = "\n".join(lines)
        filename = f"wa-digest-{datetime.now().strftime('%Y%m%d')}-{_slugify(chat_name)[:30]}.txt"

        try:
            await _ingest_file(
                ws, Path(filename), company_id,
                source="whatsapp-digest", source_text=source_text,
            )
            # Update last_contact on person entity
            slug = _slugify(chat_name)
            _update_entity_frontmatter(
                company_id, slug,
                {"last_contact": datetime.now().strftime("%Y-%m-%d")},
            )
            processed += 1
            total_msgs += len(recent)
        except Exception as e:
            log.error("Digest ingest failed for %s: %s", chat_name, e, exc_info=True)

    return f"Digest complete: {processed} contact(s), {total_msgs} message(s) processed."


async def _handle_digest_job(payload: dict):
    """Job handler for weekly auto-digest. Self-re-enqueues."""
    company_id = payload.get("company_id", "")
    days = payload.get("days", 7)
    chat_id = payload.get("chat_id", "")

    result = await _run_digest(company_id, days=days)
    log.info("Auto-digest result: %s", result)

    # Send result to user if chat_id is available
    if chat_id:
        try:
            from cupbots.helpers.channel import send_wa_message
            await send_wa_message(chat_id, f"Weekly wiki digest:\n{result}")
        except Exception:
            pass

    # Re-enqueue for next week
    _schedule_next_digest(company_id, chat_id)


def _schedule_next_digest(company_id: str, chat_id: str = ""):
    """Schedule the next weekly digest job."""
    cfg = get_config().get("wiki", {}).get("digest", {})
    day_name = cfg.get("day", "monday").lower()
    hour = cfg.get("hour", 9)
    days_lookup = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                   "friday": 4, "saturday": 5, "sunday": 6}
    target_day = days_lookup.get(day_name, 0)

    now = datetime.now()
    days_ahead = target_day - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    from datetime import timedelta
    next_run = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )

    enqueue("wiki_digest", {
        "company_id": company_id,
        "chat_id": chat_id,
        "days": cfg.get("days", 7),
    }, run_at=next_run)


register_handler("wiki_digest", _handle_digest_job)


# ---------------------------------------------------------------------------
# CRM helpers
# ---------------------------------------------------------------------------

def _find_person_entity(company_id: str, name: str) -> dict | None:
    """Find a person entity by fuzzy name match. Returns DB row dict or None."""
    conn = _db()
    slug = _slugify(name)
    # Exact slug match first
    row = conn.execute(
        "SELECT * FROM entities WHERE company_id = ? AND slug = ? AND entity_type = 'person'",
        (company_id, slug)
    ).fetchone()
    if row:
        return dict(row)

    # Keyword match
    rows = conn.execute(
        "SELECT * FROM entities WHERE company_id = ? AND entity_type = 'person' AND (name LIKE ? OR aliases LIKE ?)",
        (company_id, f"%{name}%", f"%{name}%")
    ).fetchall()
    if rows:
        return dict(rows[0])
    return None


def _get_default_workspace_id(company_id: str) -> int:
    """Get the ID of the first workspace for this company."""
    workspaces = _get_workspaces(company_id)
    if workspaces:
        return workspaces[0]["id"]
    return 0


# ---------------------------------------------------------------------------
# FAQ helpers
# ---------------------------------------------------------------------------

FAQ_MIN_SCORE = 0.3


def _faq_tokenize(text: str) -> set[str]:
    return set(re.findall(r'\w+', text.lower()))


def _faq_score(q_tokens: set[str], a_tokens: set[str]) -> float:
    if not q_tokens or not a_tokens:
        return 0.0
    return len(q_tokens & a_tokens) / max(len(q_tokens), len(a_tokens))


def _faq_search(chat_id: str, company_id: str, query: str) -> tuple[dict | None, float]:
    conn = _db()
    rows = conn.execute(
        "SELECT id, question, answer FROM faq_entries WHERE chat_id = ? AND company_id = ?",
        (chat_id, company_id),
    ).fetchall()
    if not rows:
        return None, 0.0
    q_tokens = _faq_tokenize(query)
    best, best_score = None, 0.0
    for row in rows:
        s = _faq_score(q_tokens, _faq_tokenize(row["question"]))
        if s > best_score:
            best, best_score = dict(row), s
    return best, best_score


def _faq_is_auto(chat_id: str) -> bool:
    row = _db().execute(
        "SELECT auto_answer FROM faq_config WHERE chat_id = ?", (chat_id,),
    ).fetchone()
    return bool(row and row["auto_answer"])


def _faq_set_auto(chat_id: str, company_id: str, enabled: bool):
    _db().execute(
        """INSERT INTO faq_config (chat_id, company_id, auto_answer)
           VALUES (?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET auto_answer = excluded.auto_answer""",
        (chat_id, company_id, int(enabled)),
    )
    _db().commit()


# ---------------------------------------------------------------------------
# Auto-answer (handle_message hook)
# ---------------------------------------------------------------------------

AUTO_ANSWER_THRESHOLD = 0.75


async def handle_message(msg, reply) -> bool | str | None:
    """Auto-answer from wiki or FAQ if the group is configured.

    Checks FAQ first (fast, no LLM), then wiki entities (semantic + LLM).
    Returns True if answered (stops AI from running), False/None to pass through.
    """
    if msg.command or not msg.text:
        return False

    company_id = msg.company_id or ""
    query = msg.text.strip()

    # FAQ auto-answer (fast, no LLM cost)
    if _faq_is_auto(msg.chat_id):
        entry, score = _faq_search(msg.chat_id, company_id, query)
        if entry and score >= FAQ_MIN_SCORE:
            _db().execute("UPDATE faq_entries SET views = views + 1 WHERE id = ?", (entry["id"],))
            _db().commit()
            await reply.reply_text(entry["answer"])
            return True

    # Wiki auto-answer (requires group metadata + LLM)
    group_cfg = msg.group_config
    if not group_cfg:
        return False

    wiki_ws = (group_cfg.get("metadata") or {}).get("wiki", "")
    if not wiki_ws:
        return False

    if len(query) < 5:
        return False

    try:
        ws = _get_workspace(company_id, wiki_ws)
        if not ws:
            return False

        results = await _search_entities_in_workspace(company_id, ws["id"], query, limit=3)
        if not results:
            return False

        best = results[0]
        if "distance" in best and best["distance"] > (1 - AUTO_ANSWER_THRESHOLD):
            return False

        content = _read_entity_file(company_id, best["slug"])
        if not content:
            return False

        answer = await ask_llm(
            f"Question: {query}\n\nWiki context:\n{content[:3000]}",
            system="Answer the question concisely using ONLY the wiki context provided. If the context doesn't contain enough information to answer, say so briefly. Keep your answer under 200 words.",
            max_tokens=500,
        )
        if answer and isinstance(answer, str):
            await reply.reply_text(answer)
            return True
    except Exception as e:
        log.debug("Wiki auto-answer failed: %s", e)

    return False


async def _search_entities_in_workspace(
    company_id: str, workspace_id: int, query: str, limit: int = 5
) -> list[dict]:
    """Search entities within a specific workspace via sqlite-vec or keyword fallback."""
    conn = _db()

    if _vec_available:
        embedding = await _get_embedding(query)
        if embedding:
            try:
                import struct
                blob = struct.pack(f"{len(embedding)}f", *embedding)
                rows = conn.execute("""
                    SELECT e.name, e.slug, e.entity_type, ee.distance
                    FROM entity_embeddings ee
                    JOIN entities e ON e.id = ee.entity_id
                    WHERE e.company_id = ? AND e.workspace_id = ?
                      AND ee.embedding MATCH ?
                    ORDER BY ee.distance
                    LIMIT ?
                """, (company_id, workspace_id, blob, limit)).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                log.warning("Vec search failed in workspace, falling back: %s", e)

    # Keyword fallback
    rows = conn.execute("""
        SELECT name, slug, entity_type
        FROM entities
        WHERE company_id = ? AND workspace_id = ? AND (name LIKE ? OR aliases LIKE ?)
        LIMIT ?
    """, (company_id, workspace_id, f"%{query}%", f"%{query}%", limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

def _parse_workspace_flag(args: list[str]) -> tuple[str, list[str]]:
    """Extract --workspace <name> from args. Returns (workspace_name, remaining_args)."""
    ws_name = ""
    remaining = []
    i = 0
    while i < len(args):
        if args[i] in ("--workspace", "-w") and i + 1 < len(args):
            ws_name = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return ws_name, remaining


async def handle_command(msg, reply) -> bool:
    cmd = msg.command
    # Route alias commands to wiki subcommands
    if cmd == "crm":
        return await _handle_crm(msg, reply)
    if cmd in ("note",):
        return await _handle_note(msg, reply)
    if cmd == "idea":
        return await _handle_idea(msg, reply)
    if cmd in ("ideas",):
        return await _handle_list_subtype(msg, reply, "idea")
    if cmd in ("bookmarks",):
        return await _handle_list_subtype(msg, reply, "bookmark")
    if cmd == "faq":
        return await _handle_faq(msg, reply)

    if cmd != "wiki":
        return False

    args = msg.args or []
    company_id = msg.company_id or ""

    if not args or args[0] in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    sub = args[0].lower()
    ws_name, remaining = _parse_workspace_flag(args[1:])

    if sub == "workspaces":
        workspaces = _get_workspaces(company_id)
        if not workspaces:
            await reply.reply_text("No wiki workspaces configured.\n\nAdd to config.yaml:\n```\nwiki:\n  workspaces:\n    my_wiki:\n      sources:\n        - /path/to/docs\n```")
            return True

        lines = ["*Wiki Workspaces*\n"]
        for ws in workspaces:
            sources = json.loads(ws.get("sources", "[]"))
            entity_count = _db().execute(
                "SELECT COUNT(*) FROM entities WHERE workspace_id = ?",
                (ws["id"],)
            ).fetchone()[0]
            ingest_count = _db().execute(
                "SELECT COUNT(*) FROM ingestions WHERE workspace_id = ? AND status = 'done'",
                (ws["id"],)
            ).fetchone()[0]
            lines.append(f"*{ws['name']}*")
            lines.append(f"  Sources: {len(sources)} folder(s)")
            for s in sources:
                lines.append(f"    - {s}")
            lines.append(f"  Entities: {entity_count}")
            lines.append(f"  Ingested files: {ingest_count}")
            lines.append("")

        await reply.reply_text("\n".join(lines))

    elif sub == "ingest":
        workspaces = _get_workspaces(company_id)
        if not workspaces:
            await reply.reply_text("No wiki workspaces configured. Use /wiki workspaces for setup help.")
            return True

        if ws_name:
            ws = _get_workspace(company_id, ws_name)
            if not ws:
                await reply.reply_text(f"Workspace '{ws_name}' not found.")
                return True
            target_workspaces = [ws]
        else:
            target_workspaces = workspaces

        if remaining:
            filename = " ".join(remaining)
            for ws in target_workspaces:
                for src_dir_str in json.loads(ws.get("sources", "[]")):
                    file_path = Path(src_dir_str) / filename
                    if file_path.exists():
                        await reply.reply_text(f"Ingesting {filename}...")
                        result = await _ingest_file(ws, file_path, company_id)
                        await reply.reply_text(result)
                        return True
            await reply.reply_text(f"File not found: {filename}")
        else:
            await reply.reply_text("Scanning source folders for new/changed files...")
            all_results = []
            for ws in target_workspaces:
                results = await _scan_and_ingest(ws, company_id)
                all_results.extend(results)

            if not all_results:
                await reply.reply_text("No new or changed files found.")
            else:
                ingested = [r for r in all_results if not r.startswith("Skipped")]
                skipped = len(all_results) - len(ingested)
                summary = f"Done. {len(ingested)} ingested, {skipped} skipped (unchanged)."
                if ingested:
                    summary += "\n\n" + "\n".join(ingested[:20])
                    if len(ingested) > 20:
                        summary += f"\n... and {len(ingested) - 20} more"
                await reply.reply_text(summary)

    elif sub == "search":
        if not remaining:
            await reply.reply_text("Usage: /wiki search <query>")
            return True
        query = " ".join(remaining)
        results = await _search_entities(company_id, query)
        if not results:
            await reply.reply_text(f"No entities found for: {query}")
        else:
            lines = [f"*Search results for '{query}':*\n"]
            for r in results:
                distance = f" (score: {1 - r['distance']:.2f})" if "distance" in r else ""
                lines.append(f"- *{r['name']}* [{r['entity_type']}]{distance}")
                lines.append(f"  /wiki show {r['slug']}")
            await reply.reply_text("\n".join(lines))

    elif sub == "entities":
        workspaces = _get_workspaces(company_id)
        ws_filter = ""
        params: list[Any] = [company_id]
        if ws_name:
            ws = _get_workspace(company_id, ws_name)
            if ws:
                ws_filter = " AND workspace_id = ?"
                params.append(ws["id"])

        rows = _db().execute(
            f"SELECT name, slug, entity_type, last_updated FROM entities WHERE company_id = ?{ws_filter} ORDER BY entity_type, name",
            params
        ).fetchall()

        if not rows:
            await reply.reply_text("No entities yet. Use /wiki ingest to process source files.")
            return True

        by_type: dict[str, list] = {}
        for r in rows:
            by_type.setdefault(r["entity_type"], []).append(r)

        lines = [f"*Wiki Entities ({len(rows)} total)*\n"]
        for etype, ents in sorted(by_type.items()):
            lines.append(f"\n*{etype.title()}* ({len(ents)})")
            for e in ents[:20]:
                lines.append(f"  - {e['name']}")
            if len(ents) > 20:
                lines.append(f"  ... and {len(ents) - 20} more")
        await reply.reply_text("\n".join(lines))

    elif sub == "show":
        if not remaining:
            await reply.reply_text("Usage: /wiki show <entity-name>")
            return True
        query = " ".join(remaining)
        slug = _slugify(query)
        content = _read_entity_file(company_id, slug)
        if not content:
            results = await _search_entities(company_id, query, limit=1)
            if results:
                slug = results[0]["slug"]
                content = _read_entity_file(company_id, slug)
        if content:
            await reply.reply_text(content)
        else:
            await reply.reply_text(f"Entity not found: {query}\n\nUse /wiki search {query} to find similar entities.")

    elif sub == "log":
        limit_n = 20
        if remaining and remaining[0] == "--limit" and len(remaining) > 1:
            try:
                limit_n = int(remaining[1])
            except ValueError:
                pass
        rows = _db().execute(
            "SELECT action, summary, source, created_at FROM action_log WHERE company_id = ? ORDER BY created_at DESC LIMIT ?",
            (company_id, limit_n)
        ).fetchall()
        if not rows:
            await reply.reply_text("No wiki activity yet.")
            return True
        lines = ["*Wiki Activity Log*\n"]
        for r in rows:
            src = f" ({r['source']})" if r["source"] else ""
            lines.append(f"[{r['created_at']}] *{r['action']}*{src}: {r['summary']}")
        await reply.reply_text("\n".join(lines))

    elif sub == "sync":
        workspaces = _sync_workspaces(company_id)
        await reply.reply_text(f"Synced {len(workspaces)} workspace(s) from config.yaml.")

    # --- Digest ---
    elif sub == "digest":
        if not remaining:
            await reply.reply_text("Running digest (all chats, last 7 days)...")
            result = await _run_digest(company_id)
            await reply.reply_text(result)
        elif remaining[0].lower() == "on":
            from cupbots.config import update_config_key
            update_config_key("wiki.digest.enabled", True)
            _schedule_next_digest(company_id, msg.chat_id)
            await reply.reply_text("Weekly digest enabled. Next run scheduled.")
        elif remaining[0].lower() == "off":
            from cupbots.config import update_config_key
            update_config_key("wiki.digest.enabled", False)
            for job in get_pending_jobs("wiki_digest"):
                cancel_job(str(job["id"]))
            await reply.reply_text("Weekly digest disabled.")
        elif remaining[0].lower() == "status":
            cfg = get_config().get("wiki", {}).get("digest", {})
            enabled = cfg.get("enabled", False)
            pending = get_pending_jobs("wiki_digest")
            lines = [f"Digest: {'enabled' if enabled else 'disabled'}"]
            if pending:
                lines.append(f"Next run: {pending[0].get('run_at', 'unknown')}")
            else:
                lines.append("No pending digest jobs.")
            lines.append(f"Schedule: {cfg.get('day', 'monday')} at {cfg.get('hour', 9)}:00")
            lines.append(f"History: {cfg.get('days', 7)} days")
            await reply.reply_text("\n".join(lines))
        else:
            # Filter by contact name
            contact_name = " ".join(remaining)
            days_flag = 7
            if "--days" in remaining:
                idx = remaining.index("--days")
                if idx + 1 < len(remaining):
                    try:
                        days_flag = int(remaining[idx + 1])
                    except ValueError:
                        pass
                    contact_name = " ".join(remaining[:idx])
            await reply.reply_text(f"Running digest for '{contact_name}'...")
            result = await _run_digest(company_id, contact_filter=contact_name, days=days_flag)
            await reply.reply_text(result)

    # --- Query ---
    elif sub == "query":
        if not remaining:
            await reply.reply_text("Usage: /wiki query <question>")
            return True
        question = " ".join(remaining)
        results = await _search_entities(company_id, question, limit=5)
        if not results:
            await reply.reply_text("No relevant entities found to answer your question.")
            return True

        # Gather context from top entities
        context_parts = []
        for r in results[:3]:
            content = _read_entity_file(company_id, r["slug"])
            if content:
                context_parts.append(f"--- {r['name']} ---\n{content[:3000]}")

        wiki_context = "\n\n".join(context_parts)
        answer = await ask_llm(
            f"Question: {question}\n\nWiki context:\n{wiki_context}",
            system=(
                "Answer the question using the wiki context provided. "
                "Cite which entities informed your answer using [Entity Name]. "
                "If the context is insufficient, say so. Keep under 300 words."
            ),
            max_tokens=800,
        )
        if answer and isinstance(answer, str):
            await reply.reply_text(answer)
        else:
            await reply.reply_text("Could not generate an answer.")

    # --- Lint ---
    elif sub == "lint":
        await reply.reply_text("Running wiki health check...")
        issues = []
        ent_dir = _entities_dir(company_id)
        all_slugs = {f.stem for f in ent_dir.glob("*.md")}

        for f in ent_dir.glob("*.md"):
            content = f.read_text(encoding="utf-8", errors="replace")
            fm = _parse_entity_frontmatter(content)

            # Check for broken cross-references
            for match in re.findall(r'\[\[([^\]]+)\]\]', content):
                ref_slug = _slugify(match)
                if ref_slug not in all_slugs:
                    issues.append(f"Broken link: [[{match}]] in {f.stem}")

            # Check for missing source attribution
            if "## Facts" in content:
                facts_section = content.split("## Facts")[1].split("##")[0]
                for line in facts_section.split("\n"):
                    line = line.strip()
                    if line.startswith("- ") and "(source:" not in line.lower() and line != "- _(none yet)_":
                        issues.append(f"Missing source: {f.stem}: {line[:60]}")

            # Check stale person entities
            if fm.get("type") == "person" and fm.get("last_contact"):
                try:
                    last = datetime.fromisoformat(fm["last_contact"])
                    from datetime import timedelta
                    if datetime.now() - last > timedelta(days=90):
                        issues.append(f"Stale contact: {fm.get('title', f.stem)} (last: {fm['last_contact']})")
                except Exception:
                    pass

        # Check DB entities with no file
        rows = _db().execute(
            "SELECT slug, name FROM entities WHERE company_id = ?", (company_id,)
        ).fetchall()
        for r in rows:
            if r["slug"] not in all_slugs:
                issues.append(f"Orphan DB entry: {r['name']} ({r['slug']}) — no .md file")

        if not issues:
            await reply.reply_text("Wiki health check passed. No issues found.")
        else:
            lines = [f"*Wiki Health Check: {len(issues)} issue(s)*\n"]
            for issue in issues[:30]:
                lines.append(f"- {issue}")
            if len(issues) > 30:
                lines.append(f"\n... and {len(issues) - 30} more")
            await reply.reply_text("\n".join(lines))

    # --- CRM subcommands via /wiki ---
    elif sub in ("whois", "remember", "touched", "birthday"):
        msg.args = [sub] + remaining
        return await _handle_crm(msg, reply)

    # --- Note/idea/bookmark via /wiki ---
    elif sub == "note":
        msg.args = remaining
        return await _handle_note(msg, reply)
    elif sub == "idea":
        msg.args = remaining
        return await _handle_idea(msg, reply)
    elif sub in ("notes", "ideas", "bookmarks"):
        subtype = sub.rstrip("s") if sub != "notes" else "note"
        return await _handle_list_subtype(msg, reply, subtype)
    elif sub == "save":
        msg.args = remaining
        return await _handle_save(msg, reply)

    # --- FAQ via /wiki ---
    elif sub == "faq":
        msg.args = remaining
        return await _handle_faq(msg, reply)

    else:
        await reply.reply_text(f"Unknown subcommand: {sub}\n\nUse /wiki --help for available commands.")

    return True


# ---------------------------------------------------------------------------
# CRM command handler (/crm alias)
# ---------------------------------------------------------------------------

async def _handle_crm(msg, reply) -> bool:
    args = msg.args or []
    company_id = msg.company_id or ""

    if not args or args[0] in ("--help", "-h", "help"):
        # Default: show overdue contacts
        rows = _db().execute(
            "SELECT name, slug FROM entities WHERE company_id = ? AND entity_type = 'person'",
            (company_id,)
        ).fetchall()
        if not rows:
            await reply.reply_text("No contacts in wiki yet. Use `/crm remember <name> -- <note>` to add one.")
            return True

        overdue = []
        for r in rows:
            content = _read_entity_file(company_id, r["slug"])
            if not content:
                continue
            fm = _parse_entity_frontmatter(content)
            last = fm.get("last_contact", "")
            if not last:
                overdue.append(f"- *{r['name']}* — never contacted")
                continue
            try:
                from datetime import timedelta
                last_dt = datetime.fromisoformat(last)
                if datetime.now() - last_dt > timedelta(days=30):
                    days_ago = (datetime.now() - last_dt).days
                    overdue.append(f"- *{r['name']}* — {days_ago} days ago")
            except Exception:
                pass

        if overdue:
            await reply.reply_text("*Overdue contacts:*\n\n" + "\n".join(overdue[:20]))
        else:
            await reply.reply_text("All contacts are up to date.")
        return True

    sub = args[0].lower()

    if sub == "whois":
        if len(args) < 2:
            await reply.reply_text("Usage: /crm whois <name>")
            return True
        name = " ".join(args[1:])
        person = _find_person_entity(company_id, name)
        if not person:
            await reply.reply_text(f"No person found: {name}")
            return True
        content = _read_entity_file(company_id, person["slug"])
        if content:
            await reply.reply_text(content)
        else:
            await reply.reply_text(f"Entity file missing for: {person['name']}")

    elif sub == "remember":
        raw = " ".join(args[1:])
        # Split on -- or —
        if " -- " in raw:
            name, note = raw.split(" -- ", 1)
        elif " — " in raw:
            name, note = raw.split(" — ", 1)
        else:
            await reply.reply_text("Usage: /crm remember <name> -- <note>")
            return True

        name, note = name.strip(), note.strip()
        person = _find_person_entity(company_id, name)
        if person:
            now = datetime.now().strftime("%Y-%m-%d")
            _append_to_entity_section(
                company_id, person["slug"], "Facts",
                f"- {note} (source: manual, {now})"
            )
            await reply.reply_text(f"Added to {person['name']}.")
        else:
            # Create new person entity
            ws_id = _get_default_workspace_id(company_id)
            now = datetime.now().strftime("%Y-%m-%d")
            slug = _create_raw_entity(
                company_id, ws_id, name, "person",
                body=f"## Facts\n- {note} (source: manual, {now})\n\n## Relationships\n_(none yet)_\n\n## Sources\n- manual ({now})"
            )
            await reply.reply_text(f"Created contact: {name}")

    elif sub == "touched":
        if len(args) < 2:
            await reply.reply_text("Usage: /crm touched <name> [note]")
            return True
        # First word after 'touched' is the name, rest is note
        raw = " ".join(args[1:])
        parts = raw.split(None, 1)
        name = parts[0]
        note = parts[1] if len(parts) > 1 else ""

        person = _find_person_entity(company_id, name)
        if not person:
            await reply.reply_text(f"No person found: {name}")
            return True

        today = datetime.now().strftime("%Y-%m-%d")
        _update_entity_frontmatter(company_id, person["slug"], {"last_contact": today})
        if note:
            _append_to_entity_section(
                company_id, person["slug"], "Facts",
                f"- {note} (source: manual, {today})"
            )
        await reply.reply_text(f"Updated last contact for {person['name']}.")

    elif sub == "birthday":
        if len(args) < 3:
            await reply.reply_text("Usage: /crm birthday <name> <date>\nExample: /crm birthday Sarah 1990-03-15")
            return True
        date_str = args[-1]
        name = " ".join(args[1:-1])
        person = _find_person_entity(company_id, name)
        if not person:
            await reply.reply_text(f"No person found: {name}")
            return True
        _update_entity_frontmatter(company_id, person["slug"], {"birthday": date_str})
        await reply.reply_text(f"Birthday set for {person['name']}: {date_str}")

    elif sub == "list":
        rows = _db().execute(
            "SELECT name, slug FROM entities WHERE company_id = ? AND entity_type = 'person' ORDER BY name",
            (company_id,)
        ).fetchall()
        if not rows:
            await reply.reply_text("No contacts yet.")
            return True
        lines = [f"*Contacts ({len(rows)})*\n"]
        for r in rows[:30]:
            content = _read_entity_file(company_id, r["slug"])
            fm = _parse_entity_frontmatter(content) if content else {}
            last = fm.get("last_contact", "—")
            bday = fm.get("birthday", "")
            extra = f" | bday: {bday}" if bday else ""
            lines.append(f"- {r['name']} (last: {last}{extra})")
        if len(rows) > 30:
            lines.append(f"\n... and {len(rows) - 30} more")
        await reply.reply_text("\n".join(lines))

    else:
        await reply.reply_text(f"Unknown CRM command: {sub}\n\nUse /crm --help")

    return True


# ---------------------------------------------------------------------------
# Note command handler (/note alias)
# ---------------------------------------------------------------------------

async def _handle_note(msg, reply) -> bool:
    args = msg.args or []
    company_id = msg.company_id or ""

    if not args or args[0] in ("--help", "-h", "help"):
        await reply.reply_text(
            "Usage:\n"
            "  /note <title> [-- body]\n"
            "  /note list [#tag]\n"
            "  /note save <url> [#tag]\n"
            "  /note idea <description>\n"
            "  /note ideas"
        )
        return True

    sub = args[0].lower()

    if sub == "list":
        return await _handle_list_subtype(msg, reply, "note", filter_arg=" ".join(args[1:]))

    if sub == "save":
        msg.args = args[1:]
        return await _handle_save(msg, reply)

    if sub == "idea":
        msg.args = args[1:]
        return await _handle_idea(msg, reply)

    if sub in ("ideas",):
        return await _handle_list_subtype(msg, reply, "idea")

    if sub in ("bookmarks",):
        return await _handle_list_subtype(msg, reply, "bookmark")

    if sub == "unsave":
        # Remove a bookmark
        if len(args) < 2:
            await reply.reply_text("Usage: /note unsave <url or keyword>")
            return True
        query = " ".join(args[1:])
        slug = _slugify(query)
        # Try exact slug first, then search
        content = _read_entity_file(company_id, slug)
        if not content:
            results = await _search_entities(company_id, query, limit=1)
            if results:
                slug = results[0]["slug"]
        path = _entities_dir(company_id) / f"{slug}.md"
        if path.exists():
            path.unlink()
            _db().execute("DELETE FROM entities WHERE company_id = ? AND slug = ?", (company_id, slug))
            _db().commit()
            _rebuild_index(company_id)
            await reply.reply_text(f"Removed: {slug}")
        else:
            await reply.reply_text(f"Not found: {query}")
        return True

    # Create a note
    raw = " ".join(args)
    tags = re.findall(r"#(\w+)", raw)
    raw_clean = re.sub(r"\s*#\w+", "", raw).strip()

    if " -- " in raw_clean:
        title, body = raw_clean.split(" -- ", 1)
    elif " — " in raw_clean:
        title, body = raw_clean.split(" — ", 1)
    else:
        title, body = raw_clean, ""

    title, body = title.strip(), body.strip()
    if not title:
        await reply.reply_text("Usage: /note <title> [-- body]")
        return True

    # If replying to a message, use that as body
    if hasattr(msg, "quoted_text") and msg.quoted_text and not body:
        body = msg.quoted_text

    ws_id = _get_default_workspace_id(company_id)
    fm = {"subtype": "note", "created": datetime.now().strftime("%Y-%m-%d")}
    if tags:
        fm["tags"] = ", ".join(tags)
    _create_raw_entity(company_id, ws_id, title, "topic", frontmatter=fm, body=body)
    _rebuild_index(company_id)
    tag_display = f" [{', '.join('#' + t for t in tags)}]" if tags else ""
    await reply.reply_text(f"Note saved: {title}{tag_display}")
    return True


# ---------------------------------------------------------------------------
# Idea command handler (/idea alias)
# ---------------------------------------------------------------------------

async def _handle_idea(msg, reply) -> bool:
    args = msg.args or []
    company_id = msg.company_id or ""

    if not args:
        await reply.reply_text("Usage: /idea <description>")
        return True

    description = " ".join(args)
    ws_id = _get_default_workspace_id(company_id)
    year = datetime.now().strftime("%Y")
    fm = {"subtype": "idea", "created": datetime.now().strftime("%Y-%m-%d"), "year": year}
    _create_raw_entity(company_id, ws_id, description, "topic", frontmatter=fm)
    _rebuild_index(company_id)
    await reply.reply_text(f"Idea captured: {description}")
    return True


# ---------------------------------------------------------------------------
# Bookmark command handler (/note save alias)
# ---------------------------------------------------------------------------

async def _handle_save(msg, reply) -> bool:
    args = msg.args or []
    company_id = msg.company_id or ""

    if not args:
        await reply.reply_text("Usage: /note save <url> [title] [#tag]")
        return True

    url = args[0]
    rest = " ".join(args[1:])
    tags = re.findall(r"#(\w+)", rest)
    title = re.sub(r"\s*#\w+", "", rest).strip() or url

    ws_id = _get_default_workspace_id(company_id)
    fm = {"subtype": "bookmark", "url": url, "created": datetime.now().strftime("%Y-%m-%d")}
    if tags:
        fm["tags"] = ", ".join(tags)
    _create_raw_entity(company_id, ws_id, title, "topic", frontmatter=fm)
    _rebuild_index(company_id)
    tag_display = f" [{', '.join('#' + t for t in tags)}]" if tags else ""
    await reply.reply_text(f"Bookmarked: {title}{tag_display}")
    return True


# ---------------------------------------------------------------------------
# List entities by subtype
# ---------------------------------------------------------------------------

async def _handle_list_subtype(msg, reply, subtype: str, filter_arg: str = "") -> bool:
    company_id = msg.company_id or ""
    ent_dir = _entities_dir(company_id)
    items = []

    for f in sorted(ent_dir.glob("*.md"), reverse=True):
        content = f.read_text(encoding="utf-8", errors="replace")
        fm = _parse_entity_frontmatter(content)
        if fm.get("subtype") != subtype:
            continue
        if filter_arg:
            # Filter by tag
            tag_filter = filter_arg.lstrip("#").lower()
            entity_tags = fm.get("tags", "").lower()
            if tag_filter not in entity_tags and tag_filter not in content.lower():
                continue
        items.append(fm)

    if not items:
        await reply.reply_text(f"No {subtype}s found.")
        return True

    label = subtype.title() + "s"
    lines = [f"*{label} ({len(items)})*\n"]
    for item in items[:20]:
        title = item.get("title", "untitled")
        created = item.get("created", "")
        extra = ""
        if subtype == "bookmark":
            extra = f" — {item.get('url', '')}"
        elif subtype == "idea":
            extra = f" ({item.get('year', '')})"
        tags = item.get("tags", "")
        tag_str = f" [{tags}]" if tags else ""
        lines.append(f"- {title}{extra}{tag_str} {created}")

    if len(items) > 20:
        lines.append(f"\n... and {len(items) - 20} more")
    await reply.reply_text("\n".join(lines))
    return True


# ---------------------------------------------------------------------------
# FAQ command handler (/faq alias)
# ---------------------------------------------------------------------------

async def _handle_faq(msg, reply) -> bool:
    args = msg.args or []
    company_id = msg.company_id or ""
    chat_id = msg.chat_id
    sender_id = msg.sender_id

    if not args or args[0] in ("--help", "-h", "help"):
        await reply.reply_text(
            "Usage:\n"
            "  /faq <query> — Search FAQ\n"
            "  /faq add <question> | <answer>\n"
            "  /faq remove <id>\n"
            "  /faq list\n"
            "  /faq auto on|off"
        )
        return True

    sub = args[0].lower()
    conn = _db()

    if sub == "add":
        raw = " ".join(args[1:])
        # Also try from msg.text for better parsing
        if msg.text and len(msg.text.split(None, 2)) > 2:
            raw = msg.text.split(None, 2)[2]
        if "|" not in raw:
            await reply.reply_text("Usage: /faq add <question> | <answer>")
            return True
        question, answer = raw.split("|", 1)
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            await reply.reply_text("Both question and answer are required.")
            return True
        conn.execute(
            "INSERT INTO faq_entries (company_id, chat_id, question, answer, created_by) VALUES (?, ?, ?, ?, ?)",
            (company_id, chat_id, question, answer, sender_id),
        )
        conn.commit()
        await reply.reply_text(f"Added: *{question}*")

    elif sub == "remove":
        if len(args) < 2 or not args[1].isdigit():
            await reply.reply_text("Usage: /faq remove <id>")
            return True
        deleted = conn.execute(
            "DELETE FROM faq_entries WHERE id = ? AND chat_id = ? AND company_id = ?",
            (int(args[1]), chat_id, company_id),
        ).rowcount
        conn.commit()
        if deleted:
            await reply.reply_text(f"Removed entry #{args[1]}")
        else:
            await reply.reply_text(f"Entry #{args[1]} not found.")

    elif sub == "list":
        rows = conn.execute(
            "SELECT id, question, views FROM faq_entries WHERE chat_id = ? AND company_id = ? ORDER BY views DESC",
            (chat_id, company_id),
        ).fetchall()
        if not rows:
            await reply.reply_text("No FAQ entries. Add with `/faq add <question> | <answer>`")
            return True
        lines = ["*FAQ entries:*\n"]
        for r in rows[:20]:
            lines.append(f"#{r['id']} ({r['views']} views) — {r['question']}")
        if len(rows) > 20:
            lines.append(f"\n...and {len(rows) - 20} more")
        auto = "on" if _faq_is_auto(chat_id) else "off"
        lines.append(f"\nAuto-answer: {auto}")
        await reply.reply_text("\n".join(lines))

    elif sub == "auto":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            await reply.reply_text("Usage: /faq auto on|off")
            return True
        enabled = args[1].lower() == "on"
        _faq_set_auto(chat_id, company_id, enabled)
        await reply.reply_text(f"Auto-answer {'enabled' if enabled else 'disabled'}")

    else:
        # Search FAQ
        query = " ".join(args)
        entry, score = _faq_search(chat_id, company_id, query)
        if entry and score >= FAQ_MIN_SCORE:
            conn.execute("UPDATE faq_entries SET views = views + 1 WHERE id = ?", (entry["id"],))
            conn.commit()
            await reply.reply_text(entry["answer"])
        else:
            await reply.reply_text("No matching FAQ found. Try `/faq list`.")

    return True
