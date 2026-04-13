# launchpad

Deploy landing pages from WhatsApp — design, iterate, and go live in minutes

## Commands
- `/lp` (WRITE)
- `/lp sites`
- `/lp edit` (WRITE)
- `/lp status`
- `/lp domain` (WRITE)

## Intent
USE FOR: Deploy and manage landing pages from WhatsApp

## Primitives
```
/lp <description> — Create a new landing page from a text prompt
/lp sites — List your deployed sites
/lp edit <site> — <change> — Edit an existing page (e.g. change headline)
/lp status <site> — Check deploy status and analytics link
/lp domain <site> <domain> — Connect a custom domain
/lp edit my-coffee — change the headline to "Fresh Roasted Daily"
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
