---
name: llms-generator
description: Generate an SEO and AEO-optimized llms-full.txt from your SvelteKit frontend pages.
tools: Read, Grep, Glob, AskUserQuestion
model: claude-opus-4-5
---

## Overview

This skill scans your SvelteKit routes and components to extract:
- Project name and description
- Brand voice and messaging patterns
- Product features and value propositions
- Social proof, metrics, and testimonials

The output is a structured llms-full.txt file optimized for LLM consumption and search engine understanding.

## Instructions

When this skill is invoked:

1. **Scan Frontend Routes** (`apps/frontend/src/routes/`)
   - Read all `+page.svelte` files recursively
   - Extract text content from:
     - Hero sections (h1, h2, main headlines)
     - Feature descriptions
     - Value propositions
     - Call-to-action text
     - Meta descriptions and titles

2. **Scan Key Components** (`apps/frontend/src/lib/components/`)
   - Marketing components (Hero, Features, Pricing, Testimonials)
   - Layout components with branding
   - Any component with substantive product copy

3. **Extract Structured Data**
   - **Project Name**: Look for:
     - Site titles in `app.html` or layout files
     - Logo text or brand names
     - Company references in footer

   - **Description**: Extract from:
     - Hero section taglines
     - Homepage main copy
     - About page content
     - Meta descriptions

   - **Brand Voice**: Analyze:
     - Tone patterns (formal vs casual, technical vs accessible)
     - Common phrases and terminology
     - Voice consistency across pages
     - Use of emojis, punctuation style

   - **Product Brief**: Compile:
     - Feature lists and descriptions
     - Product capabilities
     - Use cases and benefits
     - Technical highlights
     - Pricing information

   - **Proof Stats**: Gather:
     - Testimonial quotes and attributions
     - Usage statistics (users, companies, etc.)
     - Achievement metrics
     - Case study highlights
     - Awards or recognition

4. **Generate llms-full.txt**
   - Create file at project root called `./docs/lms-full.txt`
   - Structure with clear sections:
     ```
     # [Project Name]

     ## Overview
     [Comprehensive description]

     ## Brand Voice & Positioning
     [Voice guidelines and tone]

     ## Product Details
     [Features, capabilities, value proposition]

     ## Social Proof & Metrics
     [Statistics, testimonials, achievements]

     ## SEO Keywords
     [Extracted primary and secondary keywords]

     ## Target Audience
     [Inferred user personas and use cases]
     ```

5. **SEO/AEO Optimization**
   - Use semantic HTML-like structure for LLM parsing
   - Include keyword-rich summaries
   - Add structured data markers
   - Optimize for question-answering (what, why, how, who)
   - Include common search intent patterns
   - Add related concepts and entities

6. **Validation**
   - Ensure all sections have content
   - Flag any missing critical data
   - Suggest pages to review if content is sparse
   - Verify brand voice consistency

## Output

The skill generates:
1. `./docs/llms-full.txt` - The main SEO context file
2. A summary report showing:
   - Pages scanned
   - Sections populated
   - Content completeness score
   - Recommendations for improvement

## Example Usage

```bash
# User invokes the skill
/seo-context-generator

# Or with custom output path
/seo-context-generator --output ./docs/llms-full.txt
```

## Notes

- Prioritizes user-facing marketing copy over technical implementation details
- Preserves brand voice authenticity by using actual site copy
- Optimized for both traditional SEO and AI-powered search (AEO)
- Can be re-run after content updates to refresh the context file
