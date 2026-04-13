# Report Plugin Architecture

## What this solves

Turn markdown/PDF/DOCX source documents into Fortune 500-grade print-ready PDF reports — the kind PwC or KPMG would publish as a sustainability statement or annual report. Users create, edit, and preview reports entirely from WhatsApp or email, in plain language.

## How it works (our approach)

One long HTML document + CSS Paged Media (`@page`) + a single Playwright `page.pdf()` call.

```
Source (wiki / uploaded file)
    ↓
Per-section LLM synthesis → frozen HTML sections (character-identical to source)
    ↓
Jinja2 renders ONE report.html (all sections, cover, TOC, back cover)
    ↓  design_system.css.tmpl provides @page rules + 23 CSS components
    ↓
Playwright page.pdf() → A4 PDF (browser handles all pagination)
    ↓
Hub serves /r/<token> → PDF iframe + "Request Edit" textbox
```

The browser is the typesetter. `@page` rules control margins, running footers (company name + page number), full-bleed hero pages. `break-inside: avoid` keeps tables and cards on one page. `orphans: 3; widows: 3` prevents paragraph orphans. The LLM never thinks about pagination — it writes HTML, the browser flows it across pages.

**Engine files (~400 lines total):**

| File | Purpose |
|------|---------|
| `design_system.css.tmpl` | Parameterised CSS — brand palette as Jinja vars, `@page` rules, 23 components |
| `report.html.tmpl` | Master template — cover, TOC, section loop, back cover |
| `pdf.py` | 30 lines — Playwright `page.pdf()` |
| `qc.py` | CSS class lint against `components.json`, text diff vs source |
| `extract.py` | Text extraction for MD/PDF/DOCX |
| `demo.py` | Pre-built 5-section demo (no LLM) |
| `pipeline.py` | `build_report(spec)` orchestrator |
| `components.json` | Authoritative list of 87 allowed CSS class names |

## How the prototype worked (what we replaced)

The original prototype at `ai-dr-prototype/` used a **per-page rendering pipeline**:

```
Markdown sections
    ↓
6-8 parallel LLM agents → 56 individual HTML files (page-01.html ... page-56.html)
    ↓  each page is a fixed 1587×2245px viewport with position:absolute layout
    ↓
typeset.py: render each page at height:auto, measure scrollHeight
    ↓  16 of 35 pages overflowed → split at paragraph boundaries
    ↓  page-18.html → page-18.html + page-18b.html
    ↓
renumber_pages.py: remove b/c suffixes, renumber sequentially
    ↓
render.py: Playwright batch renders each HTML → PNG (1587×2245)
    ↓
Gemini: renders 8 hero pages (cover, dividers, dashboard) → PNG
    ↓
assemble-pdf.py: PIL merges all PNGs into final PDF
    ↓
57-page PDF (14.1 MB, raster — text is burned into images)
```

**Prototype files (replaced):**

| File | Lines | What it did |
|------|-------|-------------|
| `typeset.py` | ~400 | Overflow detection + page splitting algorithm |
| `render.py` | ~100 | Playwright batch PNG renderer (one launch per page) |
| `assemble-pdf.py` | ~60 | PIL Image.save with append_images |
| `renumber_pages.py` | ~400 | Two-phase rename to fix b/c suffixes |
| `design-system.css` | ~830 | Hard-coded for Geohan, fixed 1587×2245 viewport |
| 56 HTML files | ~950 each | One per page, manually paginated |

## Key differences

| Aspect | Prototype | Our approach |
|--------|-----------|--------------|
| **Pagination** | Manual: typeset.py detects overflow, splits pages, renumbers | Automatic: browser's `@page` CSS handles it |
| **Rendering** | Per-page: 56 HTML → 56 PNG → PIL merge | Single call: one HTML → `page.pdf()` |
| **Output format** | Raster PDF (PNGs stitched, 14 MB) | Vector PDF (text is text, 273 KB for 16 pages) |
| **Hero pages** | Gemini-generated PNGs (cover, dividers) | Pure CSS: `background-image` + gradient overlay + typography |
| **Editability** | Edit requires re-rendering affected PNGs + re-stitching | Edit the HTML, re-run `page.pdf()`. Text stays editable in the PDF. |
| **CMYK path** | Impossible — PNGs are RGB, no clean conversion point | Ghostscript post-process on the vector PDF |
| **Engine size** | ~1800 lines + 56 HTML files | ~400 lines, no per-page files |
| **Agents needed** | 6-8 parallel content agents + 1 hero agent + 1 assembly agent | 1 synthesis call per section (10 calls), no pagination agents |
| **Token cost** | ~150-250K tokens per report | ~50-100K tokens (no pagination prompts, no multi-agent coordination) |
| **Build time** | ~2.5 hours (LLM-bound) | ~2 seconds for demo (no LLM), ~5-15 min for full synthesis |
| **Dependencies** | Playwright + PIL + pdfplumber | Playwright + Jinja2 + pdfplumber (no PIL) |
| **Mobile viewing** | PDF only (raster, large) | v1: PDF iframe. v2: same HTML with `@media screen` responsive CSS |

## Why it's better

**The prototype solved a self-inflicted problem.** It rendered each page as an isolated HTML file with a fixed viewport, which meant the browser couldn't flow content across pages. So the team had to build their own typesetter (`typeset.py`) to detect overflow and split pages at natural break points. This was the most complex and fragile part of the prototype — 16 of 35 Geohan pages overflowed on the first pass.

CSS Paged Media (`@page`) has been supported in Chrome since 2020. It does exactly what `typeset.py` did, except it's maintained by the Chromium team, handles edge cases (orphans, widows, table header repetition, break-inside avoidance), and costs zero tokens. The entire overflow-detection problem disappears.

**The Gemini gap is eliminated.** The prototype used Gemini to render hero pages (cover, section dividers, dashboard visualisations) as PNGs. This created a one-way door: once the image was burned, you couldn't edit the text on the cover without re-generating the entire image. The prototype's own hard rules already banned AI-generated people (culturally inappropriate for Malaysian PLCs), which removed the most useful Gemini capability. Our hero pages are pure CSS — `background-image` with real client photos + gradient overlay + DM Serif Display typography. Editable, deterministic, free.

**Vector vs raster.** The prototype's PDF was a stack of PNGs — text was image pixels, not selectable/searchable text. File size was 14 MB for 57 pages. Our PDF is a real vector document: text is text, colours are named values, file size is 273 KB for 16 pages. This is the correct input for CMYK print conversion (Ghostscript can convert it directly) and for accessibility (screen readers can read the text).

## What we kept from the prototype

- **Design system CSS** — the 23 component classes (metric-card, data-table, quote-panel, target-card, etc.) are carried over verbatim. They're proven on the Geohan report.
- **Character-identical constraint** — the LLM must copy source text verbatim into HTML. No summarising, no rephrasing. QC enforces this via text diff.
- **CSS class discipline** — agents may only use classes from `components.json`. QC lints for undefined classes after every build.
- **Print DPI scaling** — font sizes are 1.85x screen equivalent for readability at A4 print resolution.
- **Content-to-page separation** — the design phase does not alter content. Content is frozen before rendering begins.
- **Real photos only** — no AI-generated imagery. Client photos as CSS `background-image`.

## CupBots integration

The engine is a self-contained Python package with zero CupBots imports. The plugin wrapper (`report.py`) handles:

- **WhatsApp commands**: `/report create|draft|build|tweak|palette|signatory|status|list|preview|show|archive|restore|delete|demo`
- **Write-command orchestrator**: natural language → flag-style primitives via `resolve_write_intent()`
- **Wiki pull**: semantic search per section → per-section LLM synthesis
- **File upload**: MD/PDF/DOCX via `--file` or `--from-attachment`
- **AgentMail edits**: subscribe to `email.received`, match `[report:ID]` in subject, queue + apply via background job, reply with preview URL
- **Hub preview**: PDF cached on hub at `/r/<token>`, viewable offline, editable via textbox
- **Learned notes**: `run_agent_loop()` auto-injects user preferences for the tweak flow
