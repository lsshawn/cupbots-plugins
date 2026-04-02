---
name: chunker
description: RAG document search via Chunker API. Upload PDFs/DOCX/TXT, semantic search across documents, retrieve chunks with metadata. Use this skill whenever you need to search through large documents.
---

# Instructions

You are a RAG search assistant powered by the Chunker API. Use these tools to upload documents, search them semantically, and retrieve relevant text chunks.

## Configuration

The Chunker API requires two environment variables:
- `CHUNKER_URL` — Base URL of the Chunker API (e.g. `http://localhost:3000` or production URL)
- `CHUNKER_API_KEY` — API key prefixed with `ck_`

All requests must include the header: `X-API-Key: <CHUNKER_API_KEY>`

## Core Operations

### 1. Semantic Search (most common)

Search across all uploaded documents with a natural language query:

```bash
curl -s -X POST "$CHUNKER_URL/chunks/query" \
  -H "X-API-Key: $CHUNKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "your search query here",
    "limit": 10,
    "include_metadata": true
  }'
```

**Parameters:**
- `query` (required) — Natural language search query
- `limit` (optional, default 10, max 100) — Number of results
- `filter` (optional) — Metadata filter: `{"country":"SG"}`, `{"year":{"$gte":2024}}`, `{"country":{"$in":["SG","MY"]}}`
- `rerank` (optional, boolean) — LLM re-ranking for nuanced queries (slower, more accurate)
- `score_threshold` (optional, 0-1) — Minimum similarity score. Recommended: 0.7
- `dedupe_document` (optional, boolean) — One result per document
- `include_metadata` (optional, boolean, default true) — Include page numbers and custom metadata

**Response shape:**
```json
{
  "query": "...",
  "reranked": false,
  "chunks": [
    {
      "content": "The actual text chunk...",
      "score": 0.91,
      "document_id": 9,
      "chunk_index": 14,
      "filename": "report.pdf",
      "metadata": { "pageStart": 42, "pageEnd": 43, "country": "SG", "year": 2025 }
    }
  ]
}
```

### 2. Batch Search (multiple queries at once)

Run up to 50 queries in parallel:

```bash
curl -s -X POST "$CHUNKER_URL/chunks/batch-query" \
  -H "X-API-Key: $CHUNKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "queries": [
      { "query": "revenue growth", "limit": 5, "filter": { "year": 2025 } },
      { "query": "carbon emissions", "limit": 5, "rerank": true }
    ]
  }'
```

### 3. Upload a Document

```bash
curl -s -X POST "$CHUNKER_URL/documents/upload" \
  -H "X-API-Key: $CHUNKER_API_KEY" \
  -F "file=@/path/to/document.pdf" \
  -F 'metadata={"country":"SG","year":2025}'
```

Add `-F "target_language=en"` to translate non-English documents before chunking.

**Supported formats:** PDF, DOCX, DOC, TXT

The response returns a document ID. Processing happens in the background. Check status with:

```bash
curl -s "$CHUNKER_URL/documents/<ID>" -H "X-API-Key: $CHUNKER_API_KEY"
```

Statuses: `pending` → `processing` → `completed` | `failed`

### 4. List Documents

```bash
curl -s "$CHUNKER_URL/documents" -H "X-API-Key: $CHUNKER_API_KEY"
curl -s "$CHUNKER_URL/documents?status=completed" -H "X-API-Key: $CHUNKER_API_KEY"
```

### 5. Get Full Document Text

```bash
curl -s "$CHUNKER_URL/documents/<ID>?include_text=true" -H "X-API-Key: $CHUNKER_API_KEY"
```

### 6. Delete Document

```bash
curl -s -X DELETE "$CHUNKER_URL/documents/<ID>" -H "X-API-Key: $CHUNKER_API_KEY"
```

## Best Practices

1. **Always use `include_metadata: true`** — page numbers are essential for citations
2. **Use `rerank: true`** for precision queries like compliance checks or specific disclosure questions
3. **Use `score_threshold: 0.7`** to filter out low-relevance noise
4. **Use batch search** when you need to answer multiple questions from the same document set
5. **Use filters** to narrow search to specific countries, years, or sources
6. **Use `dedupe_document: true`** when you want breadth across documents rather than depth in one
7. **Check document status** after upload before searching — wait for `completed`
8. **Cite sources** — always include filename, page numbers, and similarity score in your output
