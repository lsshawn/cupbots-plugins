# mdpubs

Publish and manage markdown notes on mdpubs.com

## Commands
- `/publish` (WRITE)
- `/published`

## Intent
USE FOR: Publish markdown notes to mdpubs.com

## Primitives
```
/publish <title> -- <body> — Publish a markdown note to mdpubs.com
/publish <title> — Publish (body via reply or empty)
/published — List recent published notes
/publish My Report — # Heading
/publish My Report #finance — body text
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
