# bookmarks

Save, search, and manage bookmarked links

## Commands
- `/bookmark` (WRITE)
- `/bookmarks`
- `/unbookmark` (WRITE)

## Intent
USE FOR: Save, search, or manage bookmarked URLs and links

## Primitives
```
/bookmark <url> [#tag] — Save a link (auto-fetches title)
/bookmark <url> <title> [#tag] — Save with custom title
/unbookmark <url_or_text> — Remove a bookmark by URL or keyword
/bookmarks — Show last 5 unread bookmarks
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- For write commands, use the EXACT flag syntax from Primitives above
- Respect explicit timezones in user input
