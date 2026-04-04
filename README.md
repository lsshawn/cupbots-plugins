# CupBots Plugins (Private)

Private repo containing plugin **source code** for [CupBots](https://github.com/lsshawn/cupbots). Marketplace metadata lives in the public [cupbots-plugins-registry](https://github.com/lsshawn/cupbots-plugins-registry).

## Two-Repo Architecture

```
cupbots-plugins (this repo, PRIVATE)     cupbots-plugins-registry (PUBLIC)
├── plugins/                              ├── plugins/
│   ├── note/                             │   ├── note/
│   │   ├── note.py        ← source      │   │   ├── plugin.json  ← metadata
│   │   └── plugin.json                   │   │   └── screenshot.png
│   └── ...                               │   └── ...
├── registry.json (with file paths)       ├── registry.json (no file paths)
└── scripts/                              └── scripts/
    └── build_registry.py                     ├── build_registry.py
                                              └── sync_from_private.py
```

- **This repo**: plugin `.py` files + `plugin.json` with core metadata
- **Public registry**: `plugin.json` with marketplace fields (tagline, icon, category) + assets (screenshots)
- Source code is never exposed publicly. Downloads are gated by the API.

## Structure

```
plugins/
├── note/
│   ├── note.py             # Plugin code
│   └── plugin.json         # Core metadata
├── mdpubs/
│   ├── mdpubs_plugin.py
│   ├── 1712000001_mdpubs_notes.js  # PocketBase migration
│   └── plugin.json
└── ...
```

## Adding a Plugin

1. Create a directory under `plugins/` with your plugin name
2. Add your `.py` file and `plugin.json`
3. Run `python3 scripts/build_registry.py` to regenerate `registry.json`
4. Commit all files including the updated `registry.json`
5. Also submit marketplace metadata (tagline, icon, screenshot) to [cupbots-plugins-registry](https://github.com/lsshawn/cupbots-plugins-registry)

## Syncing Metadata to Public Registry

To sync core fields from this repo to the public registry (preserving marketplace fields):

```bash
cd /path/to/cupbots-plugins-registry
python3 scripts/sync_from_private.py /path/to/cupbots-plugins
python3 scripts/build_registry.py
```

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

## Plugin Development Guide

See [DEVELOPER.md](https://github.com/lsshawn/cupbots/blob/main/DEVELOPER.md) in the main framework repo.
