---
name: bot-plugin
description: Generate production-ready bot plugins compatible with the multi-platform Telegram/WhatsApp bot framework. Enforces cross-platform handle_command(), PocketBase storage, tenant isolation, and consistent help/docs.
---

You are generating a plugin for a multi-platform bot framework. The plugin must work on both Telegram and WhatsApp, use PocketBase for client-facing data, and follow the exact patterns below.

The user provides: what the plugin should do, its commands, and what data it manages.

## Project Paths

- Framework: `/home/ss/projects/cupbots/cupbots/`
- Plugin marketplace: `/home/ss/projects/cupbots/cupbots-plugins/plugins/`
- PB migrations (framework): `/home/ss/projects/cupbots/pocketbase/pb_migrations/`
- Helpers: `/home/ss/projects/cupbots/cupbots/helpers/`
- Template: `/home/ss/projects/cupbots/cupbots/plugins/_template.py`
- Personal plugins (Telegram-only): loaded via `external_plugin_dirs` in config.yaml

## Output Files

For a marketplace plugin named `<name>`, create these files:

```
cupbots-plugins/plugins/<name>/
├── <name>.py           # Plugin code
├── plugin.json         # Metadata
└── <timestamp>_<name>.js  # PB migration (if storing data)
```

Then run: `python3 /home/ss/projects/cupbots/cupbots-plugins/scripts/build_registry.py`

For a personal plugin (not in marketplace), write to the external_plugin_dirs path instead.

## plugin.json (MANDATORY)

```json
{
  "name": "<name>",
  "description": "One-line description",
  "version": "1.0.0",
  "author": "CupBots",
  "commands": ["/cmd1", "/cmd2"],
  "config": [],
  "free": true,
  "bundled": false
}
```

Add config items if the plugin needs API keys:

```json
"config": [
  { "key": "MY_API_KEY", "label": "API Key", "type": "secret", "required": true, "help": "Get at ..." }
]
```

## Module Docstring (MANDATORY — used for /command --help)

```python
"""
Plugin Name — One-line description.

Commands (works in any topic):
  /cmd1 <required_arg>          — What it does
  /cmd1 <arg> #tag              — Variant with tag
  /cmd2                         — Another command

Examples:
  /cmd1 hello world
  /cmd1 my report -- body text here #finance
  /cmd2
"""
```

Rules:
- First line: `Plugin Name — Description` (em-dash, not hyphen)
- `Commands (works in any topic):` header (exact text)
- Each command: `/name <args>` padded to align em-dashes
- Em-dash separator: ` — ` (space-emdash-space)
- `Examples:` section with real usage (users see this on `--help`)

## File Structure (MANDATORY order)

```python
"""docstring"""

# Imports
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from cupbots.helpers.logger import get_logger

log = get_logger("<plugin-name>")

# ---------------------------------------------------------------------------
# Core logic (shared between Telegram and cross-platform handlers)
# ---------------------------------------------------------------------------

async def _do_<action>(...) -> str:
    ...

# ---------------------------------------------------------------------------
# Cross-platform handler (REQUIRED)
# ---------------------------------------------------------------------------

async def handle_command(msg, reply) -> bool:
    ...

# ---------------------------------------------------------------------------
# Telegram-specific handlers
# ---------------------------------------------------------------------------

async def cmd_<name>(update, context):
    ...

# ---------------------------------------------------------------------------
# Plugin registration (REQUIRED)
# ---------------------------------------------------------------------------

def register(app: Application):
    ...
```

## Cross-Platform Handler (REQUIRED)

```python
async def handle_command(msg, reply) -> bool:
    """Platform-agnostic command handler."""
    if msg.command == "cmd1":
        if not msg.args:
            await reply.reply_text("Usage: /cmd1 <arg>")
            return True
        # Use msg.company_id for tenant isolation
        result = await _do_thing(msg.args, company_id=msg.company_id)
        await reply.reply_text(result)
        return True
    return False
```

Rules:
- Takes `msg` (IncomingMessage) and `reply` (ReplyContext) — NO type annotations in signature (avoids import dependency)
- Returns `True` if handled, `False` if not
- ALWAYS show usage on empty args
- ALWAYS use `msg.company_id` when querying/storing data
- NEVER use Telegram-specific APIs (no `update`, `context`, `InlineKeyboardMarkup`)
- Use `reply.reply_text()` for output, `reply.reply_error()` for errors

## msg (IncomingMessage) attributes

```
msg.platform      "telegram" | "whatsapp"
msg.chat_id       platform-native chat ID
msg.sender_id     platform-native sender ID
msg.sender_name   display name
msg.text          raw message text
msg.command        parsed command name or None
msg.args          list[str] after command
msg.reply_to_text  quoted message text or None
msg.company_id    tenant identifier (scope ALL data queries by this)
msg.group_config  full PocketBase group record (metadata dict, etc.)
msg.is_group      bool
```

## Data Storage

### PocketBase (for all persistent data)

```python
from cupbots.helpers.pb import pb_create, pb_find_one, pb_find_many, pb_update, pb_delete

# ALWAYS filter by company_id for tenant isolation
record = await pb_find_one("my_collection", f"key='{key}' && company_id='{cid}'")
records = await pb_find_many("my_collection", f"company_id='{cid}'")
new = await pb_create("my_collection", {"key": key, "company_id": cid, "data": value})
await pb_update("my_collection", record["id"], {"data": new_value})
await pb_delete("my_collection", record["id"])
```

### PocketBase Migration (if plugin stores data)

File: `<timestamp>_<plugin_name>.js` in the plugin directory.

```javascript
migrate((app) => {
  const collection = new Collection({
    type: "base",
    name: "<plugin_name>_<table>",
    listRule: "",
    viewRule: "",
    createRule: "",
    updateRule: "",
    deleteRule: null,
    fields: [
      { name: "company_id", type: "text" },
      { name: "your_field", type: "text", required: true },
    ],
    indexes: [
      "CREATE INDEX idx_<table>_company ON <plugin_name>_<table> (company_id)",
    ],
  });
  app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("<plugin_name>_<table>");
  app.delete(collection);
});
```

Rules:
- Collection name: `<plugin_name>_<table>` (namespaced to avoid collisions)
- ALWAYS include `company_id` field + index
- `deleteRule: null` — superusers only (safety)

## Job Queue (for scheduled/deferred tasks)

```python
from cupbots.helpers.jobs import enqueue, register_handler

async def _handle_my_job(payload: dict, bot=None):
    if bot:
        await bot.send_message(chat_id=payload["chat_id"], text="Done!")

def register(app):
    register_handler("my_queue", _handle_my_job)

# To enqueue:
from datetime import datetime, timedelta
enqueue("my_queue", {"chat_id": chat_id, "data": "..."}, run_at=datetime.now() + timedelta(hours=1))
```

## Telegram-Specific Handlers (optional)

Use ONLY for features that can't work cross-platform:
- Inline keyboards, reply keyboards
- File uploads/downloads
- Typing indicators
- Message editing
- Thread/topic scoping

## Error Handling

```python
try:
    result = await some_operation()
    await reply.reply_text(result)
except Exception as e:
    log.error("Operation failed: %s", e)
    await reply.reply_error(str(e))
```

## Checklist Before Submitting

- [ ] Module docstring with Commands section and Examples
- [ ] `plugin.json` with name, description, version, commands
- [ ] `handle_command()` returns True/False
- [ ] `handle_command()` shows usage on empty args
- [ ] `register()` registers all Telegram handlers
- [ ] All data queries filtered by `company_id`
- [ ] PB migration file created (if storing data)
- [ ] PB collection name namespaced: `<plugin>_<table>`
- [ ] Logger initialized: `log = get_logger("<name>")`
- [ ] Imports use `from cupbots.helpers.X import Y`
- [ ] Run `build_registry.py` after adding plugin
