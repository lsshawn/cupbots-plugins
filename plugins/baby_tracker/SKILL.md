# baby_tracker

Baby Tracker

## Commands
- `/feed`
- `/sleep`
- `/diaper`
- `/milestone`
- `/stats`
- `/analyze`
- `/ask`
- `/undo`
- `/baby`

## Primitives
```
/feed <ml|left|right|pump> — Log feeding
/sleep — Toggle sleep start/stop
/diaper <wet|poop|both> — Log diaper
/milestone <text> — Log milestone
/stats — Today's summary
/analyze [days] — Chart for N days
/ask <question> — Ask Claude Code (continues session)
/undo — Delete last entry
/baby — Show baby commands
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
