# CupBots Plugins (Marketplace)

Private repo containing all official CupBots plugins. Served to users via the marketplace API or read locally by the framework.

## Structure

Each plugin is a self-contained directory under `plugins/`:

```
plugins/<name>/
├── <name>.py           # Plugin code (required)
└── plugin.json         # Metadata (required)
```

## Key Rules

- Every plugin MUST have `handle_command(msg, reply) -> bool` and `register(app)`
- Every plugin MUST have a module docstring (used for `--help`)
- All data queries MUST be scoped by `msg.company_id` (tenant isolation)
- Use per-plugin SQLite via `get_plugin_db("plugin_name")` for storage
- Define `create_tables(conn)` for auto-init of tables on first DB access
- Plugin code uses `from cupbots.helpers.X import Y` imports
- After changing any `plugin.json`, run `python3 scripts/build_registry.py`
- Never edit `registry.json` manually — it's auto-generated

## Plugin Template

See `_template.py` in the main cupbots framework repo: `cupbots/plugins/_template.py`

## Workflow

1. Create/edit plugin in `plugins/<name>/`
2. Update `plugin.json` version if changed
3. Run `python3 scripts/build_registry.py`
4. Commit all files including `registry.json`
