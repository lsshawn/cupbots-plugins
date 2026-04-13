# heartbeat

Heartbeat Agent

## Commands
- `/think`
- `/heartbeat`
- `/snapshot`

## Primitives
```
/think [question] — Run the agent now (optional: ask it something specific)
/heartbeat — Show when the next heartbeat runs
/snapshot — Record net worth snapshot now (auto-runs 1st of month)
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
