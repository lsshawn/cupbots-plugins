# wiki

AI-maintained second brain — knowledge synthesis, personal CRM, notes, ideas, bookmarks, FAQ, and WhatsApp chat digest.

## Commands
- `/wiki`
- `/crm` (routes to wiki)
- `/note` (routes to wiki)
- `/idea` (routes to wiki)
- `/ideas` (routes to wiki)
- `/bookmarks` (routes to wiki)
- `/faq` (routes to wiki)

## Intent
USE FOR: Search wiki, find entity info, ingest documents, manage knowledge base, look up people or companies, personal CRM (whois, remember, overdue contacts, birthdays), notes and ideas, bookmarks, FAQ entries, WhatsApp chat digest
NOT FOR: RAG document search (/knowledgebase), calendar events (/cal), reminders (/remind), sending messages (/wa)

## Primitives
```
/wiki                                  — Help
/wiki workspaces                       — List workspaces with stats
/wiki ingest [file] [--workspace W]    — Scan sources, ingest new/changed files
/wiki search <query>                   — Semantic search across entities
/wiki entities [--workspace W]         — List all entities by type
/wiki show <entity>                    — Display entity markdown
/wiki query <question>                 — Ask a question, get synthesized answer with citations
/wiki lint                             — Health check (orphans, stale, broken links)
/wiki log [--limit N]                  — Show recent actions
/wiki sync                             — Re-sync workspaces from config.yaml
/wiki digest                           — Run WhatsApp chat digest now (all chats)
/wiki digest <name>                    — Digest for one contact only
/wiki digest on|off                    — Enable/disable weekly auto-digest
/wiki digest status                    — Show digest schedule

/crm                                   — Overdue contacts
/crm whois <name>                      — Look up a person
/crm remember <name> -- <note>         — Add a fact about someone
/crm touched <name> [note]             — Log an interaction
/crm birthday <name> <date>            — Set birthday (YYYY-MM-DD)
/crm list                              — List all people

/note <title> [-- body]                — Create a note (raw, no LLM)
/note list [#tag]                      — List notes
/note save <url> [title] [#tag]        — Bookmark a link
/note unsave <url or keyword>          — Remove a bookmark
/idea <description>                    — Capture an idea
/ideas                                 — List ideas
/bookmarks                             — List bookmarks

/faq <query>                           — Search FAQ
/faq add <question> | <answer>         — Add Q&A pair
/faq remove <id>                       — Remove entry
/faq list                              — List entries
/faq auto on|off                       — Toggle auto-answer in group
```

## Examples
```
/wiki search API design                Search for entities about API design
/wiki query "What do we know about Acme Corp?"
/wiki digest Sarah                     Digest WhatsApp chats with Sarah
/wiki digest on                        Enable weekly auto-digest
/crm whois Sarah                       Look up Sarah's entity
/crm remember Sarah -- met at TechConf, interested in API
/crm touched Sarah had coffee
/crm birthday Sarah 1990-03-15
/note meeting with client -- discussed pricing for Q3
/note save https://example.com/article #dev #rust
/idea build a CLI tool for wiki search
/faq add What is CupBots? | A multi-platform bot framework
/faq What is CupBots
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Use `--` or `—` to separate name from note in /crm remember
- Contacts are fuzzy-matched by name
- Notes, ideas, and bookmarks are stored as raw text (NO LLM call on create)
- LLM synthesis only happens on /wiki ingest or /wiki digest
- FAQ entries are per-chat (scoped to the group/DM where added)
