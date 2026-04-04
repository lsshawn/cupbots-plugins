#!/usr/bin/env python3
"""Build registry.json from individual plugin.json files.

Run after merging a plugin PR to regenerate the registry:
  python plugin-registry/scripts/build_registry.py
"""
import json
from pathlib import Path

REGISTRY_DIR = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REGISTRY_DIR / "plugins"


def build():
    registry = {"version": 1, "api": "https://api.cupbots.dev", "plugins": {}}

    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir():
            continue
        meta_file = plugin_dir / "plugin.json"
        if not meta_file.exists():
            continue

        meta = json.loads(meta_file.read_text())
        name = meta["name"]

        # Skip draft/unpublished plugins
        if meta.get("status") == "draft":
            print(f"  Skipping {name} (draft)")
            continue

        # Collect all files in the plugin directory (except plugin.json)
        files = []
        for f in sorted(plugin_dir.iterdir()):
            if f.name == "plugin.json":
                continue
            if f.suffix == ".py":
                files.append(f"plugins/{f.name}")
            elif f.suffix == ".js":
                files.append(f"pb_migrations/{f.name}")

        meta["files"] = files
        registry["plugins"][name] = meta

    out = REGISTRY_DIR / "registry.json"
    out.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"Built registry with {len(registry['plugins'])} plugins -> {out}")


if __name__ == "__main__":
    build()
