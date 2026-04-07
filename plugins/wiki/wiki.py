"""
Wiki — AI-maintained knowledge base with incremental entity synthesis.

Implements the LLM Wiki pattern: source documents are ingested, entities
(people, companies, projects, events, topics) are extracted and maintained
as interlinked Markdown files. Each organization gets a central wiki at
data/wiki/<company_id>/ with unified context across all source folders.

Commands:
  /wiki                      Show help
  /wiki workspaces           List configured workspaces with stats
  /wiki ingest               Scan all sources, ingest new/changed files
  /wiki ingest <file>        Ingest a specific file
  /wiki search <query>       Semantic search across entities (sqlite-vec)
  /wiki entities             List all entities with types
  /wiki show <entity>        Display entity markdown
  /wiki log [--limit N]      Show recent actions
  /wiki sync                 Re-sync workspaces from config.yaml

All commands accept --workspace <name> to target a specific workspace.
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
# Auto-answer (handle_message hook)
# ---------------------------------------------------------------------------

AUTO_ANSWER_THRESHOLD = 0.75


async def handle_message(msg, reply) -> bool | str | None:
    """Auto-answer from wiki if the group has a wiki workspace configured.

    Runs before AI routing. Returns True if answered (stops AI from running),
    False/None to pass through.
    """
    # Only for non-command plain text in groups with wiki configured
    if msg.command or not msg.text:
        return False

    group_cfg = msg.group_config
    if not group_cfg:
        return False

    wiki_ws = (group_cfg.get("metadata") or {}).get("wiki", "")
    if not wiki_ws:
        return False

    company_id = msg.company_id or ""
    query = msg.text.strip()
    if len(query) < 5:
        return False

    # Search wiki entities scoped to this workspace
    try:
        ws = _get_workspace(company_id, wiki_ws)
        if not ws:
            return False

        results = await _search_entities_in_workspace(company_id, ws["id"], query, limit=3)
        if not results:
            return False

        # Check confidence threshold (for vec search, distance < 0.25 means > 0.75 similarity)
        best = results[0]
        if "distance" in best and best["distance"] > (1 - AUTO_ANSWER_THRESHOLD):
            return False

        # Read the entity file and generate a concise answer
        content = _read_entity_file(company_id, best["slug"])
        if not content:
            return False

        # Use LLM to generate a natural answer from wiki content
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
    if msg.command != "wiki":
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
            # Ingest specific file
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
            # Scan all sources
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
            # Try fuzzy match
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

    else:
        await reply.reply_text(f"Unknown subcommand: {sub}\n\nUse /wiki --help for available commands.")

    return True
