"""
DevOps — Run and monitor data pipelines.

Commands:
  /devops                           — Show help
  /devops run <pipeline> [args]     — Run a pipeline (reddit, x, youtube)
  /devops status                    — Check if pipelines ran today
  /devops schedule                  — Show scheduled pipeline times
  /devops logs [N] [error|warning]  — Recent error/warning logs

Examples:
  /devops run reddit
  /devops run reddit --skip-scrape
  /devops run youtube --limit 3
  /devops status
  /devops logs 10 error
"""

import asyncio
import sys
from datetime import datetime

from cupbots.config import get_config, get_scripts_dir
from cupbots.helpers.logger import get_logger, get_db_logs

log = get_logger("devops")

SCRIPTS_DIR = get_scripts_dir()

PIPELINE_ALIASES = {
    "reddit": "reddit",
    "x": "x",
    "yt": "youtube",
    "youtube": "youtube",
}


async def _run_pipeline(name: str, extra_args: list[str] | None = None) -> tuple[bool, str]:
    """Run a pipeline by name from config. Returns (success, message)."""
    cfg = get_config().get("pipelines", {}).get(name)
    if not cfg:
        available = ", ".join(get_config().get("pipelines", {}).keys())
        return False, f"Unknown pipeline: {name}. Available: {available}"

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
            lines = [l for l in output.splitlines()
                     if not l.strip().startswith("[download]")]
            output = "\n".join(lines).strip()[-1000:]
            if output:
                return True, f"{name} complete!\n\n{output}"
            return True, f"{name} complete!"
        else:
            err = stderr.decode()[-500:] if stderr else ""
            out = stdout.decode()[-500:] if stdout else ""
            detail = err or out or "No output"
            log.error("Pipeline %s failed (rc=%d): %s", name, proc.returncode, detail[:300])
            return False, f"{name} failed (rc={proc.returncode}):\n{detail}"
    except asyncio.TimeoutError:
        log.error("Pipeline %s timed out after %ds", name, timeout)
        return False, f"{name} timed out ({timeout // 60}min)"
    except Exception as e:
        log.error("Pipeline %s error: %s", name, e)
        return False, f"{name} error: {e}"


def _get_status() -> str:
    cfg = get_config()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"Pipeline Status ({today})\n"]

    for name, pcfg in cfg.get("pipelines", {}).items():
        log_path = SCRIPTS_DIR / pcfg.get("log", "")
        if not log_path.exists():
            lines.append(f"  {name}: No log file")
            continue

        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        if today in log_text:
            last_run = "today"
            for line in reversed(log_text.splitlines()):
                if today in line and "===" in line:
                    last_run = line.strip("= \n")
                    break
            lines.append(f"  {name}: Ran {last_run}")
        else:
            lines.append(f"  {name}: Not run today")

    return "\n".join(lines)


def _get_schedule() -> str:
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cfg = get_config()
    sched = cfg.get("schedule", {})
    if not sched:
        return "No schedule configured."

    tz = sched.get("timezone", "UTC")
    lines = [f"Pipeline Schedule ({tz})\n"]

    def _fmt_entry(entry):
        if isinstance(entry, str):
            return entry
        name = entry["name"]
        days = entry.get("days")
        if days is not None:
            day_str = ", ".join(DAY_NAMES[d] for d in days)
            return f"{name} ({day_str})"
        return name

    morning = sched.get("morning_pipelines", [])
    evening = sched.get("evening_pipelines", [])

    if morning:
        lines.append(f"Morning ({sched.get('morning_hour', 6):02d}:{sched.get('morning_minute', 0):02d}):")
        for p in morning:
            lines.append(f"  {_fmt_entry(p)}")

    if evening:
        lines.append(f"\nEvening ({sched.get('evening_hour', 20):02d}:{sched.get('evening_minute', 0):02d}):")
        for p in evening:
            lines.append(f"  {_fmt_entry(p)}")

    return "\n".join(lines)


def _get_logs(args: list[str]) -> str:
    n = 20
    level = None
    for arg in args:
        if arg.isdigit():
            n = min(int(arg), 50)
        elif arg.upper() in ("ERROR", "WARNING"):
            level = arg

    entries = get_db_logs(n=n, level=level)
    if not entries:
        return "No logs found."

    lines = []
    for e in entries:
        ts = e["timestamp"][5:]  # strip year
        lvl = "W" if e["level"] == "WARNING" else "E"
        logger = e["logger"].removeprefix("bot.")
        msg = e["message"]
        if msg.startswith("["):
            parts = msg.split("] ", 3)
            msg = parts[-1] if len(parts) > 3 else msg
        if len(msg) > 120:
            msg = msg[:117] + "..."
        lines.append(f"[{lvl}] {ts} [{logger}] {msg}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]
    return f"Last {len(entries)} logs:\n\n{text}"


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    if msg.command != "devops":
        return False

    args = msg.args
    if not args:
        await reply.reply_text(__doc__.strip())
        return True

    sub = args[0].lower()

    if sub in ("--help", "-h", "help"):
        await reply.reply_text(__doc__.strip())
        return True

    if sub == "run":
        if len(args) < 2:
            available = ", ".join(get_config().get("pipelines", {}).keys())
            await reply.reply_text(f"Usage: /devops run <pipeline> [args]\nAvailable: {available}")
            return True

        pipeline_name = PIPELINE_ALIASES.get(args[1].lower(), args[1].lower())
        extra_args = args[2:]

        await reply.send_typing()
        await reply.reply_text(f"Running {pipeline_name}...")
        success, result = await _run_pipeline(pipeline_name, extra_args)
        await reply.reply_text(result)
        return True

    if sub == "status":
        await reply.reply_text(_get_status())
        return True

    if sub == "schedule":
        await reply.reply_text(_get_schedule())
        return True

    if sub == "logs":
        await reply.reply_text(_get_logs(args[1:]))
        return True

    await reply.reply_text(__doc__.strip())
    return True
