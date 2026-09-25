# tret · Kith Climate design system v2 (handoff)

A design reference for the hosted Kith Climate build of tret. It restyles the
**whole app** to the kithclimate.com design system. That covers tokens, type,
buttons, forms, badges, tables, navigation, chat, modals and login, not only
the wordmark. It is not app code yet. Nothing in `frontend/` imports it.

- **Decided (Diego, 2026-09-25):** follow the kithclimate.com design system,
  use JetBrains Mono for data, and ship **light only**.
- **Supersedes** `../_archive/chat-redesign-v1/`. That handoff kept the
  cream/copper/Instrument Sans system, added a green slash on top, and covered
  Chat only.
- **Source of truth for values:** the live kithclimate.com CSS
  (`apps/website/index.html` and `apply.html` in the Kith workspace, read
  2026-09-25). Anything not taken from there is marked *(derived)* in
  `brand-kith-climate.css`.
- **Owner for questions:** Diego.

## What's in this folder

| Path | What it is |
|---|---|
| `brand-kith-climate.css` | **The spec.** Every token and component rule for the brand, scoped to `[data-brand="kith-climate"]` and written against the real `global.css` class names. It is designed to be copied into the app as-is (step 1 below). |
| `preview/landing.html` | New-chat landing, including the first-run banner |
| `preview/chat.html` | Active thread: tool pills, closed and open receipts, delegated work, a streaming turn, composer |
| `preview/components.html` | Other views: Runs table, approval actions, forms, every badge tone, callouts, emissions bars, list/detail, drop zone, modal, login |
| `preview/_preview.js` | Preview harness only. It loads fonts and adds a **Kith Climate ↔ Open source** toggle, so each screen shows before and after |
| `assets/favicon.svg` | Favicon: the kithclimate.com slash on the kithclimate.com `#1A1D21` tile |

**Open the previews through a server, not `file://`.** They load
`frontend/src/theme/global.css` by relative path. From the repo root run
`python3 -m http.server 8816`, then open
`http://localhost:8816/docs/design/kith-climate-v2/preview/chat.html`. The
figures in the previews are sample data.

## The system on one page

**Principles**

1. **Teal means act; green means good.** The fill teal `#5B9A8B` (hover
   `#6FB5A4`, dark `#10201B` text on it) is for primary buttons, Approve,
   Send, the caret and bars. Text teal `#3E7A6C` is for links, active states
   and focus. Success stays a sage green `#3E7040`, visibly apart from teal.
2. **Sans for the interface, mono for data.** Inter is used for navigation,
   buttons, inputs, headings, labels and prose. JetBrains Mono is kept for
   costs, tokens, IDs, model names, timestamps, receipts and code. Uppercase
   labels move from mono to kithclimate.com's sans section label (11px / 500
   / +0.1em).
3. **Controls are 6px, cards are 8px, tags are round.** These are
   kithclimate.com's radii. Buttons are never pills. Status badges, chips,
   tool pills and the receipt strip are.
4. **The glow belongs to the main action.** The site's teal glow goes on a
   page's primary button and the send button. Small row buttons don't get it.
5. **Nothing below 11px.** Every size is one step above the open-source build,
   and all content text measures at least 4.5:1.
6. **Light only.** The canvas is `#F7F9F8` and panels are `#FFFFFF`. The theme
   toggle and stored dark preference are ignored in this brand.

**Tokens** (full set, with sources, in `brand-kith-climate.css`)

| Role | Value | Note |
|---|---|---|
| Canvas / panel / input | `#F7F9F8` / `#FFFFFF` / `#F7F9F8` | kithclimate.com. Inputs sit on canvas inside white cards |
| Ink / muted / faint | `#1A1D21` / `#62666A` / `#8A8E92` | Muted is a solid step (5.6:1). The site's .55 alpha measures 3.8:1 at app sizes. Faint is for placeholders and disabled states only |
| Borders | .08 subtle / .12 default / .18 input | kithclimate.com hairline, row and input weights |
| Accent fill / hover / on-fill | `#5B9A8B` / `#6FB5A4` / `#10201B` | kithclimate.com `.btn-primary` |
| Accent text / strong | `#3E7A6C` / `#2F6153` | kithclimate.com accent text and hover. Strong is used on teal tints |
| Status | green `#3E7040`, amber `#8A5E12`, red `#A63A2C`, blue `#3B6190`, violet `#6A55A0`, gray `#62666A` | *(derived)*. Each is ≥ 4.9:1 on its own 10% tint |
| Chart series | `#15967E` `#C38A22` `#4470C4` `#C9567B` `#6BA33E` `#8E5DBF` | Teal leads. Passed the dataviz validator: lightness, chroma, CVD ΔE ≥ 9.0, normal vision, contrast |
| Type | Inter 13–15 UI, 22 page title, 32 landing; JetBrains Mono 11–12.5 data | Size tokens `--fs-2xs … --fs-md` |
| Elevation | card `0 1px 3px /.04`; lit `0 0 24px teal/.14`; popover two-layer | Cards match kithclimate.com. The lit state is for hover and selection |

**Components.** The CSS is the spec. A few calls that aren't obvious from it:

- **Navigation.** kithclimate.com's nav link: sans 13/500, muted, with the
  current page in teal text behind a teal `/`. There is no fill and no side
  bar. The v1 icon nav is dropped, because the site has no icons in its nav.
- **Buttons.** `.btn` becomes kithclimate.com's secondary button. `.btn-primary`
  and `.btn-approve` both get the teal fill, because approving is the action.
  `.btn-reject` is a red outline. `.btn-danger` has red text and turns red on
  hover.
- **Tool pills and receipt share one language** (answers brief Q5). Both are
  neutral bordered pills in mono. A teal dot marks a tool; amber means
  running. Violet no longer appears in chat.
- **The receipt stays collapsed but is now anchored** (answers brief Q3). The
  summary is a bordered strip, not loose grey text, and the open body is a
  white card. Behaviour is unchanged.
- **Assistant avatar.** The brand slash replaces the letter "b" (step 5).

## How it ships: a brand layer for the hosted build only (decided)

tret-cloud builds tret core's `frontend/` unmodified, so the hosted Kith
Climate app and the open-source product share one frontend. This design is
therefore a **brand layer**. It is on only when the build sets
`VITE_TRET_BRAND=kith-climate`, so the open-source build is untouched.

1. **Stylesheet.** Copy `brand-kith-climate.css` to
   `frontend/src/theme/brand-kith-climate.css`. Import it in `main.tsx` after
   `global.css` and `delegated-work.css`.
2. **Font.** Add `@fontsource-variable/inter` and import it in `main.tsx`,
   next to the existing JetBrains Mono import. Fonts stay self-hosted, as
   network isolation requires. Instrument Sans stays for the open-source
   build.
3. **Flag, before first paint.** Add `VITE_TRET_BRAND=` (empty) to
   `frontend/.env` so the variable always exists. Then change `index.html`:
   ```html
   <html lang="en" data-brand="%VITE_TRET_BRAND%">
   …
   var brand = document.documentElement.getAttribute('data-brand')
   var t = localStorage.getItem('tret-theme')
   if (t === 'dark' && brand !== 'kith-climate') document.documentElement.setAttribute('data-theme', 'dark')
   ```
4. **Light only in React.** In `App.tsx`, when the brand is set, `useTheme`
   always returns `light` and the theme toggle isn't rendered. The CSS hides
   the toggle too, as a safety net.
5. **Small markup changes in core** (neutral for the open-source build):
   - In `Chat.tsx`, the avatar renders `<span className="chat-avatar-slash" />`
     instead of `b`. Add a base `.chat-avatar-slash` rule to `global.css`
     using `--brand-slash`, so the open-source avatar shows the copper slash.
   - **Existing bug:** `.chat-cards { text-align: left }` never reaches the
     `<button>` cards, so example prompts render centred in **both** builds.
     Add `text-align: left` to `.chat-card` in `global.css`.
   - When the brand is set, point `link[rel=icon]` at the brand favicon. Put
     `assets/favicon.svg` at `frontend/public/brands/kith-climate/favicon.svg`
     and swap the href in `main.tsx`.
6. **The inline-style sweep (this is what makes it holistic).** The TSX has
   985 `style={{…}}` blocks. Colours already use tokens, but font sizes don't:
   there are 96 hard-coded sizes, including 36 × `11`, 26 × `10.5`, 14 ×
   `11.5`, 8 × `10`, 2 × `9.5` and 1 × `9`. Inline styles beat any stylesheet,
   so the brand can't reach them. Add the size tokens and classes to
   `global.css` **with the open-source values**, then replace the literals:

   | Inline literal | Class | Open source | Kith Climate |
   |---|---|---|---|
   | `9`, `9.5`, `10` | `.fs-2xs` | 10px | 11px |
   | `10.5`, `11` | `.fs-xs` | 11px | 11.5px |
   | `11.5`, `12`, `12.5` | `.fs-sm` | 12px | 12.5px |
   | `13.5` | `.fs-md` | 13.5px | 13.5px |

   The open-source build shifts by at most half a pixel. Heaviest files first:
   PackBuilder (113 inline blocks), SettingsView (97), EmissionsFactorsPanel
   (88), Emissions (69), Packs (61), Connections (46), Harnesses (43). The 53
   inline `fontFamily: 'var(--mono)'` can stay as they are: mono is right for
   data in both brands.
7. **tret-cloud.** In `Dockerfile.fly`, set `ENV VITE_TRET_BRAND=kith-climate`
   in the frontend stage before `npm run build`.

**Suggested order:** steps 1–4 (the whole app changes look at once), then 5,
then 6 file by file, then 7 when the hosted build should switch.

## Verification done on this handoff

Run in the browser against the three previews, with the real `global.css`
loaded:

- **In brand mode:** no text under 11px, no Instrument Sans, and no copper or
  old-blue colour in any computed style. The same audit run in open-source
  mode finds 36, 16 and 28 hits respectively, so the audit does detect them.
- **Contrast:** every content text element is ≥ 4.5:1 against its actual
  composited background. The minimum is 4.71:1. Exempt: decorative `·`
  separators and disabled controls.
- **Chart series:** all dataviz validator checks pass on `#FFFFFF`.

**Not verified:** screens whose look depends on inline styles (step 6). The
previews exercise classes only.

## Decisions (Diego, 2026-09-25)

- **Scope:** a brand layer for the hosted Kith Climate build only. The
  open-source build keeps its current look.
- **No "Kith Climate" name in the app.** The brand shows through the design
  system and the slash, not a label.
- **Timing:** the developer implementing this decides when it lands.

## Open items

- **Receipt wording is unchanged.** "than frontier" and "vs frontier" come
  from `emissions.ts` and are a methodology question
  (`docs/eco-accounting.md` says "not a saving"). They are not a design
  question.
- **Composer controls (brief Q4)** are now consistent with the other selects,
  but they still don't distinguish harness, objective and model visually.
- **Nav grouping** for 11 items (brief Q1) is optional. The slash marker makes
  the current page clear. Grouping would be a core `App.tsx` change.
- **Chart series 5** is a green close to the status green. Keep status colours
  out of charts, as `global.css` already requires.
- **tret.kithclimate.com** (the landing page) still uses the cream,
  Instrument Sans and `#2E6B5A` system. Move it to this system once the app
  ships.
