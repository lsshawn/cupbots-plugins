# search

Multi-platform research across Reddit, YouTube, and X

## Commands
- `/search`
- `/searchreddit`
- `/searchyt`
- `/searchx`

## Intent
USE FOR: Research a topic across Reddit, YouTube, and X/Twitter

## Primitives
```
/search <query> — Search all platforms
/search <query> reddit youtube — Search specific platforms
/searchreddit <query> — Reddit only
/searchyt <query> — YouTube only
/searchx <query> — X/Twitter only
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
