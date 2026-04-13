"""
Report — Generate consultant-grade PDF reports from wiki content or uploaded documents.

Commands:
  /report create --title X --workspace W [--signatory "Managing Director"] [--palette green]
  /report create --title X --file /path/to/source.pdf
  /report create --title X --from-attachment
  /report draft --id <id>           — synthesise sections from source
  /report build --id <id>           — render HTML + PDF, register preview
  /report tweak --id <id> --section SEC --instruction "make it punchier"
  /report palette --id <id> --primary #2E7D32 --accent #C8A951 --dark #37474F
  /report signatory --id <id> --title "Group Managing Director"
  /report status --id <id>
  /report list
  /report preview --id <id>         — re-emit the hosted preview URL
  /report show --id <id>            — dump current markdown in chat
  /report archive --id <id>         — soft-delete (hide from list)
  /report restore --id <id>         — undo archive
  /report delete --id <id>          — permanent delete
  /report list                      — active reports
  /report list --all                — include archived
  /report demo                      — build a pre-made demo report instantly (no AI)

Examples:
  /report create --title "Sustainability Statement FYE 2025" --workspace acmeco
  /report draft --id 1
  /report build --id 1
  /report tweak --id 1 --section environment --instruction "shorten by 30%"
  /report palette --id 1 --primary #1F4E79 --accent #C8A951
"""

from __future__ import annotations

import json
import logging
import shlex
import sqlite3
from pathlib import Path

from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.events import subscribe

log = logging.getLogger(__name__)

PLUGIN_NAME = "report"

# Default palettes (name → CSS var overrides)
PALETTES = {
    "green": {"primary": "#2E7D32", "accent": "#C8A951", "dark": "#37474F"},
    "navy": {"primary": "#1F4E79", "accent": "#C8A951", "dark": "#263238"},
    "earth": {"primary": "#795548", "accent": "#C8A951", "dark": "#3E2723"},
    "teal": {"primary": "#00796B", "accent": "#FFD54F", "dark": "#37474F"},
}

DEFAULT_SECTIONS = [
    "Governance & Board Oversight",
    "Strategy & Business Model",
    "Materiality Assessment",
    "Environmental Performance",
    "Social Impact",
    "Data Ethics & Privacy",
    "Supply Chain Management",
    "Climate Risk & TCFD",
    "Forward-Looking Targets",
    "Appendix & Data Tables",
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def create_tables(conn: sqlite3.Connection):
    """Auto-called by get_plugin_db on first access."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            input_mode TEXT NOT NULL DEFAULT 'wiki',
            workspace TEXT,
            source_path TEXT,
            signatory_title TEXT DEFAULT 'Managing Director',
            palette_json TEXT DEFAULT '{}',
            sections_json TEXT DEFAULT '{}',
            asset_manifest_json TEXT DEFAULT '{}',
            html_path TEXT,
            pdf_path TEXT,
            preview_token TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS report_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            diff_summary TEXT,
            sections_json TEXT,
            html_path TEXT,
            pdf_path TEXT,
            edit_source TEXT NOT NULL DEFAULT 'whatsapp',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(report_id, version)
        );

        CREATE TABLE IF NOT EXISTS report_edit_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'whatsapp',
            source_ref TEXT,
            instruction TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


_db_migrated = False


def _db() -> sqlite3.Connection:
    global _db_migrated
    conn = get_plugin_db(PLUGIN_NAME)
    conn.row_factory = sqlite3.Row
    if not _db_migrated:
        # Additive migration: ensure archived column exists on older DBs
        try:
            conn.execute("ALTER TABLE reports ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        _db_migrated = True
    return conn


# ---------------------------------------------------------------------------
# Flag parsing (reused from calendar pattern)
# ---------------------------------------------------------------------------


def _parse_flags(args: list[str]) -> dict:
    """Parse --flag value pairs from args list. Supports quoted values."""
    try:
        tokens = shlex.split(" ".join(args))
    except ValueError:
        tokens = list(args)

    out: dict = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].lower().replace("-", "_")
            values = []
            i += 1
            while i < len(tokens) and not tokens[i].startswith("--"):
                values.append(tokens[i])
                i += 1
            out[key] = " ".join(values).strip()
        else:
            i += 1
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_report(report_id: int, company_id: str, include_archived: bool = False) -> dict | None:
    """Fetch a report row scoped by company_id."""
    sql = "SELECT * FROM reports WHERE id = ? AND company_id = ?"
    if not include_archived:
        sql += " AND archived = 0"
    row = _db().execute(sql, (report_id, company_id)).fetchone()
    return dict(row) if row else None


def _update_report(report_id: int, company_id: str, **fields):
    """Update report fields."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values())
    _db().execute(
        f"UPDATE reports SET {sets}, updated_at = datetime('now') "
        f"WHERE id = ? AND company_id = ?",
        vals + [report_id, company_id],
    )
    _db().commit()


def _resolve_palette(name_or_hex: str | None) -> dict:
    """Resolve a palette name to CSS vars, or return default."""
    if not name_or_hex:
        return PALETTES["green"]
    if name_or_hex.lower() in PALETTES:
        return PALETTES[name_or_hex.lower()]
    # Treat as a primary colour hex
    if name_or_hex.startswith("#"):
        base = PALETTES["green"].copy()
        base["primary"] = name_or_hex
        return base
    return PALETTES["green"]


def _output_dir(report_id: int) -> Path:
    """Resolve the output directory for a report."""
    custom = resolve_plugin_setting(PLUGIN_NAME, "output_dir")
    base = Path(custom) if custom else Path("data/plugins/report/output")
    out = base / str(report_id)
    out.mkdir(parents=True, exist_ok=True)
    return out


async def _send_long_text(reply, text: str, prefix: str = ""):
    """Send text, splitting at 4000 chars if needed."""
    if prefix:
        text = f"{prefix}\n\n{text}"
    if len(text) <= 4000:
        await reply.reply_text(text)
    else:
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            await reply.reply_text(chunk)


# ---------------------------------------------------------------------------
# Wiki integration
# ---------------------------------------------------------------------------


async def _draft_from_wiki(report: dict, reply) -> dict:
    """Pull content from wiki workspace and synthesise 10 sections."""
    from cupbots.helpers.llm import ask_llm

    workspace = report["workspace"]
    company_id = report["company_id"]

    # Import wiki functions (same plugin tree, private but stable API)
    try:
        from plugins.wiki.wiki import (
            _get_workspaces,
            _read_entity_file,
            _search_entities_in_workspace,
        )
    except ImportError:
        await reply.reply_text("Wiki plugin not available. Install it first or use --file mode.")
        return {}

    workspaces = _get_workspaces(company_id)
    ws = next((w for w in workspaces if w["name"] == workspace), None)
    if not ws:
        await reply.reply_text(
            f"Workspace '{workspace}' not found. "
            f"Available: {', '.join(w['name'] for w in workspaces)}"
        )
        return {}

    ws_id = ws["id"]
    sections = {}

    # Section-specific search queries
    search_queries = {
        "Governance & Board Oversight": "board governance committee oversight directors",
        "Strategy & Business Model": "strategy business model objectives competitive",
        "Materiality Assessment": "materiality stakeholder engagement assessment topics",
        "Environmental Performance": "emissions carbon energy water waste environment",
        "Social Impact": "employees community workforce training safety social",
        "Data Ethics & Privacy": "data privacy ethics digital cybersecurity",
        "Supply Chain Management": "supply chain procurement vendors sustainable sourcing",
        "Climate Risk & TCFD": "climate risk TCFD scenario transition physical",
        "Forward-Looking Targets": "targets goals commitments 2030 2050 future",
        "Appendix & Data Tables": "data performance indicators metrics summary tables",
    }

    await reply.reply_text(f"Drafting {len(DEFAULT_SECTIONS)} sections from workspace '{workspace}'...")

    for section_title in DEFAULT_SECTIONS:
        query = search_queries.get(section_title, section_title)

        # Search for relevant entities
        entities = await _search_entities_in_workspace(company_id, ws_id, query, limit=5)

        # Read entity content
        source_texts = []
        for ent in entities:
            content = _read_entity_file(company_id, ent["slug"])
            if content:
                source_texts.append(f"# {ent['name']}\n{content}")

        if not source_texts:
            sections[section_title] = f'<p class="body-text"><em>No content found for this section.</em></p>'
            continue

        combined = "\n\n---\n\n".join(source_texts)

        # Synthesise section via LLM
        prompt = (
            f"You are writing the '{section_title}' section of a professional sustainability report "
            f"for {report.get('title', 'the company')}.\n\n"
            f"Source material:\n{combined[:8000]}\n\n"
            f"Write the section as HTML using ONLY these CSS classes: body-text, subsection-title, "
            f"sub-subsection-title, data-table, metric-card, card-grid, card, quote-panel, "
            f"bullet-list, callout-box, target-card, target-grid.\n\n"
            f"Rules:\n"
            f"- Copy facts and figures CHARACTER-IDENTICAL from the source. Do not invent data.\n"
            f"- Use <p class=\"body-text\"> for narrative paragraphs.\n"
            f"- Use <div class=\"subsection-title\"> for sub-headings.\n"
            f"- Use <table class=\"data-table\"> for tabular data.\n"
            f"- Do NOT use any CSS class not listed above.\n"
            f"- Output ONLY the HTML body content, no <html> or <style> tags."
        )

        result = await ask_llm(prompt, max_tokens=2048, system="You are a professional report writer.")
        sections[section_title] = result or f'<p class="body-text"><em>Section generation failed.</em></p>'

    return sections


async def _draft_from_file(report: dict, reply) -> dict:
    """Extract text from an uploaded file and synthesise 10 sections."""
    from cupbots.helpers.llm import ask_llm

    source_path = report["source_path"]
    if not source_path:
        await reply.reply_text("No source file path recorded. Use --file or --from-attachment.")
        return {}

    from .engine.extract import extract_text

    try:
        raw_text = extract_text(source_path)
    except (FileNotFoundError, ValueError, ImportError) as e:
        await reply.reply_text(f"Failed to extract text: {e}")
        return {}

    if not raw_text.strip():
        await reply.reply_text("Extracted text is empty.")
        return {}

    await reply.reply_text(
        f"Extracted {len(raw_text):,} characters. "
        f"Splitting into {len(DEFAULT_SECTIONS)} sections..."
    )

    sections = {}

    # Use LLM to split the document into canonical sections
    for section_title in DEFAULT_SECTIONS:
        prompt = (
            f"Extract the content relevant to '{section_title}' from this document.\n\n"
            f"Document (first 8000 chars):\n{raw_text[:8000]}\n\n"
            f"Format as HTML using ONLY these CSS classes: body-text, subsection-title, "
            f"sub-subsection-title, data-table, metric-card, card-grid, card, quote-panel, "
            f"bullet-list, callout-box, target-card, target-grid.\n\n"
            f"Rules:\n"
            f"- Copy facts and figures CHARACTER-IDENTICAL from the source.\n"
            f"- Use <p class=\"body-text\"> for narrative.\n"
            f"- Use <table class=\"data-table\"> for tables.\n"
            f"- Do NOT invent data not in the source.\n"
            f"- If no content matches this section, output: <p class=\"body-text\"><em>No content available.</em></p>\n"
            f"- Output ONLY HTML body content."
        )
        result = await ask_llm(prompt, max_tokens=2048, system="You are a professional report writer.")
        sections[section_title] = result or f'<p class="body-text"><em>Section generation failed.</em></p>'

    return sections


# ---------------------------------------------------------------------------
# Build pipeline
# ---------------------------------------------------------------------------


async def _build_report(report: dict, reply) -> bool:
    """Render HTML + PDF from frozen sections."""
    sections_json = report.get("sections_json", "{}")
    sections = json.loads(sections_json) if sections_json else {}

    if not sections:
        await reply.reply_text("No sections to build. Run `/report draft` first.")
        return False

    palette = json.loads(report.get("palette_json", "{}")) or PALETTES["green"]
    report_id = report["id"]
    company_id = report["company_id"]
    out = _output_dir(report_id)

    await reply.reply_text("Building report...")

    from .engine.pipeline import ReportSpec, SectionSpec, build_report

    spec = ReportSpec(
        title=report["title"],
        company_name=company_id or "Company",
        fiscal_year="",
        sections=[
            SectionSpec(title=title, body_html=html)
            for title, html in sections.items()
        ],
        output_dir=str(out),
        **{k: v for k, v in palette.items() if k in ("primary", "accent", "dark", "secondary", "navy")},
    )

    if report.get("signatory_title"):
        # The signatory title will be used in the template if we extend it
        pass

    try:
        result = await build_report(spec)
    except Exception as e:
        log.exception("Report build failed for id=%s", report_id)
        _update_report(report_id, company_id, status="error")
        await reply.reply_text(f"Build failed: {e}")
        return False

    # Run QC
    from .engine.qc import full_audit

    html_content = result.html_path.read_text(encoding="utf-8")
    source_text = " ".join(sections.values())
    qc = full_audit(html_content, result.pdf_path, source_text=source_text)

    if not qc.passed:
        for err in qc.errors:
            log.warning("QC error: %s", err)
        await reply.reply_text(
            f"QC warnings (build completed anyway):\n" + "\n".join(f"- {e}" for e in qc.errors)
        )

    _update_report(
        report_id,
        company_id,
        status="built",
        html_path=str(result.html_path),
        pdf_path=str(result.pdf_path),
    )

    # Report results
    size_kb = result.pdf_path.stat().st_size / 1024
    pages = result.page_count or "?"

    # Register preview with hub
    preview_url = ""
    try:
        from cupbots.helpers.hub import is_connected, register_report_preview

        if is_connected():
            pdf_bytes = result.pdf_path.read_bytes()
            html_str = result.html_path.read_text(encoding="utf-8")
            token = await register_report_preview(
                report_id=report_id,
                pdf_bytes=pdf_bytes,
                html_str=html_str,
                title=report["title"],
                user_id=report.get("user_id", ""),
                metadata={"company_id": company_id, "pages": pages},
            )
            if token:
                _update_report(report_id, company_id, preview_token=token)
                preview_url = f"https://hub.cupbots.dev/r/{token}"
    except Exception as e:
        log.warning("Failed to register preview: %s", e)

    # Build status message
    lines = [
        f"Report built.",
        f"Pages: {pages}",
        f"PDF: {size_kb:.0f} KB",
    ]
    if preview_url:
        lines.append(f"Preview: {preview_url}")
    await reply.reply_text("\n".join(lines))

    # Send PDF as WhatsApp document attachment
    try:
        await reply.reply_document(
            file_path=str(result.pdf_path),
            file_name=f"{report['title'].replace(' ', '_')}.pdf",
            mimetype="application/pdf",
        )
    except Exception as e:
        log.warning("Failed to send PDF attachment: %s", e)
        await reply.reply_text(f"PDF saved at: {result.pdf_path}")

    return True


# ---------------------------------------------------------------------------
# Tweak flow (agentic)
# ---------------------------------------------------------------------------


async def _handle_tweak(report: dict, flags: dict, reply, msg) -> bool:
    """Edit a section using plain-language instruction via run_agent_loop."""
    section = flags.get("section", "")
    instruction = flags.get("instruction", "")

    if not section or not instruction:
        await reply.reply_text(
            "Usage: /report tweak --id <id> --section <name> --instruction \"...\""
        )
        return True

    sections = json.loads(report.get("sections_json", "{}"))
    # Fuzzy match the section name
    matched = None
    for title in sections:
        if section.lower() in title.lower():
            matched = title
            break

    if not matched:
        await reply.reply_text(
            f"Section '{section}' not found. Available: {', '.join(sections.keys())}"
        )
        return True

    from cupbots.helpers.llm import ask_llm

    current_html = sections[matched]

    prompt = (
        f"Edit the following HTML section according to this instruction: \"{instruction}\"\n\n"
        f"Current HTML:\n{current_html}\n\n"
        f"Rules:\n"
        f"- Output ONLY the modified HTML.\n"
        f"- Preserve all CSS class names exactly as they are.\n"
        f"- Do NOT add classes not already present.\n"
        f"- Make minimal changes to satisfy the instruction."
    )

    result = await ask_llm(prompt, max_tokens=2048, system="You are a report editor.")
    if not result:
        await reply.reply_text("Edit failed — no response from LLM.")
        return True

    # Save new version
    report_id = report["id"]
    company_id = report["company_id"]

    # Get current version number
    row = _db().execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM report_versions WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    next_version = (row["v"] if row else 0) + 1

    sections[matched] = result
    sections_json = json.dumps(sections, ensure_ascii=False)

    _db().execute(
        "INSERT INTO report_versions (report_id, version, diff_summary, sections_json, edit_source) "
        "VALUES (?, ?, ?, ?, ?)",
        (report_id, next_version, instruction[:200], sections_json, "whatsapp"),
    )
    _update_report(report_id, company_id, sections_json=sections_json, status="draft")
    _db().commit()

    await reply.reply_text(
        f"Section '{matched}' updated (v{next_version}). "
        f"Run `/report build --id {report_id}` to regenerate the PDF."
    )
    return True


# ---------------------------------------------------------------------------
# Email edit handler (AgentMail inbound)
# ---------------------------------------------------------------------------


async def _on_report_edit_email(event: str, data: dict):
    """Handle email.received events — look for report edit instructions."""
    import re

    subject = data.get("subject", "")
    body = data.get("body_text", "")

    # Match [report:ID] in subject or body
    match = re.search(r"\[report:(\d+)\]", subject) or re.search(r"\[report:(\d+)\]", body)
    if not match:
        return  # Not a report edit email — ignore

    report_id = int(match.group(1))

    # Strip quoted reply noise (lines starting with > or "On ... wrote:")
    lines = body.split("\n")
    clean_lines = []
    for line in lines:
        if line.strip().startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", line.strip()):
            break
        clean_lines.append(line)
    instruction = "\n".join(clean_lines).strip()

    if not instruction:
        log.info("Report edit email for id=%d but no instruction found", report_id)
        return

    # Queue the edit
    _db().execute(
        "INSERT INTO report_edit_queue (report_id, source, source_ref, instruction) "
        "VALUES (?, 'email', ?, ?)",
        (report_id, data.get("message_id", ""), instruction),
    )
    _db().commit()

    log.info("Queued email edit for report %d: %s", report_id, instruction[:100])

    # Enqueue a background job — the poll loop picks it up within ~10s
    from datetime import datetime
    from cupbots.helpers.jobs import enqueue

    enqueue("report_email_edit", {
        "report_id": report_id,
        "instruction": instruction,
        "sender_email": data.get("sender", ""),
        "message_id": data.get("message_id", ""),
        "subject": subject,
    }, run_at=datetime.utcnow())


async def _handle_email_edit_job(payload: dict, bot=None):
    """Background job: apply an email edit instruction to a report, then reply via AgentMail."""
    from cupbots.helpers.llm import ask_llm

    report_id = payload["report_id"]
    instruction = payload["instruction"]
    sender_email = payload.get("sender_email", "")

    # Find the report (use empty company_id to search broadly — email edits may not carry company_id)
    row = _db().execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        log.warning("Email edit job: report %d not found", report_id)
        return

    report = dict(row)
    company_id = report["company_id"]
    sections = json.loads(report.get("sections_json", "{}"))

    if not sections:
        log.warning("Email edit job: report %d has no sections", report_id)
        return

    # Try to match instruction to a section, or apply globally
    # Simple heuristic: if instruction mentions a section name, target that section
    matched_section = None
    for title in sections:
        if title.lower().split("&")[0].strip() in instruction.lower():
            matched_section = title
            break

    if matched_section:
        # Edit specific section
        current_html = sections[matched_section]
        prompt = (
            f"Edit the following HTML section according to this instruction: \"{instruction}\"\n\n"
            f"Current HTML:\n{current_html}\n\n"
            f"Rules:\n"
            f"- Output ONLY the modified HTML.\n"
            f"- Preserve all CSS class names exactly.\n"
            f"- Make minimal changes to satisfy the instruction."
        )
        result = await ask_llm(prompt, max_tokens=2048, system="You are a report editor.")
        if result:
            sections[matched_section] = result
            diff_summary = f"Edited '{matched_section}': {instruction[:100]}"
        else:
            diff_summary = f"Failed to edit '{matched_section}'"
    else:
        # Apply instruction globally — let the LLM decide what to change
        # (only send section titles + first 200 chars each to stay compact)
        section_summaries = "\n".join(
            f"- {title}: {html[:200]}..." for title, html in sections.items()
        )
        prompt = (
            f"A user sent this edit instruction for a report: \"{instruction}\"\n\n"
            f"The report has these sections:\n{section_summaries}\n\n"
            f"Which section should be edited? Reply with ONLY the section title, nothing else."
        )
        target = await ask_llm(prompt, max_tokens=100, system="You are a report editor.")
        if target:
            target = target.strip().strip('"')
            for title in sections:
                if target.lower() in title.lower():
                    matched_section = title
                    break

        if matched_section:
            current_html = sections[matched_section]
            edit_prompt = (
                f"Edit the following HTML section according to this instruction: \"{instruction}\"\n\n"
                f"Current HTML:\n{current_html}\n\n"
                f"Rules:\n- Output ONLY the modified HTML.\n- Preserve all CSS class names.\n- Minimal changes."
            )
            result = await ask_llm(edit_prompt, max_tokens=2048, system="You are a report editor.")
            if result:
                sections[matched_section] = result
            diff_summary = f"Edited '{matched_section}': {instruction[:100]}"
        else:
            diff_summary = f"Could not determine which section to edit: {instruction[:100]}"

    # Save new version
    ver_row = _db().execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM report_versions WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    next_version = (ver_row["v"] if ver_row else 0) + 1

    sections_json = json.dumps(sections, ensure_ascii=False)
    _db().execute(
        "INSERT INTO report_versions (report_id, version, diff_summary, sections_json, edit_source) "
        "VALUES (?, ?, ?, ?, 'email')",
        (report_id, next_version, diff_summary, sections_json),
    )
    _update_report(report_id, company_id, sections_json=sections_json, status="draft")
    _db().commit()

    # Update the edit queue entry
    _db().execute(
        "UPDATE report_edit_queue SET status = 'applied', result = ? "
        "WHERE report_id = ? AND source = 'email' AND status = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (diff_summary, report_id),
    )
    _db().commit()

    # Auto-rebuild
    from .engine.pipeline import ReportSpec, SectionSpec, build_report

    palette = json.loads(report.get("palette_json", "{}")) or PALETTES["green"]
    out = _output_dir(report_id)

    spec = ReportSpec(
        title=report["title"],
        company_name=company_id or "Company",
        sections=[SectionSpec(title=t, body_html=h) for t, h in sections.items()],
        output_dir=str(out),
        **{k: v for k, v in palette.items() if k in ("primary", "accent", "dark", "secondary", "navy")},
    )

    try:
        build_result = await build_report(spec)
        _update_report(
            report_id, company_id,
            status="built",
            html_path=str(build_result.html_path),
            pdf_path=str(build_result.pdf_path),
        )

        # Register updated preview with hub
        preview_url = ""
        try:
            from cupbots.helpers.hub import is_connected, register_report_preview
            if is_connected():
                pdf_bytes = build_result.pdf_path.read_bytes()
                html_str = build_result.html_path.read_text(encoding="utf-8")
                token = await register_report_preview(
                    report_id=report_id,
                    pdf_bytes=pdf_bytes,
                    html_str=html_str,
                    title=report["title"],
                )
                if token:
                    _update_report(report_id, company_id, preview_token=token)
                    preview_url = f"https://hub.cupbots.dev/r/{token}"
        except Exception as e:
            log.warning("Failed to register preview after email edit: %s", e)

        # Reply via AgentMail
        if sender_email:
            await _send_agentmail_reply(
                to=sender_email,
                subject=f"Re: {payload.get('subject', f'Report #{report_id}')}",
                body=(
                    f"Your edit has been applied to Report #{report_id} (v{next_version}).\n\n"
                    f"Change: {diff_summary}\n\n"
                    + (f"Preview: {preview_url}\n\n" if preview_url else "")
                    + "Reply to this email with more changes, or open the preview link to review."
                ),
                in_reply_to=payload.get("message_id", ""),
            )

    except Exception as e:
        log.exception("Email edit rebuild failed for report %d: %s", report_id, e)
        _update_report(report_id, company_id, status="error")


async def _send_agentmail_reply(to: str, subject: str, body: str, in_reply_to: str = ""):
    """Send a reply email via AgentMail (using mailwatch's config for the inbox)."""
    try:
        import httpx
        from cupbots.helpers.db import resolve_plugin_setting

        api_key = resolve_plugin_setting("mailwatch", "agentmail_api_key")
        inbox_id = resolve_plugin_setting("mailwatch", "agentmail_address")
        reply_to_addr = resolve_plugin_setting("mailwatch", "agentmail_reply_to") or ""

        if not api_key or not inbox_id:
            log.warning("AgentMail not configured in mailwatch settings — cannot reply to email edit")
            return

        headers_payload = {}
        if in_reply_to:
            headers_payload["In-Reply-To"] = in_reply_to
            headers_payload["References"] = in_reply_to

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.agentmail.to/v0/inboxes/{inbox_id}/messages",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "to": [to] if isinstance(to, str) else to,
                    "subject": subject,
                    "text": body,
                    **({"reply_to": reply_to_addr} if reply_to_addr else {}),
                    **({"headers": headers_payload} if headers_payload else {}),
                },
                timeout=15,
            )
            if resp.status_code >= 400:
                log.warning("AgentMail send failed (%d): %s", resp.status_code, resp.text[:200])
            else:
                log.info("AgentMail reply sent to %s for report edit", to)

    except Exception as e:
        log.warning("Failed to send AgentMail reply: %s", e)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

DEMO_REPORT_TITLE = "__demo__"


async def _handle_demo(msg, reply, company_id: str) -> bool:
    """Build and serve a demo report — no LLM, no wiki, instant."""
    from .engine.demo import build_demo_spec
    from .engine.pipeline import build_report

    await reply.reply_text("Building demo report...")

    # Check if a demo already exists for this company
    row = _db().execute(
        "SELECT id FROM reports WHERE company_id = ? AND title = ? AND archived = 0",
        (company_id, DEMO_REPORT_TITLE),
    ).fetchone()

    if row:
        demo_id = row["id"]
    else:
        # Create the demo report record
        _db().execute(
            "INSERT INTO reports (company_id, user_id, title, input_mode, status) "
            "VALUES (?, ?, ?, 'demo', 'building')",
            (company_id, msg.sender_id, DEMO_REPORT_TITLE),
        )
        _db().commit()
        new_row = _db().execute("SELECT last_insert_rowid() as id").fetchone()
        demo_id = new_row["id"]

    out = _output_dir(demo_id)
    spec = build_demo_spec(output_dir=str(out))

    try:
        result = await build_report(spec)
    except Exception as e:
        log.exception("Demo build failed")
        await reply.reply_text(f"Demo build failed: {e}")
        return True

    # Store sections so they can be edited via /report tweak
    sections_json = json.dumps(
        {s.title: s.body_html for s in spec.sections}, ensure_ascii=False
    )
    _update_report(
        demo_id, company_id,
        status="built",
        sections_json=sections_json,
        html_path=str(result.html_path),
        pdf_path=str(result.pdf_path),
        palette_json=json.dumps({
            "primary": spec.primary,
            "accent": spec.accent,
            "dark": spec.dark,
        }),
    )

    # Register preview with hub
    preview_url = ""
    try:
        from cupbots.helpers.hub import is_connected, register_report_preview
        if is_connected():
            pdf_bytes = result.pdf_path.read_bytes()
            html_str = result.html_path.read_text(encoding="utf-8")
            token = await register_report_preview(
                report_id=demo_id,
                pdf_bytes=pdf_bytes,
                html_str=html_str,
                title="Meridian Group — Sustainability Statement FYE 2025 (Demo)",
            )
            if token:
                _update_report(demo_id, company_id, preview_token=token)
                preview_url = f"https://hub.cupbots.dev/r/{token}"
    except Exception as e:
        log.warning("Failed to register demo preview: %s", e)

    pages = result.page_count or "?"
    size_kb = result.pdf_path.stat().st_size / 1024

    lines = [
        f"*Demo Report Built* (#{demo_id})",
        f"Pages: {pages} | PDF: {size_kb:.0f} KB",
        "",
        "This is a pre-built demo — no AI, no wiki. Try editing it:",
        f"  `/report tweak --id {demo_id} --section environment --instruction \"shorten by 50%\"`",
        f"  `/report palette --id {demo_id} --primary #1F4E79`",
        f"  `/report build --id {demo_id}` to regenerate",
    ]
    if preview_url:
        lines.insert(2, f"Preview: {preview_url}")
        lines.append(f"\nOr open the preview and click 'Request Edit'.")

    await reply.reply_text("\n".join(lines))

    # Send PDF
    try:
        await reply.reply_document(
            file_path=str(result.pdf_path),
            file_name="Meridian_Group_Sustainability_FYE2025_Demo.pdf",
            mimetype="application/pdf",
        )
    except Exception as e:
        log.warning("Failed to send demo PDF: %s", e)

    return True


async def _reset_demo(company_id: str = ""):
    """Reset the demo report back to original content. Called by scheduler."""
    row = _db().execute(
        "SELECT id FROM reports WHERE company_id = ? AND title = ? AND archived = 0",
        (company_id, DEMO_REPORT_TITLE),
    ).fetchone()
    if not row:
        return

    from .engine.demo import build_demo_spec
    from .engine.pipeline import build_report

    demo_id = row["id"]
    out = _output_dir(demo_id)
    spec = build_demo_spec(output_dir=str(out))

    sections_json = json.dumps(
        {s.title: s.body_html for s in spec.sections}, ensure_ascii=False
    )
    _update_report(
        demo_id, company_id,
        status="draft",
        sections_json=sections_json,
        palette_json=json.dumps({
            "primary": spec.primary,
            "accent": spec.accent,
            "dark": spec.dark,
        }),
    )

    try:
        result = await build_report(spec)
        _update_report(
            demo_id, company_id,
            status="built",
            html_path=str(result.html_path),
            pdf_path=str(result.pdf_path),
        )

        # Re-register preview
        from cupbots.helpers.hub import is_connected, register_report_preview
        if is_connected():
            token = await register_report_preview(
                report_id=demo_id,
                pdf_bytes=result.pdf_path.read_bytes(),
                html_str=result.html_path.read_text(encoding="utf-8"),
                title="Meridian Group — Sustainability Statement FYE 2025 (Demo)",
            )
            if token:
                _update_report(demo_id, company_id, preview_token=token)
    except Exception as e:
        log.warning("Demo reset failed: %s", e)


# Subscribe to email events at module load
subscribe("email.received", _on_report_edit_email, plugin_name=PLUGIN_NAME)

# Register background job handler for email edits
try:
    from cupbots.helpers.jobs import register_handler
    register_handler("report_email_edit", _handle_email_edit_job)
except ImportError:
    pass  # Jobs module may not be available in test environments


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


async def handle_command(msg, reply) -> bool:
    """Route /report subcommands."""
    if msg.command != "report":
        return False

    # Help
    if not msg.args or (msg.args[0] in ("--help", "-h", "help")):
        await reply.reply_text(__doc__.strip())
        return True

    action = msg.args[0].lower()
    company_id = msg.company_id or ""

    # --- /report demo ---
    if action == "demo":
        return await _handle_demo(msg, reply, company_id)

    # --- /report list ---
    if action == "list":
        flags = _parse_flags(msg.args[1:])
        include_archived = "all" in flags or "archived" in flags
        sql = "SELECT id, title, status, archived, created_at FROM reports WHERE company_id = ?"
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY id DESC LIMIT 20"
        rows = _db().execute(sql, (company_id,)).fetchall()
        if not rows:
            await reply.reply_text("No reports yet. Create one with `/report create --title ...`")
            return True
        lines = []
        for r in rows:
            tag = " [archived]" if r["archived"] else ""
            lines.append(f"*{r['id']}* | {r['status']}{tag} | {r['title']} | {r['created_at']}")
        await reply.reply_text("Reports:\n" + "\n".join(lines))
        return True

    # --- /report create ---
    if action == "create":
        flags = _parse_flags(msg.args[1:])
        title = flags.get("title", "")
        if not title:
            await reply.reply_text("Missing --title. Usage: /report create --title \"My Report\" --workspace W")
            return True

        workspace = flags.get("workspace")
        file_path = flags.get("file")
        from_attachment = "from_attachment" in flags or "from-attachment" in flags

        # Determine input mode
        if from_attachment:
            media_path = getattr(msg, "media_path", None)
            if not media_path:
                await reply.reply_text("No document attachment found on this message.")
                return True
            input_mode = "attachment"
            source_path = media_path
        elif file_path:
            if not Path(file_path).exists():
                await reply.reply_text(f"File not found: {file_path}")
                return True
            input_mode = "file"
            source_path = file_path
        elif workspace:
            input_mode = "wiki"
            source_path = None
        else:
            await reply.reply_text(
                "Specify a source: --workspace W, --file /path, or --from-attachment"
            )
            return True

        palette = _resolve_palette(flags.get("palette"))
        signatory = flags.get("signatory", "Managing Director")

        _db().execute(
            "INSERT INTO reports (company_id, user_id, title, input_mode, workspace, "
            "source_path, signatory_title, palette_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                company_id,
                msg.sender_id,
                title,
                input_mode,
                workspace,
                source_path,
                signatory,
                json.dumps(palette),
            ),
        )
        _db().commit()

        row = _db().execute("SELECT last_insert_rowid() as id").fetchone()
        rid = row["id"]

        await reply.reply_text(
            f"Report #{rid} created: \"{title}\" ({input_mode} mode).\n"
            f"Next: `/report draft --id {rid}`"
        )
        return True

    # --- Commands that need --id ---
    flags = _parse_flags(msg.args[1:])
    report_id_str = flags.get("id", "")

    if not report_id_str:
        # Try bare numeric arg: /report status 3
        if len(msg.args) > 1 and msg.args[1].isdigit():
            report_id_str = msg.args[1]
        else:
            await reply.reply_text(f"Missing --id. Usage: /report {action} --id <id>")
            return True

    try:
        report_id = int(report_id_str)
    except ValueError:
        await reply.reply_text(f"Invalid report ID: {report_id_str}")
        return True

    report = _get_report(report_id, company_id)
    if not report:
        await reply.reply_text(f"Report #{report_id} not found.")
        return True

    # --- /report status ---
    if action == "status":
        palette = json.loads(report.get("palette_json", "{}"))
        sections = json.loads(report.get("sections_json", "{}"))
        await reply.reply_text(
            f"*Report #{report_id}*\n"
            f"Title: {report['title']}\n"
            f"Status: {report['status']}\n"
            f"Mode: {report['input_mode']}\n"
            f"Sections: {len(sections)}\n"
            f"Signatory: {report['signatory_title']}\n"
            f"Palette: {palette.get('primary', '?')}\n"
            f"Created: {report['created_at']}\n"
            f"Updated: {report['updated_at']}"
        )
        return True

    # --- /report show ---
    if action == "show":
        sections = json.loads(report.get("sections_json", "{}"))
        if not sections:
            await reply.reply_text("No sections yet. Run `/report draft` first.")
            return True
        text = ""
        for title, html in sections.items():
            text += f"\n## {title}\n{html[:500]}...\n"
        await _send_long_text(reply, text.strip(), prefix=f"Report #{report_id} sections:")
        return True

    # --- /report draft ---
    if action == "draft":
        _update_report(report_id, company_id, status="drafting")
        await reply.reply_text(f"Drafting report #{report_id}...")

        if report["input_mode"] == "wiki":
            sections = await _draft_from_wiki(report, reply)
        else:
            sections = await _draft_from_file(report, reply)

        if sections:
            _update_report(
                report_id,
                company_id,
                sections_json=json.dumps(sections, ensure_ascii=False),
                status="draft",
            )
            await reply.reply_text(
                f"Draft complete — {len(sections)} sections.\n"
                f"Next: `/report build --id {report_id}`"
            )
        else:
            _update_report(report_id, company_id, status="error")

        return True

    # --- /report build ---
    if action == "build":
        await _build_report(report, reply)
        return True

    # --- /report tweak ---
    if action == "tweak":
        return await _handle_tweak(report, flags, reply, msg)

    # --- /report palette ---
    if action == "palette":
        new_palette = {}
        for key in ("primary", "accent", "dark", "secondary", "navy"):
            if key in flags:
                new_palette[key] = flags[key]
        if not new_palette:
            await reply.reply_text(
                "Usage: /report palette --id <id> --primary #hex [--accent #hex] [--dark #hex]"
            )
            return True

        current = json.loads(report.get("palette_json", "{}"))
        current.update(new_palette)
        _update_report(report_id, company_id, palette_json=json.dumps(current))
        await reply.reply_text(f"Palette updated. Rebuild with `/report build --id {report_id}`")
        return True

    # --- /report signatory ---
    if action == "signatory":
        title = flags.get("title", "")
        if not title:
            await reply.reply_text("Usage: /report signatory --id <id> --title \"CEO\"")
            return True
        _update_report(report_id, company_id, signatory_title=title)
        await reply.reply_text(f"Signatory updated to '{title}'.")
        return True

    # --- /report preview ---
    if action == "preview":
        token = report.get("preview_token")
        if token:
            await reply.reply_text(f"Preview: https://hub.cupbots.dev/r/{token}")
        else:
            await reply.reply_text(
                "No preview available. Build the report first with "
                f"`/report build --id {report_id}`"
            )
        return True

    # --- /report archive ---
    if action == "archive":
        if report.get("archived"):
            await reply.reply_text(f"Report #{report_id} is already archived.")
            return True
        _update_report(report_id, company_id, archived=1, status="archived")
        await reply.reply_text(f"Report #{report_id} archived. Use `/report restore --id {report_id}` to undo.")
        return True

    # --- /report restore ---
    if action in ("restore", "unarchive"):
        report = _get_report(report_id, company_id, include_archived=True)
        if not report:
            await reply.reply_text(f"Report #{report_id} not found.")
            return True
        if not report.get("archived"):
            await reply.reply_text(f"Report #{report_id} is not archived.")
            return True
        _update_report(report_id, company_id, archived=0, status="draft")
        await reply.reply_text(f"Report #{report_id} restored.")
        return True

    # --- /report delete ---
    if action == "delete":
        # Hard delete — removes the report and all versions
        _db().execute("DELETE FROM report_versions WHERE report_id = ?", (report_id,))
        _db().execute("DELETE FROM report_edit_queue WHERE report_id = ?", (report_id,))
        _db().execute(
            "DELETE FROM reports WHERE id = ? AND company_id = ?",
            (report_id, company_id),
        )
        _db().commit()
        await reply.reply_text(f"Report #{report_id} permanently deleted.")
        return True

    # Unknown subcommand — return False so the router can try the orchestrator
    return False
