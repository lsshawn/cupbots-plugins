# note

Personal knowledge hub — notes, ideas, and bookmarks under /note.

## Commands
- `/note`

## Primitives
```
/note <title> [-- body] — Create a zettelkasten note (#tags supported)
/note list — Show recent notes
/note idea <description> — Capture an idea
/note ideas — List this year's ideas
/note save <url> [title] [#tag] — Bookmark a link
/note bookmarks — List unread bookmarks
/note unsave <url or keyword> — Remove a bookmark
```

## Examples
```
/note Meeting recap -- discussed Q3 targets #work
/note Quick thought #random
/note list                          Recent notes
/note idea AI tool that auto-generates invoices
/note ideas                         This year's ideas
/note save https://example.com Great article #dev
/note bookmarks                     Unread bookmarks
/note unsave example.com            Remove bookmark
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Notes support #tags inline and `--` or `—` to separate title from body
- Reply to a message with `/note <title>` to use the quoted text as body
