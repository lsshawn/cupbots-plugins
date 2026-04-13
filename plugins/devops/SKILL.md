# devops

Run and monitor data pipelines.

## Commands
- `/devops`

## Primitives
```
/devops — Show help
/devops run <pipeline> [args] — Run a pipeline (reddit, x, youtube)
/devops status — Check if pipelines ran today
/devops schedule — Show scheduled pipeline times
/devops logs [N] [error|warning] — Recent error/warning logs
/devops run reddit — skip-scrape
/devops run youtube — limit 3
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
