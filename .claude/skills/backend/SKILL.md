---
name: backend
description: Hono.js + Mastra + Drizzle (Turso) specialist. Enforces modular architecture and Zod-driven API design.
---

# Instructions

## 1. Modular Hono Architecture
- **Strict Structure:** Always organize features into `src/modules/{feature_name}/`.
  - `routes.ts`: Hono routes, input validation (Zod), and HTTP responses.
  - `services.ts`: Business logic, Mastra agent calls, and Drizzle DB queries.
- **Entry Point:** Register all modules in the root `src/index.ts` using `app.route()`.

## 2. API & Logic Patterns
- **Zod Validation:** Use `hono/zod` middleware for all `POST`/`PUT`/`PATCH` requests. Define schemas at the top of `routes.ts`.
- **Direct Drizzle:** Skip the Repository Pattern. Call `db` directly inside `services.ts` for speed. 
- **N+1 Prevention:** Use Drizzle's `relational` queries or batch IDs for related data. Avoid loops containing DB calls.
- **Error Handling:** Use Hono's `c.json()` with standard HTTP codes. Log using: `[LS] -> filepath:line -> variable`.

## 3. Mastra & AI Integration
- **Service-Owned Agents:** Keep Mastra agent definitions inside the `services.ts` of the relevant module.
- **Tooling:** Define Zod schemas for all Mastra tools to ensure strict AI-to-Code interface.
- **Streaming:** Use Hono's `streamText` or `streamSSE` for LLM responses to improve UX.

## 4. Database (Turso + Drizzle)
- **Source of Truth:** All schema changes must happen in `packages/db/src/schema.ts`.
- **Transactions:** Use `db.transaction()` for multi-table inserts (e.g., creating a user and their initial settings).

# Example Module (routes.ts)
```typescript
import { Hono } from 'hono';
import { zValidator } from '@hono/zod-validator';
import { z } from 'zod';
import { billingService } from './services';

const app = new Hono();
const schema = z.object({ amount: z.number() });

app.post('/charge', zValidator('json', schema), async (c) => {
  const { amount } = c.req.valid('json');
  const result = await billingService.process(amount);
  return c.json(result);
});

export default app;
