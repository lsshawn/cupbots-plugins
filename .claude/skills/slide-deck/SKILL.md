---
name: slide-deck
description: Create structured slide deck content from uploaded documents using Chunker RAG search. Extracts key insights, organizes them into slides, and produces presentation-ready content with citations.
---

# Instructions

You create slide deck content by searching through documents and producing structured, presentation-ready content with proper citations.

## Prerequisites

This skill depends on the **chunker** skill for all document search operations. Use the chunker skill's search patterns (single query, batch query, list documents) to retrieve content. Do not hardcode API details here — the chunker skill is the single source of truth for how to interact with the Chunker API.

**Before starting:** Verify `CHUNKER_URL` and `CHUNKER_API_KEY` environment variables are set.

## Workflow

### Step 1: Understand the Brief

Ask the user for:
1. **Topic/title** of the presentation
2. **Audience** — who is this for? (board, investors, team, public)
3. **Key questions** to answer (or let the user provide a list)
4. **Document filter** — specific countries, years, sources to focus on (optional)
5. **Slide count target** — how many slides (default: 10-15)

### Step 2: Research via Chunker Skill

Use the **chunker** skill's batch search to pull relevant content for all key questions at once.

**Important search settings for slide decks:**
- Always use `rerank: true` — precision matters more than speed for presentations
- Use `include_metadata: true` — you need page numbers for citations
- Use `limit: 5` per query — enough depth without noise
- Apply metadata filters if the user specified country/year/source constraints

### Step 3: Analyze and Organize

After retrieving chunks:
1. **Group by theme** — cluster related chunks across documents
2. **Extract key data points** — numbers, percentages, trends, quotes
3. **Identify narrative arc** — what story do the documents tell?
4. **Flag gaps** — are there questions the documents don't answer?

### Step 4: Generate Slide Deck Content

Output a structured slide deck in this format:

```markdown
# [Presentation Title]
**Audience:** [target audience]
**Sources:** [number] documents, [number] chunks analyzed

---

## Slide 1: [Title]
**Type:** Title Slide

- Main title
- Subtitle / date / presenter placeholder

---

## Slide 2: [Title]
**Type:** Key Finding / Data / Quote / Comparison

**Content:**
- Bullet point with key insight
- Supporting data point (X% increase in Y)
- Context or comparison

**Speaker Notes:**
Additional context the presenter should know.

**Sources:**
- [filename], p. [pageStart]-[pageEnd] (score: [similarity])

---
```

### Slide Types to Use

| Type | When to Use |
|------|-------------|
| **Title** | Opening slide |
| **Executive Summary** | 3-5 bullet overview |
| **Key Finding** | Single insight with supporting data |
| **Data Highlight** | Numbers, trends, percentages |
| **Comparison** | Side-by-side (e.g., year-over-year, country vs country) |
| **Quote** | Direct quote from document with attribution |
| **Deep Dive** | Detailed breakdown of one topic |
| **Recommendations** | Action items derived from findings |
| **Appendix** | Source list, methodology notes |

### Step 5: Review and Refine

After generating the deck:
1. Check that every slide has at least one source citation
2. Verify data points are accurately quoted from chunks
3. Ensure the narrative flows logically from slide to slide
4. Offer to expand, condense, or restructure any section

## Output Options

The user can request output as:
- **Markdown** (default) — structured markdown as above
- **JSON** — machine-readable slide objects for programmatic use
- **Outline** — condensed bullet-point outline for quick review

## Best Practices

1. **Never fabricate data** — every number must come from a retrieved chunk
2. **Always cite sources** — filename + page numbers on every content slide
3. **Use batch queries** — research all topics in one API call for efficiency
4. **Prefer reranked results** — use `rerank: true` for all slide deck queries
5. **Balance depth and breadth** — use `dedupe_document: true` for overview slides, remove it for deep dives
6. **Flag low-confidence results** — if the best chunk scores below 0.7, note that the topic may not be well-covered in the source documents
7. **Suggest follow-up searches** — if the initial results reveal related topics worth exploring
