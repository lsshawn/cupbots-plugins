# CupBots Plugins (Private)

Private repo containing plugin **source code**. Marketplace metadata (tagline, icon, screenshots) lives in the public [cupbots-plugins-registry](https://github.com/lsshawn/cupbots-plugins-registry).

## Structure

Each plugin is a self-contained directory under `plugins/`:

```
plugins/<name>/
├── <name>.py           # Plugin code (required)
├── plugin.json         # Metadata (required)
└── SKILL.md            # Agent-facing docs (required) — read on demand by AI agent
```

## Key Rules

- Every plugin MUST have `handle_command(msg, reply) -> bool` and `register(app)`
- Every plugin MUST have a module docstring (used for `--help`)
- All data queries MUST be scoped by `msg.company_id` (tenant isolation)
- Use per-plugin SQLite via `get_plugin_db("plugin_name")` for storage
- Define `create_tables(conn)` for auto-init of tables on first DB access
- Plugin code uses `from cupbots.helpers.X import Y` imports
- For LLM calls, use `ask_llm()` (provider-agnostic, reads `ai.api_provider` config). Do NOT use `run_claude_cli()` for simple parsing — it's 10-100x more expensive
- Third-party pip packages must be declared in `plugin.json` → `"pip_dependencies": ["pkg"]`
- Every plugin MUST have a `SKILL.md` with exact command syntax, examples, and rules. The AI agent reads this on demand before calling commands (progressive disclosure). Generate seed files: `python scripts/generate_skill_md.py`
- After changing any `plugin.json`, run `python3 scripts/build_registry.py`
- Never edit `registry.json` manually — it's auto-generated

## Plugin Template

See `_template.py` in the main cupbots framework repo: `cupbots/plugins/_template.py`

## Workflow

1. Create/edit plugin in `plugins/<name>/`
2. Update `plugin.json` version if changed
3. Run `python3 scripts/build_registry.py`
4. Commit all files including `registry.json`
5. Sync metadata to public registry: `cd ../cupbots-plugins-registry && python3 scripts/sync_from_private.py ../cupbots-plugins && python3 scripts/build_registry.py`
6. Commit and push the public registry (triggers Vercel landing page rebuild)
