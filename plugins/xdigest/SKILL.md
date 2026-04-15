# xdigest

AI market intelligence from your X/Twitter lists — product signals, business ideas, distribution levers

## Commands
- `/xdigest`

## Intent
USE FOR: Get AI market intelligence digest from X/Twitter lists

## Primitives
```
/xdigest — Show help
/xdigest run — Run digest now (all configured lists)
/xdigest run <list_id> — Run digest for one specific list
/xdigest cookies — Paste plaintext cookies.txt to set up X auth
/xdigest lists — Show configured list IDs
/xdigest history — Show recent digests
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Supports multiple X list IDs — configure via xdigest_list_ids (YAML list)
- Cookies: user pastes raw Netscape cookies.txt (from browser extension), plugin auto-converts to Playwright JSON
- Legacy xdigest_list_id (single string) still works for backwards compat
