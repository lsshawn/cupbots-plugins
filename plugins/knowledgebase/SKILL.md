# knowledgebase

Upload documents and search them with AI — PDF, DOCX, TXT supported. Replaces FAQ with semantic search.

## Commands
- `/knowledgebase`
- `/kb`

## Intent
USE FOR: Search uploaded documents (PDF, DOCX) for answers using AI
NOT FOR: FAQ entries or personal notes

## Primitives
```
/knowledgebase upload — Upload a document (attach file or reply to one)
/knowledgebase search <query> — Search your knowledge base
/knowledgebase ask <question> — Search + AI-generated answer from results
/knowledgebase list — List uploaded documents
/knowledgebase delete <id> — Delete a document
/knowledgebase tag <id> <tags> — Tag a document (comma-separated)
/knowledgebase auto on|off — Auto-answer plain messages from knowledge base
/kb search <query> — Short alias for /knowledgebase search
/kb ask <question> — Short alias for /knowledgebase ask
/knowledgebase search refund — tag faq
```

## Rules
- Do NOT invent subcommands — only use commands listed above
- Respect explicit timezones in user input
