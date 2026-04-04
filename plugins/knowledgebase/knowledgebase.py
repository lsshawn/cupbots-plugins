"""
Knowledgebase — Upload documents and search them with AI.

Commands:
  /knowledgebase upload           — Upload a document (attach file or reply to one)
  /knowledgebase search <query>   — Search your knowledge base
  /knowledgebase ask <question>   — Search + AI-generated answer from results
  /knowledgebase list             — List uploaded documents
  /knowledgebase delete <id>      — Delete a document
  /knowledgebase tag <id> <tags>  — Tag a document (comma-separated)
  /knowledgebase auto on|off      — Auto-answer plain messages from knowledge base

Shortcuts:
  /kb search <query>              — Short alias for /knowledgebase search
  /kb ask <question>              — Short alias for /knowledgebase ask

Upload supports: PDF, DOCX, TXT

Examples:
  /knowledgebase upload                     (attach a file or reply to one)
  /knowledgebase search refund policy
  /knowledgebase ask What is the return window?
  /knowledgebase tag 5 faq,returns,policy
  /knowledgebase search refund --tag faq
  /knowledgebase auto on
"""

import json
import os
import tempfile

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cupbots.helpers.db import get_plugin_db, resolve_plugin_setting
from cupbots.helpers.logger import get_logger

log = get_logger("knowledgebase")

PLUGIN_NAME = "knowledgebase"
COMMANDS = ("knowledgebase", "kb")
AUTO_ANSWER_THRESHOLD = 0.75
AUTO_ANSWER_LIMIT = 3

# Chunker API — resolved from plugin_config table or env vars
CHUNKER_API_URL = os.environ.get("CHUNKER_API_URL", "https://chunker-api.108labs.ai")


def _api_key() -> str:
    return resolve_plugin_setting(PLUGIN_NAME, "CHUNKER_API_KEY") or ""

SUPPORTED_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "text/plain": ".txt",
}


# ---------------------------------------------------------------------------
# Database — track docs locally + config
# ---------------------------------------------------------------------------

def create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kb_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL,
            chunker_doc_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            uploaded_by TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            chunks INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS kb_config (
            chat_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL DEFAULT '',
            auto_answer INTEGER NOT NULL DEFAULT 0
        );
    """)


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Chunker API helpers
# ---------------------------------------------------------------------------

def _headers():
    return {"X-API-Key": _api_key()}


async def _upload_to_chunker(file_bytes: bytes, filename: str,
                             metadata: dict) -> dict:
    """Upload a file to Chunker API (async). Returns {documentId, chunks, error}."""
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(
            f"{CHUNKER_API_URL}/documents/upload",
            headers=_headers(),
            files={"file": (filename, file_bytes)},
            data={"metadata": json.dumps(metadata)},
        )
        r.raise_for_status()
        data = r.json()
        doc = data.get("document", {})
        return {"documentId": doc.get("id"), "status": doc.get("status", "pending")}


async def _check_document(doc_id: int) -> dict:
    """Check document status once. Returns document dict."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CHUNKER_API_URL}/documents/{doc_id}",
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json().get("document", r.json())


async def _search_chunker(query: str, limit: int = 5,
                          filter_meta: dict | None = None,
                          rerank: bool = False) -> list[dict]:
    """Search Chunker API. Returns list of chunk results."""
    body: dict = {
        "query": query,
        "limit": limit,
        "include_metadata": True,
        "score_threshold": 0.5,
    }
    if filter_meta:
        body["filter"] = filter_meta
    if rerank:
        body["rerank"] = True

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{CHUNKER_API_URL}/chunks/query",
            headers=_headers(),
            json=body,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("chunks", data.get("results", []))


async def _delete_from_chunker(doc_id: int) -> bool:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(
            f"{CHUNKER_API_URL}/documents/{doc_id}",
            headers=_headers(),
        )
        return r.status_code in (200, 204, 404)


# ---------------------------------------------------------------------------
# Local DB helpers
# ---------------------------------------------------------------------------

def _save_doc(company_id: str, chat_id: str, chunker_doc_id: int,
              filename: str, uploaded_by: str, status: str = "pending",
              chunks: int = 0) -> int:
    cur = _db().execute(
        "INSERT INTO kb_documents (company_id, chat_id, chunker_doc_id, filename, "
        "uploaded_by, status, chunks) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (company_id, chat_id, chunker_doc_id, filename, uploaded_by, status, chunks),
    )
    _db().commit()
    return cur.lastrowid


def _update_doc_status(local_id: int, status: str, chunks: int = 0):
    _db().execute(
        "UPDATE kb_documents SET status = ?, chunks = ? WHERE id = ?",
        (status, chunks, local_id),
    )
    _db().commit()


def _get_docs(company_id: str, chat_id: str | None = None) -> list:
    if chat_id:
        return _db().execute(
            "SELECT * FROM kb_documents WHERE company_id = ? AND chat_id = ? "
            "ORDER BY created_at DESC",
            (company_id, chat_id),
        ).fetchall()
    return _db().execute(
        "SELECT * FROM kb_documents WHERE company_id = ? ORDER BY created_at DESC",
        (company_id,),
    ).fetchall()


def _get_doc(local_id: int, company_id: str):
    return _db().execute(
        "SELECT * FROM kb_documents WHERE id = ? AND company_id = ?",
        (local_id, company_id),
    ).fetchone()


def _set_tags(local_id: int, company_id: str, tags: str):
    _db().execute(
        "UPDATE kb_documents SET tags = ? WHERE id = ? AND company_id = ?",
        (tags, local_id, company_id),
    )
    _db().commit()


def _delete_doc(local_id: int, company_id: str) -> dict | None:
    row = _get_doc(local_id, company_id)
    if not row:
        return None
    _db().execute("DELETE FROM kb_documents WHERE id = ? AND company_id = ?",
                  (local_id, company_id))
    _db().commit()
    return dict(row)


def _is_auto_answer(chat_id: str) -> bool:
    row = _db().execute(
        "SELECT auto_answer FROM kb_config WHERE chat_id = ?", (chat_id,),
    ).fetchone()
    return bool(row and row["auto_answer"])


def _set_auto_answer(chat_id: str, company_id: str, enabled: bool):
    _db().execute(
        """INSERT INTO kb_config (chat_id, company_id, auto_answer)
           VALUES (?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET auto_answer = excluded.auto_answer""",
        (chat_id, company_id, int(enabled)),
    )
    _db().commit()


# ---------------------------------------------------------------------------
# Parse --tag flag from args
# ---------------------------------------------------------------------------

def _parse_tag_filter(args: list[str]) -> tuple[list[str], dict | None]:
    """Extract --tag value from args. Returns (remaining_args, filter_dict)."""
    clean = []
    tag_filter = None
    i = 0
    while i < len(args):
        if args[i] == "--tag" and i + 1 < len(args):
            tag_filter = {"tag": args[i + 1]}
            i += 2
        else:
            clean.append(args[i])
            i += 1
    return clean, tag_filter


# ---------------------------------------------------------------------------
# Format search results
# ---------------------------------------------------------------------------

def _format_results(results: list[dict], query: str) -> str:
    if not results:
        return f"No results for: {query}"
    lines = [f"Results for: {query}\n"]
    for i, r in enumerate(results, 1):
        score = r.get("score", r.get("similarity", 0))
        filename = r.get("filename", "")
        meta = r.get("metadata", {})
        page = meta.get("pageStart")
        content = r.get("content", r.get("text", ""))[:300]

        loc = filename
        if page:
            loc += f" p.{page}"
        lines.append(f"{i}. [{score:.0%}] {loc}")
        lines.append(f"   {content}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def _handle_upload(file_bytes: bytes, filename: str, mime: str,
                         chat_id: str, company_id: str,
                         sender_id: str) -> str:
    """Upload a document to Chunker and track it locally."""
    if not _api_key():
        return "Knowledgebase not configured. Use /config knowledgebase CHUNKER_API_KEY <key>"

    metadata = {"company_id": company_id, "chat_id": chat_id}

    try:
        result = await _upload_to_chunker(file_bytes, filename, metadata)
    except httpx.HTTPStatusError as e:
        log.error("Chunker upload failed: %s", e)
        return f"Upload failed: {e.response.status_code}"
    except Exception as e:
        log.error("Chunker upload error: %s", e)
        return f"Upload failed: {e}"

    chunker_id = result.get("documentId")
    if not chunker_id:
        return "Upload failed: no document ID returned."

    local_id = _save_doc(company_id, chat_id, chunker_id, filename,
                         sender_id, status="processing")

    return f"Uploaded: {filename} (ID: {local_id}). Processing in background — use /kb list to check status."


async def _handle_search(args: list[str], company_id: str,
                         rerank: bool = False) -> str:
    if not args:
        return "Usage: /knowledgebase search <query>"
    if not _api_key():
        return "Knowledgebase not configured. Use /config knowledgebase CHUNKER_API_KEY <key>"

    clean_args, tag_filter = _parse_tag_filter(args)
    query = " ".join(clean_args)
    if not query:
        return "Usage: /knowledgebase search <query>"

    filter_meta = {"company_id": company_id}
    if tag_filter:
        filter_meta.update(tag_filter)

    try:
        results = await _search_chunker(query, limit=5,
                                        filter_meta=filter_meta,
                                        rerank=rerank)
    except Exception as e:
        log.error("Search failed: %s", e)
        return f"Search failed: {e}"

    return _format_results(results, query)


async def _handle_ask(args: list[str], company_id: str) -> str:
    """Search + generate an AI answer from the top results."""
    if not args:
        return "Usage: /knowledgebase ask <question>"
    if not _api_key():
        return "Knowledgebase not configured. Use /config knowledgebase CHUNKER_API_KEY <key>"

    clean_args, tag_filter = _parse_tag_filter(args)
    question = " ".join(clean_args)
    if not question:
        return "Usage: /knowledgebase ask <question>"

    filter_meta = {"company_id": company_id}
    if tag_filter:
        filter_meta.update(tag_filter)

    try:
        results = await _search_chunker(question, limit=5,
                                        filter_meta=filter_meta, rerank=True)
    except Exception as e:
        log.error("Search failed: %s", e)
        return f"Search failed: {e}"

    if not results:
        return f"No relevant documents found for: {question}"

    # Build context from results
    context_parts = []
    for r in results:
        filename = r.get("filename", "")
        page = r.get("metadata", {}).get("pageStart", "")
        text = r.get("content", r.get("text", ""))
        source = f"[{filename} p.{page}]" if page else f"[{filename}]"
        context_parts.append(f"{source}\n{text}")

    context = "\n\n---\n\n".join(context_parts)

    try:
        from cupbots.helpers.llm import run_claude_cli
        result = await run_claude_cli(
            f"Based on the following documents, answer this question: {question}"
            f"\n\nDocuments:\n{context}",
            model="haiku",
            system_prompt=(
                "You are a helpful assistant answering questions based on the "
                "provided documents. Be concise. Cite sources with [filename p.X] "
                "when possible. If the documents don't contain enough information, "
                "say so."
            ),
            max_turns=1,
            timeout=30,
        )
        answer = result.get("text", "No answer generated.")
    except Exception as e:
        log.error("LLM answer generation failed: %s", e)
        # Fall back to showing raw results
        return _format_results(results, question)

    return answer


async def _handle_list(company_id: str, chat_id: str) -> str:
    docs = _get_docs(company_id, chat_id)
    if not docs:
        return "No documents uploaded yet. Send a file to get started."

    # Refresh status for any still-processing docs
    for d in docs:
        if d["status"] == "processing":
            try:
                remote = await _check_document(d["chunker_doc_id"])
                if remote.get("status") in ("completed", "failed"):
                    chunks = remote.get("chunks", remote.get("chunkCount", 0))
                    _update_doc_status(d["id"], remote["status"], chunks)
            except Exception:
                pass
    # Re-fetch after updates
    docs = _get_docs(company_id, chat_id)

    lines = ["Documents:\n"]
    for d in docs[:20]:
        tags = f" [{d['tags']}]" if d["tags"] else ""
        lines.append(
            f"#{d['id']} {d['filename']} — {d['chunks']} chunks, "
            f"{d['status']}{tags}"
        )
    if len(docs) > 20:
        lines.append(f"\n...and {len(docs) - 20} more")
    auto = "on" if _is_auto_answer(chat_id) else "off"
    lines.append(f"\nAuto-answer: {auto}")
    return "\n".join(lines)


async def _handle_delete(args: list[str], company_id: str) -> str:
    if not args or not args[0].isdigit():
        return "Usage: /knowledgebase delete <id>"
    local_id = int(args[0])
    row = _delete_doc(local_id, company_id)
    if not row:
        return f"Document #{local_id} not found."
    # Best-effort delete from Chunker
    try:
        await _delete_from_chunker(row["chunker_doc_id"])
    except Exception as e:
        log.warning("Chunker delete failed for doc %d: %s", row["chunker_doc_id"], e)
    return f"Deleted: {row['filename']} (#{local_id})"


async def _handle_tag(args: list[str], company_id: str) -> str:
    if len(args) < 2 or not args[0].isdigit():
        return "Usage: /knowledgebase tag <id> <tags>"
    local_id = int(args[0])
    tags = " ".join(args[1:]).replace(" ", "").strip()
    doc = _get_doc(local_id, company_id)
    if not doc:
        return f"Document #{local_id} not found."
    _set_tags(local_id, company_id, tags)
    return f"Tagged #{local_id} ({doc['filename']}): {tags}"


# ---------------------------------------------------------------------------
# Telegram file download helper
# ---------------------------------------------------------------------------

async def _download_tg_file(update: Update, context) -> tuple[bytes | None, str, str]:
    """Download a document from the message or replied message.
    Returns (file_bytes, filename, mime_type) or (None, "", "")."""
    msg = update.message
    sources = [msg]
    if msg.reply_to_message:
        sources.insert(0, msg.reply_to_message)

    for src in sources:
        if src.document:
            mime = src.document.mime_type or ""
            if mime not in SUPPORTED_TYPES:
                continue
            tg_file = await src.document.get_file()
            ba = bytearray()
            await tg_file.download_as_bytearray(ba)
            return bytes(ba), src.document.file_name or "document", mime

    return None, "", ""


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command not in COMMANDS:
        return False

    args = msg.args
    company_id = msg.company_id or ""
    chat_id = msg.chat_id

    if not args:
        await reply.reply_text(__doc__.strip())
        return True

    sub = args[0].lower()

    if sub in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    if sub == "upload":
        # Cross-platform upload: Telegram only for now (WhatsApp needs media forwarding)
        if msg.platform == "telegram" and msg.raw:
            update = msg.raw
            if hasattr(update, "message"):
                file_bytes, filename, mime = await _download_tg_file(update, None)
                if file_bytes:
                    await reply.send_typing()
                    result = await _handle_upload(
                        file_bytes, filename, mime,
                        chat_id, company_id, msg.sender_id,
                    )
                    await reply.reply_text(result)
                    return True
            await reply.reply_text(
                "Attach a document or reply to one, then use /knowledgebase upload"
            )
        else:
            await reply.reply_text(
                "File upload is currently supported on Telegram. "
                "On WhatsApp, this feature is coming soon."
            )
        return True

    if sub == "search":
        await reply.send_typing()
        result = await _handle_search(args[1:], company_id)
        await reply.reply_text(result)
        return True

    if sub == "ask":
        await reply.send_typing()
        result = await _handle_ask(args[1:], company_id)
        await reply.reply_text(result)
        return True

    if sub == "list":
        result = await _handle_list(company_id, chat_id)
        await reply.reply_text(result)
        return True

    if sub == "delete":
        result = await _handle_delete(args[1:], company_id)
        await reply.reply_text(result)
        return True

    if sub == "tag":
        result = await _handle_tag(args[1:], company_id)
        await reply.reply_text(result)
        return True

    if sub == "auto":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            await reply.reply_text("Usage: /knowledgebase auto on|off")
            return True
        enabled = args[1].lower() == "on"
        _set_auto_answer(chat_id, company_id, enabled)
        await reply.reply_text(
            f"Auto-answer {'enabled' if enabled else 'disabled'}"
        )
        return True

    # Unknown subcommand — treat as search
    await reply.send_typing()
    result = await _handle_search(args, company_id)
    await reply.reply_text(result)
    return True


# ---------------------------------------------------------------------------
# handle_message — auto-answer plain text from knowledge base
# ---------------------------------------------------------------------------

async def handle_message(msg, reply):
    if not msg.text or msg.command:
        return False
    if not _is_auto_answer(msg.chat_id):
        return False

    company_id = msg.company_id or ""
    if not _api_key():
        return False

    try:
        results = await _search_chunker(
            msg.text, limit=AUTO_ANSWER_LIMIT,
            filter_meta={"company_id": company_id},
        )
    except Exception:
        return False

    if not results:
        return False

    top = results[0]
    score = top.get("score", top.get("similarity", 0))
    if score < AUTO_ANSWER_THRESHOLD:
        return False

    content = top.get("content", top.get("text", ""))
    filename = top.get("filename", "")
    page = top.get("metadata", {}).get("pageStart")
    source = f"[{filename} p.{page}]" if page else f"[{filename}]"

    await reply.reply_text(f"{content[:1500]}\n\n{source}")
    return True


# ---------------------------------------------------------------------------
# Telegram-specific: handle file uploads with /knowledgebase caption
# ---------------------------------------------------------------------------

async def _tg_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle documents sent with /knowledgebase or /kb caption."""
    msg = update.message
    if not msg:
        return
    caption = (msg.caption or "").strip().lower()
    if not caption.startswith(("/knowledgebase", "/kb")):
        return

    file_bytes, filename, mime = await _download_tg_file(update, context)
    if not file_bytes:
        await msg.reply_text("Unsupported file type. Supported: PDF, DOCX, TXT")
        return

    chat_id = str(update.effective_chat.id)
    sender_id = str(update.effective_user.id) if update.effective_user else ""

    # Get company_id from group config
    from cupbots.helpers.db import get_group_config
    import asyncio
    group_cfg = await get_group_config(chat_id)
    company_id = group_cfg.get("company_id", "") if group_cfg else ""

    await msg.reply_text(f"Uploading {filename}...")
    result = await _handle_upload(file_bytes, filename, mime,
                                  chat_id, company_id, sender_id)
    await msg.reply_text(result)


async def cmd_knowledgebase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    from cupbots.helpers.channel import TelegramReplyContext, IncomingMessage
    from cupbots.helpers.db import get_group_config

    chat_id = str(update.effective_chat.id)
    group_cfg = await get_group_config(chat_id)
    company_id = group_cfg.get("company_id", "") if group_cfg else ""

    msg = IncomingMessage(
        platform="telegram",
        chat_id=chat_id,
        sender_id=str(update.effective_user.id) if update.effective_user else "",
        sender_name=update.effective_user.first_name if update.effective_user else "",
        text=update.message.text or "",
        command="knowledgebase",
        args=context.args or [],
        reply_to_text=(update.message.reply_to_message.text
                       if update.message.reply_to_message else None),
        company_id=company_id,
        group_config=group_cfg,
        raw=update,
    )
    tg_reply = TelegramReplyContext(update)
    await handle_command(msg, tg_reply)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(app: Application):
    app.add_handler(CommandHandler("knowledgebase", cmd_knowledgebase))
    app.add_handler(CommandHandler("kb", cmd_knowledgebase))

    # Catch file uploads with /knowledgebase or /kb caption
    doc_filter = (filters.Document.ALL | filters.PHOTO) & filters.CaptionRegex(
        r"^/(knowledgebase|kb)\b"
    )
    app.add_handler(MessageHandler(doc_filter, _tg_upload_handler), group=40)
