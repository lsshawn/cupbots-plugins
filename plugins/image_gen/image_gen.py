"""
Image Gen — Generate images using Gemini.

Commands (works in any topic):
  /img <prompt>        — Generate an image (flash model)
  /img pro <prompt>    — Generate with higher quality (pro model)

Reply to a photo with /img <prompt> to use it as reference.

Usage examples:
  /img a cat riding a bicycle
  /img pro detailed oil painting of a sunset
"""

import asyncio
import io
import os
from functools import partial

from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.logger import get_logger

log = get_logger("image_gen")

# ---------------------------------------------------------------------------
# Gemini image generation
# ---------------------------------------------------------------------------

MODELS = {
    "flash": "gemini-2.5-flash-image",
    "pro": "gemini-3-pro-image-preview",
}

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in .env")
        _client = genai.Client(api_key=api_key)
    return _client


def _generate_sync(prompt, reference_image=None, mime_type="image/jpeg", model=None):
    client = _get_client()
    model_id = MODELS.get(model or "flash", MODELS["flash"])

    contents = []
    if reference_image:
        contents.append(types.Part.from_bytes(data=reference_image, mime_type=mime_type))
    contents.append(prompt)

    response = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            return {
                "success": True,
                "image_bytes": part.inline_data.data,
                "mime_type": part.inline_data.mime_type or "image/png",
                "prompt": prompt,
                "model": model_id,
            }

    text_parts = [p.text for p in response.candidates[0].content.parts if p.text]
    return {
        "success": False,
        "error": text_parts[0] if text_parts else "No image generated",
        "prompt": prompt,
    }


async def generate_image(prompt, reference_image=None, mime_type="image/jpeg", model=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_generate_sync, prompt, reference_image, mime_type, model)
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args(args: list[str]) -> tuple[str | None, str]:
    if not args:
        return None, ""
    if args[0].lower() in ("pro", "flash"):
        return args[0].lower(), " ".join(args[1:])
    return None, " ".join(args)


# ---------------------------------------------------------------------------
# Cross-platform handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "img":
        return False

    model, prompt = _parse_args(msg.args)
    if not prompt:
        await reply.reply_text("Usage: /img <prompt>\n/img pro <prompt>")
        return True

    await reply.reply_text("Generating...")
    result = await generate_image(prompt, model=model)

    if result["success"]:
        await reply.reply_text(f"Image generated ({result['model']}). View in Telegram.")
    else:
        await reply.reply_text(f"Failed: {result.get('error', 'Unknown error')}")

    return True


# ---------------------------------------------------------------------------
# Telegram-specific handler (sends photo inline)
# ---------------------------------------------------------------------------

async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    model, prompt = _parse_args(context.args or [])
    if not prompt:
        await update.message.reply_text(
            "Usage: `/img <prompt>`\n`/img pro <prompt>`\n\n"
            "Reply to a photo to use it as reference.",
            parse_mode="Markdown",
        )
        return

    # Check if replying to a photo (use as reference)
    reference_image = None
    mime_type = "image/jpeg"
    reply_msg = update.message.reply_to_message
    if reply_msg and reply_msg.photo:
        status = await update.message.reply_text("Downloading reference...")
        photo = reply_msg.photo[-1]  # highest resolution
        tg_file = await photo.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        reference_image = buf.getvalue()
        await status.edit_text("Generating...")
    else:
        status = await update.message.reply_text("Generating...")

    try:
        result = await generate_image(prompt, reference_image=reference_image,
                                     mime_type=mime_type, model=model)
    except Exception as e:
        log.error("Image generation failed: %s", e)
        await status.edit_text(f"Failed: {e}")
        return

    if not result["success"]:
        await status.edit_text(f"Failed: {result.get('error', 'Unknown error')}")
        return

    # Send the image
    image_bytes = result["image_bytes"]
    caption = f"{prompt[:200]}" if len(prompt) > 200 else prompt

    try:
        await update.message.reply_photo(
            photo=io.BytesIO(image_bytes),
            caption=caption,
        )
        await status.delete()
    except Exception as e:
        log.error("Failed to send photo: %s", e)
        # Fall back to document if photo fails (e.g. too large)
        try:
            await update.message.reply_document(
                document=io.BytesIO(image_bytes),
                filename="generated.png",
                caption=caption,
            )
            await status.delete()
        except Exception as e2:
            log.error("Failed to send document: %s", e2)
            await status.edit_text(f"Generated but failed to send: {e2}")


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(app: Application):
    app.add_handler(CommandHandler("img", cmd_img))
