"""
Claude AI

Commands (works in any topic and DMs):
  /new      — Start a fresh Claude session
  /haiku    — Switch to Haiku (fast, cheap)
  /sonnet   — Switch to Sonnet (balanced)
  /opus     — Switch to Opus (most capable)
  /roast <idea or plan> — Devil's advocate / indie hacker coach roast
  /image <prompt>       — Generate image (flash)
  /image pro <prompt>   — Generate image (pro, higher quality)

Reply to any message to resume context. Replies to /yt analyses load the
full transcript so Claude can answer with complete context.
"""

import asyncio
import os
import tempfile
import time as _time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from cupbots.config import get_config, get_thread_id, get_scripts_dir, get_data_dir
from cupbots.helpers.logger import get_logger
from cupbots.helpers.llm import run_claude_cli, add_history, get_history_context

log = get_logger("claude")

CONTEXT_DIR = get_data_dir() / "analysis-context"
PLUGINS_DIR = Path(__file__).resolve().parent


def _build_command_list() -> str:
    """Build a command reference from all plugin docstrings."""
    commands = []
    for plugin_file in sorted(PLUGINS_DIR.glob("*.py")):
        if plugin_file.name.startswith("_") or plugin_file.name == "claude_chat.py":
            continue
        try:
            source = plugin_file.read_text()
            # Extract module docstring
            if source.startswith('"""'):
                doc = source.split('"""')[1].strip()
            elif source.startswith("'"):
                doc = source.split("'''")[1].strip()
            else:
                continue
            cmds = [l.strip() for l in doc.splitlines()
                    if l.strip().startswith("/") and "—" in l]
            if cmds:
                commands.extend(cmds)
        except Exception:
            continue
    return "\n".join(commands)

from cupbots.helpers.llm import chat_sessions

# Per-user model override: user_id -> model name
_user_models: dict[int, str] = {}

# Protects _user_models from concurrent access
_state_lock = asyncio.Lock()

VALID_MODELS = {"haiku", "sonnet", "opus"}


def _topic_context(thread_id: int | None) -> str:
    """Return a topic-aware system prompt addition based on which thread the user is in."""
    threads = get_config().get("threads", {})
    data_dir = get_data_dir()

    if thread_id == threads.get("baby"):
        db_path = data_dir / "baby.db"
        return (
            "You are in the Baby Tracker topic. "
            f"The baby tracking SQLite database is at {db_path} — "
            "you can query it directly for analysis. "
            "Be helpful about feeding, sleep, and diaper patterns."
        )
    elif thread_id == threads.get("devops"):
        return (
            "You are in the DevOps topic. "
            "Help with pipeline runs, server management, deployments, and scripting. "
            "The project scripts are in the scripts/ directory."
        )
    elif thread_id == threads.get("finance"):
        # Load chart of accounts from journal summaries for AI context
        finance_root = Path("/home/ss/projects/note/finances")
        coa_sections = []
        for ledger_name in ("personal", "cupbots"):
            summary = finance_root / ledger_name / "journal_summary.beancount"
            if summary.exists():
                try:
                    lines = summary.read_text().splitlines()
                    accounts = [l.split("open ")[-1].split()[0] for l in lines if " open " in l]
                    if accounts:
                        coa_sections.append(f"{ledger_name}: {', '.join(accounts)}")
                except Exception:
                    pass
        coa_hint = ""
        if coa_sections:
            coa_hint = "\n\nChart of accounts:\n" + "\n".join(coa_sections)

        return (
            "You are in the Finance topic. "
            "The user has a full finance system powered by Beancount double-entry accounting. "
            "Ledgers: personal at /home/ss/projects/note/finances/personal/journal.beancount, "
            "CupBots (business) at /home/ss/projects/note/finances/cupbots/journal.beancount. "
            "AI context summaries: finances/personal/journal_summary.beancount, "
            "finances/cupbots/journal_summary.beancount. "
            "FX rates: finances/cupbots/fx.beancount. "
            "Available commands: /expense, /income, /transfer, /invoice, /payment, "
            "/finance (scan invoices), /reconcile, /trial, /validate, /fbal, /fsearch, "
            "/fquery, /pnl, /bs, /cashflow, /fxgain, /receivables, /payables, /annualreport, "
            "/networth, /savings, /portfolio, /fire, /void, /edit, /summary, /fxsync, /duplicates. "
            "When the user asks about accounts or categories, check the chart of accounts below. "
            "Help with bookkeeping, financial queries, and reporting."
            + coa_hint
        )
    else:
        return (
            "You are in a general conversation topic. Help with whatever the user needs. "
            "Note: the user has a finance system (Beancount) with commands like /expense, /income, "
            "/pnl, /bs, /networth etc. If they ask about finances, guide them to use these commands "
            "or offer to query the ledger directly."
        )


def _context_bar(input_tokens: int, model: str = "") -> str:
    """Build a minimal context usage indicator."""
    cfg = get_config().get("claude", {})
    window = cfg.get("context_window", 200000)
    if input_tokens <= 0:
        return f"\n\n· {model}" if model else ""
    pct = (input_tokens / window) * 100
    return f"\n\n· {model} · {pct:.0f}% context"


def _build_system_prompt(thread_id: int | None = None) -> str:
    """Build a lightweight system prompt — details live in CLAUDE.md (auto-read by CLI)."""
    topic_hint = _topic_context(thread_id)

    # Load communication style from memory file
    style_file = Path.home() / ".claude" / "projects" / "-home-ss-projects-note" / "memory" / "user_communication_style.md"
    style_prompt = ""
    try:
        raw = style_file.read_text()
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            style_prompt = parts[2].strip() if len(parts) > 2 else ""
        else:
            style_prompt = raw.strip()
    except FileNotFoundError:
        pass

    parts = [topic_hint]
    if style_prompt:
        parts.append(
            "OWNER'S COMMUNICATION STYLE (use this when drafting replies on their behalf):\n"
            + style_prompt
        )
    parts.append(
        "Available bot commands (for reference on what the bot can do):\n"
        + _build_command_list()
    )
    return "\n\n".join(parts)


async def _run_claude(
    question: str,
    session_id: str | None = None,
    image_path: str | None = None,
    thread_id: int | None = None,
    model: str | None = None,
) -> tuple[str, str, int]:
    """Run Claude Code CLI. Returns (response_text, session_id, input_tokens)."""
    cfg = get_config().get("claude", {})
    project_root = get_scripts_dir().parent
    model = model or cfg.get("model", "haiku")

    if image_path:
        question = f"I've attached an image at {image_path}. {question}" if question else f"Analyze this image at {image_path}"

    try:
        result = await run_claude_cli(
            question,
            model=model,
            system_prompt=_build_system_prompt(thread_id),
            tools="Bash,Read,Write,Grep,Glob",
            max_turns=cfg.get("max_turns", 10),
            max_budget_usd=str(cfg.get("max_budget_usd", "0.50")),
            session_id=session_id,
            cwd=str(project_root),
            timeout=120,
        )
        return result["text"], result["session_id"], result["input_tokens"]
    except RuntimeError as e:
        log.error("Claude CLI error: %s", e)
        return str(e), "", 0


def _get_session(user_id: int, thread_id: int | None) -> str | None:
    """Get active session for user in this thread."""
    return chat_sessions.get_session(user_id, context_key=thread_id)


def _set_session(user_id: int, session_id: str, thread_id: int | None):
    chat_sessions.set_session(user_id, session_id, context_key=thread_id)


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle any non-command message — pipe to Claude with topic context."""
    msg = update.message
    if not msg or not msg.from_user:
        return

    # Skip human-only topics and messages that @mention someone
    cfg = get_config()
    skip_topics = cfg.get("skip_topics", [])
    if msg.message_thread_id and msg.message_thread_id in skip_topics:
        return
    if msg.entities:
        for ent in msg.entities:
            if ent.type in ("mention", "text_mention"):
                return

    # Only handle messages from our group or DMs from group members
    chat_id = int(cfg["telegram"]["chat_id"])
    if msg.chat.type == "private":
        # Verify user is a member of our group
        try:
            member = await context.bot.get_chat_member(chat_id, msg.from_user.id)
            if member.status in ("left", "kicked"):
                return
        except Exception:
            return  # Can't verify = don't respond
    elif msg.chat.id != chat_id:
        return

    user_id = msg.from_user.id
    thread_id = msg.message_thread_id
    question = msg.text or msg.caption or ""
    image_path = None

    # Handle photos
    if msg.photo:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        await file.download_to_drive(tmp.name)
        image_path = tmp.name
        if not question:
            question = "What's in this image?"

    if not question and not image_path:
        return

    # If replying to a message, pull in that message's context
    reply_context = ""
    reply_session_id = None
    if msg.reply_to_message:
        reply_to = msg.reply_to_message
        reply_to_id = reply_to.message_id

        # Check for saved analysis context (from /yt, Reddit, X pipelines)
        ctx_file = CONTEXT_DIR / f"{reply_to_id}.txt"
        if ctx_file.exists():
            raw_ctx = ctx_file.read_text(encoding="utf-8")
            # Write to temp file for Claude to read (may be very large)
            tmp_ctx = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="reply-ctx-", delete=False
            )
            tmp_ctx.write(raw_ctx)
            tmp_ctx.close()
            reply_context = (
                f"The user is replying to an analysis message. "
                f"Full raw context (transcript/data + analysis) is at {tmp_ctx.name} — "
                f"read it to answer the user's question.\n\n"
            )
            # Try to reuse the analysis session from youtube plugin
            try:
                from plugins.youtube import _analysis_sessions
                reply_session_id = _analysis_sessions.get(reply_to_id)
            except Exception:
                pass
        else:
            # Regular reply — include the replied-to text inline
            reply_text = reply_to.text or reply_to.caption or ""
            if reply_text:
                # Trim to avoid blowing up context on very long messages
                if len(reply_text) > 2000:
                    reply_text = reply_text[:2000] + "... (truncated)"
                reply_context = (
                    f"The user is replying to this message:\n"
                    f"---\n{reply_text}\n---\n\n"
                )

    # Prepend cross-plugin context (e.g. recent /search results) so Claude
    # knows what the user is referring to even in a resumed session
    history = get_history_context(user_id)
    add_history(user_id, "user", question)

    if reply_context:
        question = reply_context + "User's message: " + question
    elif history:
        question = history + "Current message: " + question

    session_id = reply_session_id or _get_session(user_id, thread_id)
    async with _state_lock:
        model = _user_models.get(user_id)
    # Use sonnet for analysis follow-ups (they need more capability)
    has_analysis_context = reply_context and "reply-ctx-" in reply_context
    if has_analysis_context and not model:
        model = "sonnet"
    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
    thinking_msg = await msg.reply_text("replying...", disable_notification=True)

    tmp_ctx_path = None
    if has_analysis_context:
        # Extract temp file path from the reply_context string
        import re as _re
        m = _re.search(r"(/tmp/reply-ctx-[^\s]+)", reply_context)
        if m:
            tmp_ctx_path = m.group(1)

    try:
        response_text, new_session_id, input_tokens = await _run_claude(
            question, session_id, image_path, thread_id, model
        )

        # Track assistant response for cross-plugin context
        add_history(user_id, "assistant", response_text[:500])

        if new_session_id:
            _set_session(user_id, new_session_id, thread_id)
            # If this was an analysis follow-up, update the youtube plugin's session map
            if has_analysis_context and msg.reply_to_message:
                try:
                    from plugins.youtube import _analysis_sessions
                    _analysis_sessions[msg.reply_to_message.message_id] = new_session_id
                except Exception:
                    pass

        effective_model = model or get_config().get("claude", {}).get("model", "haiku")
        bar = _context_bar(input_tokens, effective_model)

        max_len = 4000 - len(bar)
        if len(response_text) > max_len:
            response_text = response_text[:max_len] + "\n\n... (truncated)"

        response_text += bar

        await thinking_msg.edit_text(response_text)

    except asyncio.TimeoutError:
        await thinking_msg.edit_text("Claude timed out after 2 minutes.")
    except Exception as e:
        log.error("Claude chat error: %s", e)
        await thinking_msg.edit_text(f"Error: {e}")
    finally:
        for path in [image_path, tmp_ctx_path]:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset Claude session for this user in this topic."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    old = chat_sessions.reset(user_id, context_key=update.message.message_thread_id)
    async with _state_lock:
        _user_models.pop(user_id, None)
    if old:
        await update.message.reply_text("Session reset. Next message starts fresh.")
    else:
        await update.message.reply_text("No active session.")


async def cmd_switch_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch Claude model for this user. If extra text follows, treat it as a message."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    # Extract model name — handle /sonnet@cupbots_bot
    cmd_part = update.message.text.strip("/").split()[0].lower()
    cmd = cmd_part.split("@")[0]  # strip @botname
    if cmd not in VALID_MODELS:
        return

    async with _state_lock:
        _user_models[user_id] = cmd

    # Check if there's a follow-up message after the model command
    # e.g. "/sonnet why didn't u switch" → switch + ask
    remaining = " ".join(context.args) if context.args else ""
    if remaining:
        msg = update.message
        thread_id = msg.message_thread_id
        session_id = _get_session(user_id, thread_id)
        await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        thinking_msg = await msg.reply_text(f"{cmd} replying...", disable_notification=True)

        try:
            response_text, new_session_id, input_tokens = await _run_claude(
                remaining, session_id, None, thread_id, cmd
            )
            if new_session_id:
                _set_session(user_id, new_session_id, thread_id)

            bar = _context_bar(input_tokens, cmd)
            max_len = 4000 - len(bar)
            if len(response_text) > max_len:
                response_text = response_text[:max_len] + "\n\n... (truncated)"
            response_text += bar
            await thinking_msg.edit_text(response_text)
        except asyncio.TimeoutError:
            await thinking_msg.edit_text("Claude timed out after 2 minutes.")
        except Exception as e:
            log.error("Claude chat error: %s", e)
            await thinking_msg.edit_text(f"Error: {e}")
    else:
        await update.message.reply_text(f"Switched to {cmd}.")


async def cmd_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate an image using Gemini Nano Banana. Supports text-to-image and image-to-image."""
    if not update.message:
        return

    args = list(context.args) if context.args else []
    model = None

    # Check for model prefix: /image pro ... or /image flash ...
    if args and args[0].lower() in ("pro", "flash"):
        model = args.pop(0).lower()

    prompt = " ".join(args)
    ref_bytes = None

    # Check if a photo is attached (image-to-image / style transfer)
    if update.message.photo:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = await file.download_as_bytearray()
        ref_bytes = bytes(buf)
        if not prompt:
            prompt = "Transform this image creatively"
    elif update.message.reply_to_message and update.message.reply_to_message.photo:
        # Also support replying to a photo with /image
        photo = update.message.reply_to_message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = await file.download_as_bytearray()
        ref_bytes = bytes(buf)
        if not prompt:
            prompt = "Transform this image creatively"

    if not prompt:
        await update.message.reply_text(
            "Usage:\n"
            "/image a cat on a bicycle\n"
            "/image pro a detailed portrait\n"
            "Send a photo with /image as caption\n"
            "Reply to a photo with /image make it anime style\n"
            "\nModels: flash (default, fast), pro (higher quality)",
        )
        return

    model_label = model or "flash"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    msg = await update.message.reply_text(f"generating image ({model_label})...")

    try:
        from cupbots.helpers.image_gen import generate_image
        result = await generate_image(prompt, reference_image=ref_bytes, model=model)

        if result["success"]:
            import io
            await msg.delete()
            await update.message.reply_photo(
                photo=io.BytesIO(result["image_bytes"]),
                caption=prompt[:1024],
            )
        else:
            await msg.edit_text(f"Image generation failed: {result.get('error', 'unknown error')}")
    except Exception as e:
        log.error("Image generation error: %s", e)
        await msg.edit_text(f"Error: {e}")


# ---------------------------------------------------------------------------
# /roast — Devil's advocate / indie hacker coach
# ---------------------------------------------------------------------------

_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _load_skill_prompt(skill_name: str) -> str | None:
    """Load a skill's SKILL.md content (strip frontmatter)."""
    skill_file = _SKILLS_DIR / skill_name / "SKILL.md"
    try:
        raw = skill_file.read_text()
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            return parts[2].strip() if len(parts) > 2 else raw
        return raw.strip()
    except FileNotFoundError:
        return None


async def cmd_roast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Devil's advocate roast using indie-hacker-coach skill."""
    if not update.message:
        return

    text = " ".join(context.args) if context.args else ""

    # Support replying to a message to roast it
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    if not text:
        await update.message.reply_text(
            "Usage: /roast <your idea, plan, or code decision>\n"
            "Or reply to any message with /roast"
        )
        return

    skill_prompt = _load_skill_prompt("indie-hacker-coach")
    if not skill_prompt:
        await update.message.reply_text("Skill 'indie-hacker-coach' not found in ~/.claude/skills/")
        return

    system = (
        skill_prompt + "\n\n"
        "ADDITIONAL ROLE: You are also the Devil's Advocate. "
        "For every claim, plan, or idea the user presents, find the Murphy's Law scenario. "
        "What will break? What are they not seeing? What's the laziest, fastest counter-argument? "
        "Be specific — name the exact failure mode, not vague warnings.\n\n"
        "Format: diagnose, roast, then prescribe (max 3 bullets). End with an accountability question."
    )

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    msg = await update.message.reply_text("roasting...", disable_notification=True)

    try:
        cfg = get_config().get("claude", {})
        result = await run_claude_cli(
            text,
            model="haiku",
            system_prompt=system,
            tools="",
            max_turns=1,
            max_budget_usd="0.05",
            cwd=str(get_scripts_dir().parent),
            timeout=60,
        )
        response = result["text"]
        bar = _context_bar(result["input_tokens"], "haiku")
        max_len = 4000 - len(bar)
        if len(response) > max_len:
            response = response[:max_len] + "\n\n... (truncated)"
        response += bar
        await msg.edit_text(response)
    except Exception as e:
        log.error("Roast error: %s", e)
        await msg.edit_text(f"Roast failed: {e}")


async def _cleanup_sessions(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job to clean up expired sessions."""
    n = chat_sessions.cleanup()
    if n:
        log.info("Cleaned up %d expired Claude sessions", n)


async def _handle_imagine_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos with /image caption."""
    msg = update.message
    if not msg or not msg.caption:
        return
    caption = msg.caption.strip()
    if not caption.startswith("/image"):
        return
    # Parse args from caption
    args = caption.split()[1:]  # strip /image
    context.args = args
    await cmd_image(update, context)


async def handle_command(msg, reply) -> bool:
    """Cross-platform handler for Claude AI commands."""
    chat_id = msg.chat_id

    if msg.command == "new":
        old = chat_sessions.reset(chat_id)
        await reply.reply_text("Session reset." if old else "No active session.")
        return True

    if msg.command in VALID_MODELS:
        # Model switch — no per-user model on WhatsApp, just use for this message
        model = msg.command
        remaining = " ".join(msg.args) if msg.args else ""
        if remaining:
            session_id = chat_sessions.get_session(chat_id)
            await reply.reply_text(f"{model} replying...")
            try:
                response_text, new_session_id, _ = await _run_claude(
                    remaining, session_id, model=model)
                if new_session_id:
                    chat_sessions.set_session(chat_id, new_session_id)
                if len(response_text) > 4000:
                    response_text = response_text[:3997] + "..."
                await reply.reply_text(response_text)
            except Exception as e:
                await reply.reply_error(f"Claude error: {e}")
        else:
            await reply.reply_text(f"Switched to {model}.")
        return True

    if msg.command == "roast":
        text = " ".join(msg.args) if msg.args else ""
        if not text and msg.reply_to_text:
            text = msg.reply_to_text
        if not text:
            await reply.reply_text("Usage: /roast <your idea or plan>")
            return True
        skill_prompt = _load_skill_prompt("indie-hacker-coach")
        if not skill_prompt:
            await reply.reply_text("Skill 'indie-hacker-coach' not found.")
            return True
        system = (
            skill_prompt + "\n\n"
            "ADDITIONAL ROLE: You are also the Devil's Advocate. "
            "For every claim, plan, or idea the user presents, find the Murphy's Law scenario. "
            "What will break? What are they not seeing? What's the laziest, fastest counter-argument? "
            "Be specific — name the exact failure mode, not vague warnings.\n\n"
            "Format: diagnose, roast, then prescribe (max 3 bullets). End with an accountability question."
        )
        await reply.reply_text("roasting...")
        try:
            result = await run_claude_cli(
                text, model="haiku", system_prompt=system,
                tools="", max_turns=1, max_budget_usd="0.05",
                cwd=str(get_scripts_dir().parent), timeout=60,
            )
            response = result["text"]
            if len(response) > 4000:
                response = response[:3997] + "..."
            await reply.reply_text(response)
        except Exception as e:
            await reply.reply_error(f"Roast failed: {e}")
        return True

    if msg.command == "think":
        # Delegate to heartbeat plugin if available
        return False

    return False


def register(app: Application):
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("roast", cmd_roast))
    app.add_handler(CommandHandler("imagine", cmd_image))
    # Catch photos with /image caption (Telegram doesn't treat photo captions as commands)
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.CaptionRegex(r"^/image"),
        _handle_imagine_photo,
    ), group=10)
    for model in VALID_MODELS:
        app.add_handler(CommandHandler(model, cmd_switch_model))

    # Catch all non-command text/photo messages — lowest priority group
    # Exclude photos/docs with captions starting with / (handled by other plugins)
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.CAPTION)
            & ~filters.COMMAND
            & ~filters.CaptionRegex(r"^/"),
            _handle_message,
        ),
        group=99,  # Run after all other handlers
    )

    app.job_queue.run_repeating(_cleanup_sessions, interval=600, first=600)
    log.info("Claude AI chat active in all topics and DMs")
