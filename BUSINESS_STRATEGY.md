# CupBots Business Strategy

## Core Thesis

The bot framework is **not** the moat — the **marketplace** is. Control distribution and payments, let the runtime be free.

## Self-Hosting Decision

**Recommendation: Open-source the framework.**

### Why open-source is low-risk

- The Python framework code is commodity — anyone technical enough to self-host can rebuild a plugin system
- Python code can't be meaningfully obfuscated; if distributed, it will be read
- Closed-source slows adoption, blocks community contributions, and increases support burden
- A competitor can open-source a similar framework and win on adoption alone
- Plugin developers need to read framework internals to build and debug — closed-source creates friction

### What stays proprietary

- Plugin marketplace (registry, discovery, reviews)
- Payment/billing infrastructure
- Managed hosting tier
- Plugin distribution API (license keys, entitlements, signed packages)

## Plugin Code Visibility

Installed plugins are fully visible Python files. **This is fine.** WordPress plugins work the same way and generate billions annually.

### Why piracy isn't a real threat

- Copied plugins don't receive updates, bug fixes, or new features
- Users would rather pay $5/month than maintain a fork
- People who copy were never going to pay anyway
- Plugin devs build for the marketplace because that's where the users are

### Practical protections

- License keys validated against marketplace API on install/update
- Entitlement checks for premium features (phone-home on boot)
- Fast update cycles that make running stale copies painful

## Revenue Model

### Plugin subscription cut (15–20%)

Justify the cut by providing:
- **Distribution** — access to the entire install base
- **Billing** — plugin devs don't handle payments themselves
- **Trust** — reviews, ratings, verified publishers
- **Analytics** — install counts, usage metrics

### Managed hosting tier

Premium tier for non-technical users who don't want to self-host. This is where the highest margins live.

## Key Risk

The real threat is **disintermediation**, not cloning:
- A popular plugin dev talks directly to a large customer and bypasses the marketplace fee
- A competing marketplace stands up with better terms for plugin devs

## Defensive Priorities

1. Make the marketplace indispensable (distribution + billing + trust)
2. Grow the install base via open-source framework (more users = more plugin customers)
3. Keep plugin install flow dependent on your API (registry, licensing, updates)
4. Build network effects — more plugins attract more users, more users attract more plugin devs
