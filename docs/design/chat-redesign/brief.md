# tret — app design brief (for a Claude Design session)

**Purpose of this doc:** ground a Claude Design session in what tret actually is and how it actually works today, so a real design pass can be done against the real product — not a marketing screenshot. This was written by reading the live `frontend/` source (`kith-ai-lab/tret`), not from memory or the launch-page mock.

**How this loop works:** this brief → you design in Claude Design → download the design handoff → hand it back → it gets implemented as real code on a branch of the `tret` repo.

---

## 1. What tret is

tret is an open-source AI harness platform for non-technical knowledge work — what a coding agent is for engineers, built for analysts instead. Users hand it plain-language tasks (extract evidence, check a data point, draft a section) and it does structured, auditable work under rules that make the output trustworthy enough to put in front of a client, a credit officer, or an auditor. The flagship domain pack is **climate risk assessment**; the core is domain-agnostic.

Five things are load-bearing, not cosmetic — a design pass should make these more visible, not paper over them:

1. **The AI never invents numbers.** Every value traces to a retrieved dataset row or a deterministic, manifest-pinned method call.
2. **Outputs are drafts until a named human approves them.**
3. **Every run is fully auditable** — model used, why the router picked it, doctrine version, every tool call, cost, and estimated energy/carbon.
4. **Doctrine-as-context** — reasoning rules are versioned markdown the agent must follow and cite.
5. **Honest uncertainty** — `insufficient_data` is a respectable verdict; missing data becomes an explicit request, never a guess.

It's a real product with a hosted hub app (Kith Climate distribution) and a self-hosted OSS core (Kith AI Lab). This brief covers the **app UI**, not the marketing/launch pages.

---

## 2. Design system foundation (already established — respect or deliberately evolve it)

This isn't a blank slate. There's a real brand carried over from the (design-reviewed) launch site into `frontend/src/theme/global.css`. A design pass should build on this, not reinvent color/type from scratch — but it should also fix the one place the dev implementation broke brand consistency (flagged below).

**Type**
- Sans (UI/body): **Instrument Sans** — variable weight, 400–700
- Mono (everything numeric, technical, or structural — labels, badges, receipts, nav): **JetBrains Mono** — 400–700
- Both are on Google Fonts.

**Color — light (default)**
| Token | Value | Use |
|---|---|---|
| bg | `#f6f4ee` | page background (warm cream) |
| bg-panel | `#fdfcf8` | cards, panels |
| bg-input | `#ffffff` | form fields |
| bg-hover | `#efece3` | hover state |
| border | `rgba(33,31,26,.14)` | default border |
| border-subtle | `rgba(33,31,26,.08)` | dividers |
| text | `#211f1a` | primary text (warm near-black) |
| text-muted | `#6e6a60` | secondary text |
| **accent** | **`#9a5b26`** | **the brand copper — links, active states, primary actions** |
| green | `#2e6b5a` | success / carbon-positive |
| red | `#a63a2c` | error |
| amber | `#8f6400` | warning |
| violet | `#6b5a9e` | tool-activity indicators |

**Color — dark**
Same structure, inverted. **Known problem, worth deliberately fixing in this design pass:** the current dev implementation swaps the brand copper accent for a generic blue (`#8ab4f8`) in dark mode — the one place the brand identity doesn't carry through. Everywhere else (the brand slash mark, the wordmark) is defined as identical across themes on purpose. A dark palette that keeps a warm, copper-tinted accent (rather than a cool blue) would be more consistent with the rest of the system.

**Brand mark**
The wordmark is "tre" + a skewed copper-gradient slash + "t" — same mark used on the launch site, reused at 20px in the app sidebar. Assets: `kith-climate/tret/_brand/`.

**Existing component vocabulary** (so new/redesigned pieces extend it rather than inventing a parallel language):
- `.badge` — small pill, color-coded by semantic tone (green/red/amber/blue/violet/gray), used for statuses
- `.panel` — bordered card, `bg-panel`, 6px radius
- `.mono-table` — dense data tables, mono type, uppercase mono column headers
- `.chip` — small pill for tags/filters
- Buttons: default / `.btn-primary` (accent-tinted) / `.btn-approve` (green) / `.btn-reject` (red) / `.btn-danger`
- Standard control height ≈ 30–34px, radius 5–10px depending on component, border-first (not filled) as the default resting state

---

## 3. App structure (sidebar, 11 sections)

Left sidebar, fixed width, top to bottom: wordmark → workspace switcher (which org/team) → nav → light/dark toggle → user footer (name, role, log out).

| Section | What it is |
|---|---|
| **Chat** | The primary surface — conversational entry point to everything else. Full spec in §4. |
| Workbench | Structured task runner — pick a harness/task type and run it directly, outside a chat conversation. |
| Runs | List of every individual run (chat-triggered or direct), each with routing/cost/carbon detail and full audit trail. |
| Approvals | Queue of draft outputs awaiting a named human's sign-off before they count as final. |
| Analytics | Usage rollups — spend, volume, routing mix over time. |
| Emissions | Carbon-specific reporting — the footprint accounting in aggregate, with methodology/derivation detail. |
| Deliverables | Generated documents/exports assembled from completed, approved work. |
| Documents | Source documents uploaded into a workspace for the AI to work from. |
| Packs | Domain packs (climate-risk is the flagship) — browse, install, and a pack builder for authoring new ones. |
| Harnesses | Configured task harnesses — the reusable "recipe" objects that pair a task profile with a model policy, doctrine, and tools. |
| Settings | Provider API keys, billing, workspace config. |

**Workspace switcher**: users can belong to multiple workspaces (personal or team); switching re-scopes everything.

---

## 4. Deep-dive spec: Chat (today's implementation, in full)

This is the screen most worth a real design pass first — it's the highest-traffic surface and the most interaction-dense.

### Layout
Three columns: **sidebar** (nav, per §3) · **conversation rail** (collapsible) · **main column** (topbar + thread/landing + composer, pinned to a centered ~720px reading column).

### State A — Landing (no conversation selected / new chat)
- Rail: "+ New chat" button, list of past conversations (title + relative time), or "No conversations yet."
- Centered hero: small mono mark "tret_", H1 **"What can I help you assess?"**, one-line subhead about plain-language delegation to the right harness task.
- The composer (see below), centered, same width as the hero text.
- Below it, a 2×2 grid of example-prompt cards, each a mono uppercase label + the literal prompt text, clickable to send immediately. Real current examples:
  - *Trust a vendor score* — "Is the vendor flood score for Alder Point (S-003) still trustworthy?"
  - *Extract evidence* — "Extract governance evidence from the uploaded questionnaire"
  - *Survey the data* — "What datasets do we have?"
  - *Draft a section* — "Draft the risk management section of the TCFD assessment"

### State B — Active thread
- Topbar: panel-toggle icon (show/hide rail), conversation title, new-chat icon.
- **User turn**: right-aligned bubble, bordered card style (not a filled chat-bubble color).
- **Assistant turn**: small avatar + body. Body =
  1. Optional row of **tool-activity pills** (violet-toned) — e.g. "lookup_dataset · flood_scores_2026" — clickable through to the underlying run.
  2. The answer, rendered as markdown prose.
  3. A **collapsible footprint/receipt chip** (closed by default) — the compact line always shows: model name · cost · estimated CO₂e with a confidence band · a coarse comparison to a frontier-model baseline ("~50x lighter than frontier," or a red "surcharge" framing when a pinned model costs *more*). Expanded, it shows: routing rationale (which harness, which objective), token counts (in/out, cache read/write), and full energy/derivation detail.
  4. A footer with "view run →" linking to the full audit record in **Runs**.
- **Streaming state** (a turn in progress): tool-call pills marked "running" (amber), a blinking-caret "thinking…"/streaming text, a live token/cost ticker, and a reconnect/disconnect state that's explicit about *the run continuing* even if the live view drops — never implies the run itself failed.

### Composer (shared between both states)
- A row of three routing-override pills, all defaulting to **"using harness default"** — never silently pre-selected:
  - **Harness** — which harness this chat/run uses (fixed once a conversation starts)
  - **Objective** — one of `quality` / `balanced` (default) / `token_conservation` / `eco` — what the router optimizes for
  - **Model override** — pin a specific model, constrained to what the active harness's policy allows
- Rounded input box (18px radius) with auto-growing textarea, placeholder "Message tret…", circular send button (accent-filled).
- A quiet caption below: *"Responses are drafts — structured findings go to Approvals before they count."* — this line matters; it's the product's core trust claim surfacing at the point of use.

### Data model notes (for accuracy, not for the composer UI itself)
- Cost tiers, low → high: `local → economy → standard → premium`. `local` is a confidentiality ceiling (only local/offline models), not a spend ceiling.
- A harness pairs a task profile with a model policy (`auto` or `pinned`), an optional doctrine/system-prompt extension, and one or more domain packs.

---

## 5. Open questions worth the design pass actually deciding

These are real, unresolved UX questions — not filler. A useful Claude Design output would take a position on each:

1. **Nav scannability at 11 items, text-only today.** Does an icon-led nav (or grouping/sectioning) help, and if so what's the icon language?
2. **Dark mode's accent color.** Copper-tinted (brand-consistent) vs. the current ad-hoc blue — and if copper, how does it read for both accent *and* the semantic-status colors (green/red/amber) that currently stay fixed across themes?
3. **Footprint chip discoverability.** It's collapsed by default and easy to miss, but it's the product's core differentiator (cost + carbon receipts). Should it default open, or get a stronger visual anchor even collapsed?
4. **Composer control pills** are currently plain `<select>`-styled pills with no visual distinction between "harness," "objective," and "model" beyond label text. Worth a clearer visual system (icon per control type, or grouping) given they sit right above every message sent.
5. **Tool-activity pills vs. footprint chip** currently share no visual relationship despite both being "what happened during this turn" — worth unifying into one activity/audit language.

---

## 6. What to bring back

From the Claude Design session: the **design handoff export** (spec doc + high-fidelity screens for whichever states you took on — landing / active thread / both, light + dark). Hand the folder back and it gets implemented as real code on a new branch of `kith-ai-lab/tret` (cloned locally at `~/dev/tret`), matching the actual React/TypeScript component structure rather than being pasted in as static HTML.

**Reference assets already in the workspace:**
- Brand marks: `kith-climate/tret/_brand/`
- Real source (for exact current values, if useful to open side-by-side): `~/dev/tret/frontend/src/theme/global.css`, `src/views/Chat.tsx`, `src/App.tsx`
