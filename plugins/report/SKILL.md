# report

Generate consultant-grade PDF reports from wiki content or uploaded documents

## Commands
- `/report` (WRITE)

## Intent
USE FOR: Create, draft, build, edit, or preview professional consultant-grade reports (sustainability, annual, ESG). Generate PDF from wiki content or uploaded markdown/PDF/DOCX. Tweak sections with plain language. Change brand palette or signatory title.
NOT FOR: Sending documents via email, creating calendar events, editing wiki entities directly

## Primitives
```
/report create --title X --workspace W [--signatory "Managing Director"] [--palette green]
/report create --title X --file /path/to/source.pdf
/report create --title X --from-attachment
/report draft --id <id>                    — synthesise sections from source
/report build --id <id>                    — render HTML + PDF, register preview
/report tweak --id <id> --section SEC --instruction "make it punchier"
/report palette --id <id> --primary #2E7D32 --accent #C8A951 --dark #37474F
/report signatory --id <id> --title "Group Managing Director"
/report status --id <id>
/report list
/report preview --id <id>                  — re-emit the hosted preview URL
/report show --id <id>                     — dump current markdown in chat
/report archive --id <id>                  — soft-delete (hide from list)
/report restore --id <id>                  — undo archive
/report delete --id <id>                   — permanent delete
/report list [--all]                       — list reports (--all includes archived)
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
