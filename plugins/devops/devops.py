"""
DevOps

Commands (scoped to devops topic thread):
  /run_reddit      — Reddit digest (Run = fetch+analyze, Analyze Only = skip fetch)
  /run_x           — X list scraper (Run = fetch+analyze, Analyze Only = skip fetch)
  /run_yt          — Trigger YouTube analyzer pipeline
  /status          — Check if pipelines ran today
  /schedule        — Show scheduled pipeline times
  /logs [N] [level] — Recent error/warning logs
  /devops          — Show help
"""

import asyncio
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from cupbots.config import get_config, get_thread_id, get_scripts_dir
from cupbots.topic_filter import topic_command
from cupbots.helpers.logger import get_logger, get_db_logs

log = get_logger("devops")

SCRIPTS_DIR = get_scripts_dir()


async def _run_pipeline(name: str, extra_args: list[str] | None = None) -> tuple[bool, str]:
    """Run a pipeline by name from config. Returns (success, message)."""
    cfg = get_config()["pipelines"].get(name)
    if not cfg:
        return False, f"Unknown pipeline: {name}"

    script = str(SCRIPTS_DIR / cfg["script"])
    cwd = str(SCRIPTS_DIR / cfg["cwd"])
    backend = cfg.get("backend", "api")

    timeout = cfg.get("timeout", 600)
    cmd = [sys.executable, script, "--backend", backend] + (extra_args or [])
    log.info("Starting pipeline: %s %s", name, " ".join(extra_args or []))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode == 0:
            log.info("Pipeline %s completed successfully", name)
            output = stdout.decode() if stdout else ""
            # Filter noisy download progress lines
            lines = [l for l in output.splitlines()
                     if not l.strip().startswith("[download]")]
            output = "\n".join(lines).strip()[-1000:]
            if output:
                return True, f"✅ {name} complete!\n\n{output}"
            return True, f"✅ {name} complete!"
        else:
            err = stderr.decode()[-500:] if stderr else ""
            out = stdout.decode()[-500:] if stdout else ""
            detail = err or out or "No output"
            log.error("Pipeline %s failed (rc=%d): %s", name, proc.returncode, detail[:300])
            return False, f"❌ {name} failed (rc={proc.returncode}):\n```\n{detail}\n```"
    except asyncio.TimeoutError:
        log.error("Pipeline %s timed out after %ds", name, timeout)
        return False, f"⏰ {name} timed out ({timeout // 60}min)"
    except Exception as e:
        log.error("Pipeline %s error: %s", name, e)
        return False, f"💥 {name} error: {e}"


async def cmd_run_reddit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Run", callback_data="run:reddit"),
        InlineKeyboardButton("Analyze Only", callback_data="run:reddit:skip"),
        InlineKeyboardButton("Cancel", callback_data="run:cancel"),
    ]])
    await update.message.reply_text("Run Reddit digest?", reply_markup=kb)


async def cmd_run_x(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Run", callback_data="run:x"),
        InlineKeyboardButton("Analyze Only", callback_data="run:x:skip"),
        InlineKeyboardButton("Cancel", callback_data="run:cancel"),
    ]])
    await update.message.reply_text("Run X list scraper?", reply_markup=kb)


async def cmd_run_yt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    # /run_yt <N> — run N videos directly without confirmation
    if args and args[0].isdigit():
        n = int(args[0])
        await update.message.reply_text(f"🚀 Running youtube (limit {n})...")
        success, msg = await _run_pipeline("youtube", ["--limit", str(n)])
        await update.message.reply_text(msg)
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Run All", callback_data="run:yt"),
        InlineKeyboardButton("Dry Run (1)", callback_data="run:yt:dry"),
        InlineKeyboardButton("Cancel", callback_data="run:cancel"),
    ]])
    await update.message.reply_text("Run YouTube analyzer?", reply_markup=kb)


PIPELINE_MAP = {"reddit": "reddit", "x": "x", "yt": "youtube"}


async def _callback_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pipeline run confirmation buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "run:cancel":
        await query.edit_message_text("Cancelled.")
        return

    if data.startswith("run:"):
        parts = data.split(":")
        name = parts[1]
        modifier = parts[2] if len(parts) > 2 else None
        pipeline = PIPELINE_MAP.get(name)
        if not pipeline:
            return
        extra_args = []
        if modifier == "dry":
            extra_args = ["--dry-run", "--no-telegram"]
        elif modifier == "skip":
            extra_args = ["--skip-scrape"]
        label = pipeline
        if modifier == "dry":
            label = f"{pipeline} (dry run)"
        elif modifier == "skip":
            label = f"{pipeline} (analyze only)"
        await query.edit_message_text(f"🚀 Running {label}...")
        success, msg = await _run_pipeline(pipeline, extra_args)
        # Strip markdown if present for edit_message_text
        if "```" in msg:
            await query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await query.edit_message_text(msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_config()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📋 Pipeline Status ({today})\n"]

    for name, pcfg in cfg.get("pipelines", {}).items():
        log_path = SCRIPTS_DIR / pcfg.get("log", "")
        if not log_path.exists():
            lines.append(f"⚪ {name}: No log file")
            continue

        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        if today in log_text:
            last_run = "today"
            for line in reversed(log_text.splitlines()):
                if today in line and "===" in line:
                    last_run = line.strip("= \n")
                    break
            lines.append(f"✅ {name}: Ran {last_run}")
        else:
            lines.append(f"⚠️ {name}: Not run today")

    await update.message.reply_text("\n".join(lines))


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cfg = get_config()
    sched = cfg["schedule"]
    tz = sched["timezone"]
    lines = [f"⏱️ Pipeline Schedule ({tz})\n"]

    def _fmt_entry(entry):
        if isinstance(entry, str):
            return entry
        name = entry["name"]
        days = entry.get("days")
        if days is not None:
            day_str = ", ".join(DAY_NAMES[d] for d in days)
            return f"{name} ({day_str})"
        return name

    lines.append(f"Morning ({sched['morning_hour']:02d}:{sched['morning_minute']:02d}):")
    for p in sched.get("morning_pipelines", []):
        lines.append(f"  • {_fmt_entry(p)}")
    lines.append(f"\nEvening ({sched['evening_hour']:02d}:{sched['evening_minute']:02d}):")
    for p in sched.get("evening_pipelines", []):
        lines.append(f"  • {_fmt_entry(p)}")
    await update.message.reply_text("\n".join(lines))


async def _scheduled_pipeline(context: ContextTypes.DEFAULT_TYPE):
    """Job callback — run a scheduled pipeline and post result to devops thread."""
    job_data = context.job.data  # str or dict with name + days
    if isinstance(job_data, dict):
        name = job_data["name"]
        allowed_days = job_data.get("days")
        if allowed_days is not None:
            today = datetime.now().weekday()  # 0=Mon, 6=Sun
            if today not in allowed_days:
                log.info("Skipping %s — not scheduled for today (day=%d)", name, today)
                return
    else:
        name = job_data
    cfg = get_config()
    chat_id = cfg["telegram"]["chat_id"]
    thread_id = get_thread_id("devops")

    log.info("Scheduled run: %s", name)
    success, msg = await _run_pipeline(name)

    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=f"🤖 Scheduled: {msg}",
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent error/warning logs from the database."""
    n = 20
    level = None
    if context.args:
        for arg in context.args:
            if arg.isdigit():
                n = min(int(arg), 50)
            elif arg.upper() in ("ERROR", "WARNING"):
                level = arg

    entries = get_db_logs(n=n, level=level)
    if not entries:
        await update.message.reply_text("No logs found.")
        return

    lines = []
    for e in entries:
        ts = e["timestamp"][5:]  # strip year
        lvl = "⚠️" if e["level"] == "WARNING" else "❌"
        logger = e["logger"].removeprefix("bot.")
        # Truncate message to keep it readable
        msg = e["message"]
        # Strip the formatted prefix (timestamp/logger/level) from the message
        if msg.startswith("["):
            parts = msg.split("] ", 3)
            msg = parts[-1] if len(parts) > 3 else msg
        if len(msg) > 120:
            msg = msg[:117] + "..."
        lines.append(f"{lvl} {ts} [{logger}] {msg}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]
    await update.message.reply_text(f"📋 Last {len(entries)} logs:\n\n{text}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 DevOps Commands\n\n"
        "/run_reddit — Reddit digest (fetch + analyze, or analyze only)\n"
        "/run_x — X list scraper (fetch + analyze, or analyze only)\n"
        "/run_yt — Trigger YouTube analyzer\n"
        "/status — Check if pipelines ran today\n"
        "/schedule — Show scheduled pipeline times\n"
        "/logs [N] [error|warning] — Recent error/warning logs",
    )


def register(app: Application):
    """Register all devops commands, scoped to the devops topic thread."""
    cfg = get_config()
    thread_id = get_thread_id("devops") or 134

    commands = {
        "run_reddit": cmd_run_reddit,
        "run_x": cmd_run_x,
        "run_yt": cmd_run_yt,
        "status": cmd_status,
        "schedule": cmd_schedule,
        "logs": cmd_logs,
        "devops": cmd_help,
    }
    for name, handler in commands.items():
        app.add_handler(topic_command(name, handler, thread_id=thread_id))

    app.add_handler(CallbackQueryHandler(_callback_run, pattern=r"^run:"))

    # Schedule pipelines
    sched = cfg["schedule"]
    tz = ZoneInfo(sched["timezone"])

    def _parse_pipeline_entry(entry):
        """Parse pipeline entry — either a string or {name, days} dict."""
        if isinstance(entry, str):
            return entry, entry
        return entry["name"], entry

    for entry in sched.get("morning_pipelines", []):
        pipeline_name, job_data = _parse_pipeline_entry(entry)
        app.job_queue.run_daily(
            _scheduled_pipeline,
            time=time(sched["morning_hour"], sched["morning_minute"], tzinfo=tz),
            name=f"sched_{pipeline_name}_morning",
            data=job_data,
        )

    for entry in sched.get("evening_pipelines", []):
        pipeline_name, job_data = _parse_pipeline_entry(entry)
        app.job_queue.run_daily(
            _scheduled_pipeline,
            time=time(sched["evening_hour"], sched["evening_minute"], tzinfo=tz),
            name=f"sched_{pipeline_name}_evening",
            data=job_data,
        )

    log.info("Scheduled %d morning + %d evening pipelines",
             len(sched.get("morning_pipelines", [])),
             len(sched.get("evening_pipelines", [])))
