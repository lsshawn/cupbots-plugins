---
name: frontend
description: Svelte 5 (Runes) + SvelteKit specialist. Enforces Context-state, DaisyUI/Tailwind 4 variables, and high-end indie aesthetics.
---

This skill guides creation of distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implement real working code with exceptional attention to aesthetic details and creative choices.

The user provides frontend requirements: a component, page, application, or interface to build. They may include context about the purpose, audience, or technical constraints.

## SvelteKit (Svelte 5) & State Management Patterns

### Svelte 5 Runes & Logic
- **Strictly Runes:** Use `$state`, `$derived`, and `$props`. Never use `let` or `onMount` for reactivity.
- **No `$effect`:** Synchronization must happen via `$derived` or event handlers. `$effect` is for window/third-party libs only.
- **Context Pattern:** Use the "Set Once, Get Many" class pattern. No global stores.
  - **Class:** `export class EntityState { value = $state(null); constructor(init) { this.value = init; } }`
  - **Wiring:** `setContext(KEY, new EntityState(data))` in `+layout.svelte`; `getContext(KEY)` in children.
- **Data:** Use SvelteKit Server Functions/Actions for mutations.
- **Remote Functions:** Use SvelteKit's Server Functions/Actions for data mutations to keep logic on the server and minimize `fetch` boilerplate.

## Remote Functions
Use `*.remote.ts` files for server logic called from components.
- **`query()`:** For reads. Supports batching. Use with `<svelte:boundary>` and `{@const await data}` to prevent flicker. Use `data.refresh()` for polling.
- **`command()`:** For writes (updates/deletes).
- **`form()`:** For progressive enhancement.
- **Pattern:** Extract reactive dependencies first: `let slug = $derived(page.params.slug); let data = $derived(get_post({ slug }));`.
- **Constraint:** `getRequestEvent()` works for cookies, but `cookies.set()` in a `command()` won't propagate. Use client auth SDKs for cookie-based auth.

## Aesthetics & UI (Anti-AI Slop)
- **CSS Variables:** Define semantic tokens in `app.css` (e.g., `--brand-p`, `--card-bg`). Use `@apply` or DaisyUI classes (`btn-primary`). **Important: ** Do not use tailwind colors like `text-white`, `bg-[#ccc]` or `bg-gray-500` in the code. Use only those color vairiables in `app.css`
- **Typography:** If font-family exists in `app.css`, stick to that. Avoid Inter/system fonts. Use high-character display fonts (e.g., Cal Sans, Playfair, Mono) paired with a clean body font.
- **Icon:**: Use iconify icons. E.g. `import Icon from '@iconify/svelte' and then use it like `<Icon icon="ph:arrow-circle-down" />`. Prefer Phosphor icons.
- **Color:** Reuse the color variables in `app.css`. Don't come up with new theme and no generic "SaaS Blue" or purple/white gradients. Use OKLCH for vibrant, consistent colors.
- **Motion:** Use Svelte transitions or CSS staggered reveals. One high-impact page-load animation is better than many micro-interactions.

## UI Execution Guidelines
- Use **DaisyUI** components.
- **Layout:** Break the grid. Use asymmetry, negative space, or editorial-style typography. Minimalist. 
- **Page-based routing:** Always prioritize page-based routing and URL search parameters for UI state management. Every interactive element that changes the view—such as tabs, filters, pagination, or modals—must update the URL. Use the URL as the single source of truth; do not use local component state for any UI state that should shareable.
- **Indie Hacker Vibe:** Prioritize speed and "cool factor." Don't use a generic template.
- Avoid installing third-party libraries/components as much as possible.
- For date, use `datefns` and `cally`.

## Form best practice

### **1. Form Layout**

* **Single-Column Layout:** Use a vertical, single-column flow to prevent users from missing fields and to reduce cognitive load.
* **Logical Hierarchy:** Group related fields (e.g., contact info, payment) and order them from "easiest" to "hardest."
* **Top-Aligned Labels:** Place labels directly above input fields rather than to the left to ensure a faster, vertical scanning path.
* **Visual Grouping:** Use white space or section headers to separate different thematic sections.
* **Stepped Format:** Break long forms into multi-step screens with a stepper or progress indicator to prevent user overwhelm.
* **No UX shift:** Components especially buttons should not 

### **2. Field & Input Optimization**

* **Minimize Fields:** Only ask for essential information. If a field is optional, consider removing it.
* **Label Optional Fields:** Instead of an asterisk (*), explicitly write "(optional)" next to the label.
* **Avoid Field Splitting:** Use single input fields for data like phone numbers or credit cards instead of breaking them into multiple boxes.
* **Vertical Selection:** Arrange radio buttons and checkboxes vertically for better readability and touch-target accuracy.

### **3. UX Copy & Accessibility**

* **Concise Labels:** Use clear, brief labels. Provide "helper text" immediately below the field if the request needs clarification (e.g., "Why we need your phone number").
* **Action-Oriented Buttons:** Replace generic words like "Submit" with descriptive, action-based phrases like "Create My Account" or "Send Message."
* **First-Person Voice:** Consider using first-person language on buttons (e.g., "Start my free trial") to increase engagement.
* **No Placeholder Labels:** Never use placeholder text as the primary label. Placeholders disappear when typing and are often inaccessible to screen readers.

### **4. Validation & Error Handling**

* **Inline Validation:** Provide real-time feedback (e.g., a green check or red error) after a user finishes a field, rather than waiting until the end.
* **Clear Error Messages:** Ensure error messages are specific about *how* to fix the problem (e.g., "Password must be 8+ characters") rather than just saying "Invalid input."
* **Preserve Data:** If a form submission fails, never clear the fields. Let the user fix only the incorrect data.

### Context-Based State Management (Set Once, Get Many)

Use Svelte 5 classes with context for reliable, SSR-safe state management. **Never use global stores.** Follow the "set once in layout, get many in components" pattern.

**Template for State Class:**
```typescript
// src/lib/states/company.svelte.ts
import { getContext, setContext } from 'svelte';

export class CompanyState {
  value = $state<Company | null>(null);
  
  constructor(initial?: Company) { 
      this.value = initial ?? null; 
  }
  
  // Add methods for mutations
  update(company: Company) { 
      this.value = company; 
  }
}

const COMPANY_KEY = Symbol('COMPANY_CONTEXT');

export const setCompanyState = (initial?: Company) => 
    setContext(COMPANY_KEY, new CompanyState(initial));

export const getCompanyState = () => 
    getContext<CompanyState>(COMPANY_KEY);
```

**Set State Once in Root Layout:**
```svelte
<!-- src/routes/+layout.svelte -->
<script>
    import { setCompanyState } from '$lib/states/company.svelte';
    
    let { data, children } = $props();
    
    // Initialize state ONCE at top level
    setCompanyState(data?.selectedCompany);
</script>

{@render children()}
```

**Get State in Child Components/Pages:**
```svelte
<!-- src/routes/dashboard/+page.svelte -->
<script>
    import { getCompanyState } from '$lib/states/company.svelte';
    
    const companyState = getCompanyState();
</script>

<h1>{companyState.value?.name}</h1>
```

## Core Svelte Rules

### 1. **NEVER mutate `$state()` inside `$derived()`**

```svelte
<!-- ❌ WRONG -->
<script>
let count = $state(0);
let data = $derived.by(() => {
  count = 10; // ❌ NEVER DO THIS - causes state_unsafe_mutation error
  return someCalculation();
});
</script>

<!-- ✅ CORRECT -->
<script>
let count = $state(0);
let data = $derived.by(() => {
  // Only READ from state, never WRITE
  return someCalculation(count);
});
</script>
```

### 2. **`$derived()` is READ-ONLY - only for pure computations**

- ✅ DO: Read from `$state` variables
- ✅ DO: Call pure functions
- ✅ DO: Perform calculations
- ✅ DO: Return computed values
- ❌ DON'T: Mutate any `$state` variables
- ❌ DON'T: Perform side effects
- ❌ DON'T: Call functions that mutate state

### 3. **Template expressions are read-only**

```svelte
<!-- ❌ WRONG -->
<script>
let count = $state(0);
</script>
<div>{count = count + 1}</div> <!-- ❌ Don't mutate in templates -->

<!-- ✅ CORRECT -->
<script>
let count = $state(0);
let incremented = $derived(count + 1); // ✅ Use derived
</script>
<div>{incremented}</div>
```

### 4. **Use separate `$derived()` for each computed value**

```svelte
<!-- ❌ WRONG -->
<script>
let currentMonth = $state('January');

let days = $derived.by(() => {
  // ❌ Don't try to compute multiple values in one derived
  currentMonth = new Date().toLocaleDateString(); // ❌ MUTATION!
  return calculateDays();
});
</script>

<!-- ✅ CORRECT -->
<script>
// ✅ Separate derived for each computed value
let currentMonth = $derived(new Date().toLocaleDateString());
let days = $derived(calculateDays());
</script>
```

### 5. Snippet replaces slots

```svelte
<script>
  let { children, header } = $props();
</script>

{@render header?.()}
{@render children()}
```

---

## Common Patterns

### ✅ Correct: Calendar with display values
```svelte
<script>
let currentDate = $state(new Date());
let posts = $state([]);

// Separate derived values (read-only)
let monthDisplay = $derived(currentDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' }));
let calendarDays = $derived(calculateDays(currentDate, posts));

// Mutations in regular functions
function nextMonth() {
  currentDate = new Date(currentDate.setMonth(currentDate.getMonth() + 1));
}
</script>

<h2>{monthDisplay}</h2>
<button onclick={nextMonth}>Next</button>
```

### ✅ Correct: Filtering/sorting
```svelte
<script>
let items = $state([5, 2, 8, 1]);
let sortOrder = $state('asc');

let sortedItems = $derived.by(() => {
  const copy = [...items]; // Don't mutate original
  return sortOrder === 'asc'
    ? copy.sort((a, b) => a - b)
    : copy.sort((a, b) => b - a);
});
</script>
```

### ✅ Correct: Props with types
```svelte
<script lang="ts">
interface Props {
  isOpen: boolean;
  post: Post | null;
  businessType?: BusinessType;
  onSave?: () => void;
}

let {
  isOpen = $bindable(false),
  post = $bindable(null),
  businessType = 'b2b',
  onSave = () => {}
}: Props = $props();

// ✅ Derived based on props
let isValid = $derived(post !== null && businessType !== undefined);
</script>
```

---

## Error Prevention Summary

| Concept | Mutability | Use Case |
|---------|-----------|----------|
| `$state()` | ✅ Mutable | Reactive data that changes |
| `$derived()` | ❌ Read-only | Pure computed values |
| `$effect()` | ✅ Can mutate | Side effects, logging, API calls. Do not use $effect if an event-based function can replace it |
| Template expressions | ❌ Read-only | Display computed values |
| Regular functions | ✅ Can mutate | Event handlers, user actions |

**Golden Rule**: If you need to SET a value, don't do it in `$derived()`, templates, or `$inspect()`.

---

## TypeScript Integration

```svelte
<script lang="ts">
// ✅ Type your state
let count = $state<number>(0);
let user = $state<User | null>(null);

// ✅ Type your derived
let doubled = $derived<number>(count * 2);

// ✅ Type your props
interface Props {
  value: number;
  onChange?: (val: number) => void;
}

let { value, onChange }: Props = $props();

// ✅ Type your effects (no return type needed)
$effect(() => {
  console.log('Count:', count);
});
</script>
```
