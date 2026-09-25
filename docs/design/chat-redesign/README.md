# Chat redesign v1: design system handoff

A design reference for the app UI, starting with Chat. It is **not app code**:
nothing here is imported by `frontend/`. Implement it in the real React
components (`src/views/Chat.tsx`, `src/components/chat/`, `src/theme/global.css`) rather than pasting the
mockup HTML in.

- **Source:** Claude Design canvas "tret Chat Redesign", last saved 2026-09-03.
- **Brief it answers:** [`brief.md`](brief.md), written from the live
  `frontend/` source at the time.
- **Owner for questions:** Diego.

## What's in this folder

| Path | What it is |
|---|---|
| `preview/chat.html` | Active thread. Open it in a browser. Click the theme control in the sidebar, or add `?theme=dark` |
| `preview/landing.html` | New-chat landing (empty rail, hero, composer, example cards) |
| `tokens.css` | Every token change, using the variable names `global.css` already has. Each value is marked designed, new, derived or kept |
| `source/*.dc.html`, `source/canvas.json` | Original canvas source. Needs the Claude Design runtime to render; use `preview/` instead |
| `brief.md` | The brief the design was made against |

## The system in one screen

**What changes**

1. **Dark mode goes warm.** The blue-gray dark palette and the blue accent
   (`#8ab4f8`) are replaced by warm ink surfaces and a copper accent
   (`#e0a868`). Status colours are re-tuned to sit on warm ink. The full set,
   with before and after values, is in `tokens.css`. This was the brief's main
   ask.
2. **Icon-led nav.** All 11 sidebar items get a 16px stroke icon
   (1.75 stroke, round caps and joins, `currentColor`). Inactive icons sit at
   0.7 opacity; the active item gets accent text on an `--accent-dim` fill.
   The SVGs are inline in `source/Main.dc.html`.
3. **Footprint receipt is open by default** and reads as a card, not a chip.
   A summary line shows model · cost · CO₂e · baseline comparison. The body
   has two comparison bars (cost uses the copper gradient, carbon uses a green
   gradient) and a key/value routing grid.
4. **Two new tokens:** `--text-faint` (timestamps, placeholders, hints) and
   `--shadow`.

**What stays:** the light palette (unchanged), fonts (Instrument Sans for UI,
JetBrains Mono for anything numeric, structural or labelled), the
border-first resting style, the violet tool-activity language and the brand
slash.

## Layout

| Region | Spec |
|---|---|
| Sidebar | 208px fixed. Wordmark (19px/700, −0.03em) → workspace card → nav → theme toggle → user footer. `--border-subtle` right edge |
| Conversation rail | 244px. "New chat" button (10px radius) → list of title (12.5px sans) + relative time (10px mono, `--text-faint`). Active item has a `--bg-hover` fill |
| Topbar | 52px. Rail toggle · title (12px mono, muted) · new chat. Icon buttons are 30×30 with an 8px radius |
| Reading column | 720px max, centred, 28px side padding, 26px between turns. Landing column is 640px |
| Composer dock | Same 720px column, 10px top and 20px bottom padding |

## Type scale

The body base is 14px/1.5 sans.

| Use | Size / weight | Family |
|---|---|---|
| Landing H1 | 30 / 500, −0.015em | sans |
| Wordmark | 19 / 700, −0.03em | sans |
| Chat prose, user bubble, input | 14.5, line-height 1.7 (prose) or 1.6 | sans |
| Example-card prompt | 13 | sans |
| Rail title | 12.5 | sans |
| Nav, workspace name, topbar title | 12 | mono |
| Tool pills, footprint summary and rows | 11 | mono |
| Composer controls, turn footer, hint | 10.5 | mono |
| Uppercase labels (workspace kind, routing keys, card titles) | 9.5–10.5, +0.08em, uppercase | mono |

## Radii

| Radius | Used on |
|---|---|
| 2px | Brand slash |
| 6px | Log-out button |
| 7px | Nav item |
| 8px | Workspace card, rail item, theme toggle, icon button, avatar |
| 10px | New-chat button |
| 11px | Tool pill, send button |
| 12px | Footprint card, example card, composer control |
| 16px (4px bottom-right) | User bubble |
| 18px | Input box |

## Components

- **User turn:** right-aligned, max 80% width, `--bg-panel` fill with a
  `--border` outline, radius `16 16 4 16`. Bordered, not a filled chat
  colour.
- **Assistant turn:** 28px avatar (brand slash on `--accent-dim` with an
  `--accent-border` outline), then the body:
  1. Tool pills: violet (`--violet`, `--violet-dim`, `--violet-border`),
     11px mono, 11px radius, an 11px icon, and the text
     `tool_name · target`.
  2. Prose.
  3. Footprint card (below).
  4. Turn footer: 10.5px mono in `--text-faint`, with an underlined
     "view run" link.
- **Footprint card:** `--bg-panel`, 12px radius, uses `<details open>`.
  - Summary: chevron · **model** · cost · CO₂e (est.) · comparison in
    `--green`.
  - Bars: a 78px label column, a 7px track on `--bg-input`, and a 70px
    right-aligned value column.
  - Routing grid: 128px uppercase key column (`--text-faint`), with the value
    in `--text`.
- **Composer:** the three control pills sit above the input (10.5px mono,
  12px radius, `--bg-panel`). The input box has an 18px radius. The send
  button is 36×36, 11px radius, with an `--accent` fill. The trust caption
  sits centred below it.
- **Landing:** "tret_" mark (the underscore in `--accent`) → H1 → subhead
  (max width 34rem) → composer → a 2×2 grid of example cards. Each card has
  an uppercase accent label and the literal prompt.

## Before you implement: open items

**Placeholder figures and copy. Do not ship them.** The numbers and wording
in the mockups are illustrative: "~50x lighter than frontier", "saved $0.061
vs frontier (74%)", 0.4 g, $0.021. Keep rendering whatever the app computes
today and its current wording. "Saved vs frontier" framing in particular
conflicts with `docs/eco-accounting.md` and `docs/emissions-methodology.md`
("not a saving"). The receipt's *layout* is the design; its claims are not.

**Things the design gets wrong or leaves open:**

- The footprint routing grid shows `Objective: cost-optimized`. That is not
  a real objective (`quality` / `balanced` / `token_conservation` / `eco`),
  so render the real value.
- The collapsed summary drops the **CO₂e confidence band** that the app shows
  today. Keep the band.
- **Composer controls (brief Q4)** still look like three identical pills; the
  design does not solve this. Keep the current controls until a follow-up
  pass.
- **Tool pills vs footprint (brief Q5)** remain two visual languages. Not
  solved either.
- **Not designed at all:** the streaming state (running and amber pills,
  caret, live ticker, reconnect), the "surcharge" red framing for pinned
  models that cost more, and the other 10 sections. Apply the tokens and nav
  there; the layouts stay as they are.

## Suggested order

1. Swap the dark token block in `global.css` for `tokens.css` and add the two
   new tokens. This is low risk, gets the most visible win and touches every
   screen.
2. Sidebar nav icons.
3. Chat: user bubble, assistant turn, footprint card open by default, and the
   composer spacing.
4. Landing polish.
