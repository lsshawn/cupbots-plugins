"""
FAQ — Trainable Q&A bot for groups.

Commands:
  /faq <query>              — Search FAQ for an answer
  /faq add <question> | <answer> — Add a Q&A entry
  /faq remove <id>          — Remove an entry
  /faq list                 — List all entries
  /faq auto on|off          — Toggle auto-answer on plain messages

Examples:
  /faq add What is CupBots? | A multi-platform bot framework
  /faq add How do I install? | Run /plugin install <name>
  /faq What is CupBots
  /faq list
  /faq auto on
"""

import re

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.db import get_plugin_db
from cupbots.helpers.logger import get_logger

log = get_logger("faq")

PLUGIN_NAME = "faq"
MIN_SCORE = 0.3


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_tables(conn):
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


def _db():
    return get_plugin_db(PLUGIN_NAME)


# ---------------------------------------------------------------------------
# Fuzzy search
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    return set(re.findall(r'\w+', text.lower()))


def _score(query_tokens: set[str], question_tokens: set[str]) -> float:
    if not query_tokens or not question_tokens:
        return 0.0
    return len(query_tokens & question_tokens) / max(len(query_tokens), len(question_tokens))


def _search(chat_id: str, company_id: str, query: str) -> tuple[dict | None, float]:
    rows = _db().execute(
        "SELECT id, question, answer FROM faq_entries WHERE chat_id = ? AND company_id = ?",
        (chat_id, company_id),
    ).fetchall()
    if not rows:
        return None, 0.0
    query_tokens = _tokenize(query)
    best, best_score = None, 0.0
    for row in rows:
        s = _score(query_tokens, _tokenize(row["question"]))
        if s > best_score:
            best, best_score = row, s
    return best, best_score


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _is_auto_answer(chat_id: str) -> bool:
    row = _db().execute(
        "SELECT auto_answer FROM faq_config WHERE chat_id = ?", (chat_id,),
    ).fetchone()
    return bool(row and row["auto_answer"])


def _set_auto_answer(chat_id: str, company_id: str, enabled: bool):
    _db().execute(
        """INSERT INTO faq_config (chat_id, company_id, auto_answer)
           VALUES (?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET auto_answer = excluded.auto_answer""",
        (chat_id, company_id, int(enabled)),
    )
    _db().commit()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

async def _handle_faq(args: list[str], chat_id: str, company_id: str,
                      sender_id: str = "", raw_text: str = "") -> str:
    if not args:
        return __doc__.strip()

    sub = args[0].lower()

    if sub in ("--help", "-h", "help"):
        return __doc__.strip()

    if sub == "add":
        after_add = raw_text.split(None, 2)[2] if raw_text and len(raw_text.split(None, 2)) > 2 else " ".join(args[1:])
        if "|" not in after_add:
            return "Usage: `/faq add <question> | <answer>`"
        question, answer = after_add.split("|", 1)
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            return "Both question and answer are required."
        _db().execute(
            "INSERT INTO faq_entries (company_id, chat_id, question, answer, created_by) VALUES (?, ?, ?, ?, ?)",
            (company_id, chat_id, question, answer, sender_id),
        )
        _db().commit()
        return f"✅ Added: *{question}*"

    if sub == "remove":
        if len(args) < 2 or not args[1].isdigit():
            return "Usage: `/faq remove <id>`"
        deleted = _db().execute(
            "DELETE FROM faq_entries WHERE id = ? AND chat_id = ? AND company_id = ?",
            (int(args[1]), chat_id, company_id),
        ).rowcount
        _db().commit()
        return f"✅ Removed entry #{args[1]}" if deleted else f"Entry #{args[1]} not found."

    if sub == "list":
        rows = _db().execute(
            "SELECT id, question, views FROM faq_entries WHERE chat_id = ? AND company_id = ? ORDER BY views DESC",
            (chat_id, company_id),
        ).fetchall()
        if not rows:
            return "No FAQ entries yet. Add one with `/faq add <question> | <answer>`"
        lines = ["*FAQ entries:*\n"]
        for r in rows[:20]:
            lines.append(f"#{r['id']} ({r['views']} views) — {r['question']}")
        if len(rows) > 20:
            lines.append(f"\n...and {len(rows) - 20} more")
        auto = "on" if _is_auto_answer(chat_id) else "off"
        lines.append(f"\nAuto-answer: {auto}")
        return "\n".join(lines)

    if sub == "auto":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            return "Usage: `/faq auto on|off`"
        enabled = args[1].lower() == "on"
        _set_auto_answer(chat_id, company_id, enabled)
        return f"✅ Auto-answer {'enabled' if enabled else 'disabled'}"

    # Search query
    query = " ".join(args)
    entry, score = _search(chat_id, company_id, query)
    if entry and score >= MIN_SCORE:
        _db().execute("UPDATE faq_entries SET views = views + 1 WHERE id = ?", (entry["id"],))
        _db().commit()
        return entry["answer"]
    return "No matching FAQ found. Try `/faq list` to see all entries."


# ---------------------------------------------------------------------------
# handle_message — auto-answer plain text in groups
# ---------------------------------------------------------------------------

async def handle_message(msg, reply):
    if not msg.text or msg.command:
        return False
    if not _is_auto_answer(msg.chat_id):
        return False
    entry, score = _search(msg.chat_id, msg.company_id or "", msg.text)
    if entry and score >= MIN_SCORE:
        _db().execute("UPDATE faq_entries SET views = views + 1 WHERE id = ?", (entry["id"],))
        _db().commit()
        await reply.reply_text(entry["answer"])
        return True
    return False


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "faq":
        return False
    result = await _handle_faq(
        msg.args, msg.chat_id, msg.company_id or "",
        sender_id=msg.sender_id, raw_text=msg.text or "",
    )
    await reply.reply_text(result)
    return True


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

async def cmd_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = str(update.effective_chat.id)
    args = context.args or []
    from cupbots.helpers.channel import TelegramReplyContext
    reply = TelegramReplyContext(update)
    result = await _handle_faq(
        args, chat_id, "",
        sender_id=str(update.effective_user.id) if update.effective_user else "",
        raw_text=update.message.text or "",
    )
    await reply.reply_text(result)


def register(app: Application):
    app.add_handler(CommandHandler("faq", cmd_faq))
