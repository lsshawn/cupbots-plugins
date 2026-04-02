# CupBots Plugins

Official plugin marketplace for [CupBots](https://github.com/USER/cupbots). This repo contains all official plugins — both free bundled plugins and paid premium plugins.

## Structure

```
cupbots-plugins/
├── plugins/
│   ├── note/
│   │   ├── note.py         # Plugin code
│   │   └── plugin.json     # Metadata (name, version, commands, config, pricing)
│   ├── calendar/
│   │   ├── calendar_plugin.py
│   │   └── plugin.json
│   ├── mdpubs/
│   │   ├── mdpubs_plugin.py
│   │   ├── 1712000001_mdpubs_notes.js  # PocketBase migration
│   │   └── plugin.json
│   └── ...
├── registry.json           # Auto-generated — DO NOT edit manually
└── scripts/
    └── build_registry.py   # Generates registry.json from plugin.json files
```

## Adding a Plugin

1. Create a directory under `plugins/` with your plugin name
2. Add your `.py` file and `plugin.json`
3. If your plugin needs a DB table, add a PocketBase migration `.js` file
4. Run `python3 scripts/build_registry.py` to regenerate `registry.json`
5. Commit all files including the updated `registry.json`

## plugin.json Format

```json
{
  "name": "my_plugin",
  "description": "Short description for marketplace listing",
  "version": "1.0.0",
  "author": "Your Name",
  "commands": ["/mycmd"],
  "config": [],
  "free": true,
  "bundled": true
}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Plugin identifier (matches directory name) |
| `description` | Yes | One-line description for marketplace |
| `version` | Yes | Semver version string |
| `author` | Yes | Author name |
| `commands` | Yes | List of `/commands` the plugin registers |
| `config` | No | Config items users need to set (API keys, etc.) |
| `free` | No | `true` (default) or `false` for paid plugins |
| `bundled` | No | `true` if auto-installed on first boot |
| `price` | No | Price string for paid plugins (e.g. "$49") |
| `price_type` | No | `one_time` or `monthly` |
| `requires` | No | List of plugin dependencies |
| `migrations` | No | List of migration filenames |

### Config items

```json
{
  "key": "WEATHER_API_KEY",
  "label": "OpenWeather API Key",
  "type": "secret",
  "required": true,
  "help": "Get one at https://openweathermap.org/api"
}
```

## Rebuilding the Registry

After any change to plugin.json files:

```bash
python3 scripts/build_registry.py
```

This walks all `plugins/*/plugin.json`, collects file lists, and writes `registry.json`.

## Plugin Development Guide

See [DEVELOPER.md](https://github.com/USER/cupbots/blob/main/DEVELOPER.md) in the main framework repo for the full plugin development guide.
