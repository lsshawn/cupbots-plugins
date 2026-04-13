# ytdigest

AI-powered YouTube video analysis with timestamped insights — processes single videos or entire RSS feeds

## Commands
- `/ytdigest`

## Intent
USE FOR: Process YouTube RSS feeds or channels for batch video analysis

## Primitives
```
/ytdigest <url> — Analyze a single YouTube video
/ytdigest run — Process unread videos from Miniflux RSS
/ytdigest history — Show recent analyses
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
