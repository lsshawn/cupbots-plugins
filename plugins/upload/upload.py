"""
Upload

Commands (works in any topic):
  /uploads  — List recent uploads

Send any file/photo/video to auto-save to uploads/.
Add caption "upload [folder]" to save to a specific folder.
"""

import sys
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ApplicationHandlerStop, CommandHandler, ContextTypes, MessageHandler, filters

from cupbots.helpers.logger import get_logger

log = get_logger("upload")

from cupbots.config import get_config as _get_config
NOTE_ROOT = Path(_get_config().get("allowed_paths", {}).get("notes", "/home/ss/projects/note"))
DEFAULT_FOLDER = "uploads"


def _extract_file(msg) -> tuple:
    """Extract file object and filename from a message. Returns (file_obj, filename)."""
    if msg.document:
        return msg.document, msg.document.file_name
    if msg.photo:
        return msg.photo[-1], f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    if msg.video:
        return msg.video, msg.video.file_name or f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    if msg.audio:
        return msg.audio, msg.audio.file_name or f"audio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
    if msg.voice:
        return msg.voice, f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg"
    if msg.video_note:
        return msg.video_note, f"videonote_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    if msg.sticker:
        return msg.sticker, f"sticker_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webp"
    return None, None


async def _save_file(update: Update, folder: str, show_help: bool = True) -> bool:
    """Download and save a file to the given folder. Returns True if saved."""
    msg = update.message

    # Resolve and validate target directory
    target_dir = (NOTE_ROOT / folder).resolve()
    if not str(target_dir).startswith(str(NOTE_ROOT)):
        await msg.reply_text("❌ Invalid folder path.")
        return False

    # Check the message itself first, then fall back to replied-to message
    file_obj, filename = _extract_file(msg)
    if not file_obj and msg.reply_to_message:
        file_obj, filename = _extract_file(msg.reply_to_message)
    log.info("Extracted file: obj=%s name=%s", bool(file_obj), filename)

    if not file_obj:
        if show_help:
            await msg.reply_text(
                "📎 Send a file to auto-save to `uploads/`\n"
                "Add caption `upload [folder]` to pick a folder",
                parse_mode="Markdown",
            )
        return False

    target_dir.mkdir(parents=True, exist_ok=True)

    dest = target_dir / filename
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = target_dir / f"{stem}_{ts}{suffix}"

    try:
        log.info("Downloading %s to %s", filename, dest)
        tg_file = await file_obj.get_file()
        await tg_file.download_to_drive(str(dest))
        log.info("Download complete: %s", dest)
    except Exception as e:
        log.error("Upload failed: %s", e)
        await msg.reply_text(f"❌ Download failed: {e}")
        return False

    rel = dest.relative_to(NOTE_ROOT)
    size_kb = dest.stat().st_size / 1024

    log.info("Uploaded %s (%.1f KB)", rel, size_kb)
    await msg.reply_text(f"✅ Saved: `{rel}` ({size_kb:.1f} KB)", parse_mode="Markdown")
    return True



async def _handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle any file sent with caption 'upload [folder]' or no caption."""
    msg = update.message
    if not msg:
        return

    log.info("Attachment handler fired: doc=%s photo=%s video=%s caption=%r",
             bool(msg.document), bool(msg.photo), bool(msg.video), msg.caption)

    caption = (msg.caption or "").strip()

    # If caption starts with another command (e.g. /expense), skip — let other plugins handle it
    if caption.startswith("/"):
        return

    # Parse folder from caption: "upload some/folder" or empty
    if caption.lower().startswith("upload"):
        folder = caption[6:].strip() or DEFAULT_FOLDER
    elif caption:
        # Has a non-upload caption — not for us, let claude_chat handle it
        return
    else:
        # No caption at all — save to default folder
        folder = DEFAULT_FOLDER

    saved = await _save_file(update, folder, show_help=False)
    if saved:
        raise ApplicationHandlerStop  # prevent claude_chat from also responding


async def cmd_uploads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List recent uploads in the uploads/ folder."""
    if not update.message:
        return

    uploads_dir = NOTE_ROOT / DEFAULT_FOLDER
    if not uploads_dir.exists():
        await update.message.reply_text("No uploads yet.")
        return

    # Get all files, sorted by modification time (newest first)
    files = sorted(uploads_dir.rglob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    files = [f for f in files if f.is_file()]

    if not files:
        await update.message.reply_text("No uploads yet.")
        return

    lines = ["📁 *Recent uploads:*\n"]
    for f in files[:10]:
        rel = f.relative_to(NOTE_ROOT)
        size_kb = f.stat().st_size / 1024
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %H:%M")
        lines.append(f"• `{rel}` ({size_kb:.1f} KB, {mtime})")

    if len(files) > 10:
        lines.append(f"\n_{len(files)} total files_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_command(msg, reply) -> bool:
    """Cross-platform upload commands."""
    if msg.command == "uploads":
        uploads_dir = NOTE_ROOT / DEFAULT_FOLDER
        if not uploads_dir.exists():
            await reply.reply_text("No uploads yet.")
            return True
        files = sorted(uploads_dir.rglob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
        files = [f for f in files if f.is_file()]
        if not files:
            await reply.reply_text("No uploads yet.")
            return True
        lines = ["Recent uploads:\n"]
        for f in files[:10]:
            rel = f.relative_to(NOTE_ROOT)
            size_kb = f.stat().st_size / 1024
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %H:%M")
            lines.append(f"  {rel} ({size_kb:.1f} KB, {mtime})")
        if len(files) > 10:
            lines.append(f"\n{len(files)} total files")
        await reply.reply_text("\n".join(lines))
        return True
    return False


def register(app: Application):
    app.add_handler(CommandHandler("uploads", cmd_uploads))

    # Catch any file/photo/video sent without a command — save to uploads/
    # group=50: runs before claude_chat (group=99) but after finance caption handlers (group=0)
    attachment_filter = (
        filters.PHOTO | filters.Document.ALL | filters.VIDEO
        | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL
    ) & ~filters.COMMAND
    app.add_handler(MessageHandler(attachment_filter, _handle_attachment), group=50)
