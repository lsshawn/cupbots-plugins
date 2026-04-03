# Onboarding

**First impressions decide if new members stay or leave.** Most WhatsApp communities lose new members in the first 24 hours — not because the community is bad, but because nobody welcomed them and they felt invisible.

The Onboarding plugin automates your welcome flow so every new member feels seen, understands the group's purpose, and knows exactly what to do next — without you having to be online 24/7.

## The Problem

You've worked hard to get people to join your WhatsApp community. But when they arrive:

- They see a wall of old messages and no context
- Nobody greets them — existing members don't notice
- They don't know the rules, the culture, or what's expected
- It feels impersonal, or worse — like a spam group they got added to
- They mute the group. Then they leave.

**You get one shot at a first impression.** This plugin makes sure you nail it every time.

## What Makes a Great Onboarding

A good welcome does three things:

1. **Makes them feel expected** — not like an afterthought. Use their name, acknowledge they just arrived.
2. **Gives them context** — what this community is about, what to expect, how often people post.
3. **Gives them a first action** — "Tell us about yourself" is the simplest way to turn a lurker into a participant.

A few other things that help:
- **Don't send instantly** — a 5-10 second delay feels more human than a bot that fires in 0.2s
- **Keep it short** — 3-5 lines max. They'll scroll past anything longer.
- **Private DMs feel personal** — a group welcome is for the community to see; a DM is for them.
- **State the rules early** — it prevents problems before they start, and signals that this is a well-run group.

## How It Works

### Install (from chat — no terminal needed)

```
/plugin install onboarding
/reload
```

### Setup wizard (guided, from chat)

Run `/onboard` and the bot walks you through it:

```
You:   /onboard
Bot:   Let's set up onboarding for this group!

       Step 1/5: Welcome message
       What should new members see when they join?
       Use {name} for their name.

You:   Hey {name}! Welcome to CupBots Community — we build bots and ship fast.
Bot:   Saved.

       Step 2/5: Group rules

You:   1. Be kind  2. No spam  3. Share what you build
Bot:   Saved.

       Step 3/5: Ask new members to introduce themselves?

You:   yes
Bot:   Saved.

       Step 4/5: Send a private DM welcome?

You:   yes
Bot:   Saved.

       Step 5/5: Welcome delay (seconds)

You:   5
Bot:   All set! Here's a preview:
       ─────────────────
       Hey John! Welcome to CupBots Community — we build bots and ship fast.

       Rules:
       1. Be kind  2. No spam  3. Share what you build

       Tell us a bit about yourself!
       ─────────────────
```

That's it. Every new member now gets welcomed automatically.

### Quick config (for power users)

Skip the wizard and set values directly:

```
/onboard set welcome Hey {name}, glad you're here!
/onboard set rules Be respectful. No spam. Have fun.
/onboard set intro true
/onboard set dm true
/onboard set delay 10
```

### Commands

| Command | What it does |
|---------|-------------|
| `/onboard` | Run setup wizard (first time) or show current config |
| `/onboard set <key> <value>` | Set a specific config value |
| `/onboard preview` | See what the welcome message looks like |
| `/onboard edit` | Re-run the setup wizard with existing values |
| `/onboard on` | Enable onboarding |
| `/onboard off` | Pause onboarding (keeps your config) |
| `/onboard reset` | Delete all config for this group |

### Config keys

| Key | What it controls | Default |
|-----|-----------------|---------|
| `welcome` | Welcome message. `{name}` = member name | "Welcome, {name}! We're glad you're here." |
| `rules` | Group rules shown after welcome | (empty) |
| `intro` | Ask new members to introduce themselves | true |
| `dm` | Also send a private DM to the new member | false |
| `delay` | Seconds to wait before sending (0-300) | 5 |

## Per-Group Configuration

Each group gets its own onboarding config. Your professional community can have a formal welcome while your casual group keeps it light — all managed from the chat, no config files needed.

Works in WhatsApp communities with multiple sub-groups — each sub-group can have its own welcome message.

## Tips for Writing a Good Welcome Message

**Do:**
- Use `{name}` — personalization makes it feel real
- State your community's purpose in one line
- Keep it under 4 lines
- End with a call to action ("tell us about yourself")

**Don't:**
- Write a novel — nobody reads long welcomes
- Use all caps or excessive emojis
- Include links in the first message (triggers spam filters in people's heads)
- Welcome people at 3am with no delay (feels robotic)

## Example Welcome Messages

**Professional community:**
```
/onboard set welcome Hey {name}, welcome to IndieBuilders! We're a group of indie hackers shipping products and sharing what we learn. Jump in anytime.
```

**Casual group:**
```
/onboard set welcome {name} just joined! Welcome to the crew.
```

**Local community:**
```
/onboard set welcome Welcome {name}! This is the KL Tech Community — we do monthly meetups and share opportunities. Introduce yourself when you're ready.
```
