# Demo Script (~10 minutes)

Login: `admin@example.com` / `bench-admin` (or your `.env` values).
Everything below works on a fresh `docker compose up` with one OpenRouter key.

## The one-liner

> "Coding agents get a harness — tools, guardrails, audit. Analysts get a
> chatbox and a prayer. bench is the harness for non-technical knowledge
> work: the AI can't invent numbers, its outputs are drafts until a named
> human approves, and every run — including *why this model was chosen* — is
> auditable. Climate risk assessment is the first domain pack; the core is
> domain-agnostic."

## The seeded world

Fictional but hand-designed so every demo path hits something interesting:

- **`sites`** — 12 sites (tree names) across 4 regions (Coastal Lowland,
  River Valley, Interior Plains, Upland Hills), each with a sector.
- **`regional_signals`** — forward-looking climate signal per region × peril
  (flood/heat/wind/drought) × two emissions scenarios, horizon 2050:
  direction, magnitude class, median + tail change vs a 1995–2014 baseline.
- **`hazard_scores`** — vendor-style present-day risk scores per site × peril,
  with source, **vintage year**, and a method note.
- **Evidence docs** (`packs/climate-risk/sample-data/evidence/`) — "Acme
  Fabrication Co.": a Scope 1/2 carbon workbook (with deliberate data-quality
  wrinkles) and an ESG questionnaire (with deliberate gaps: an emissions
  target with no documented baseline, one certified site out of four, no
  climate-linked incentives).

### Designed cases — know these cold

| Ask about | Designed outcome | What it demonstrates |
|---|---|---|
| **Alder Point (S-003) × flood** | `diverge_signal_higher` / `outdated_inputs` — vendor scored it "low" (22) in **2018**, "before basin re-mapping"; the signal shows a robust high increase under both scenarios | The flagship: "the gap is the product" |
| Willow Bend (S-006) × flood | `agree` — fresh 2024 vendor score (71, high) matches the signal | It doesn't manufacture drama |
| Rowan Ridge (S-011) × wind | `diverge_reference_higher` / `methodology_choice` — vendor method note says "gust exposure at site elevation with terrain amplification"; the regional signal actually *decreases* | Reason codes need evidence (the method note) |
| Mesquite Flat (S-009) × drought | fragile signal — scenarios disagree (stable-low vs increase-high) → confidence capped at low, possibly `insufficient_data` | Honest uncertainty is architectural |
| Hawthorn Quay (S-004) × flood | no vendor score exists → `insufficient_data` + a **data request** | Gaps become records, not guesses |

## The walkthrough

1. **Chat (landing page).** Ask in plain language: *"Is the flood risk score
   our vendor gave the Alder Point site still trustworthy? Please check
   properly."* Narrate while it streams: the assistant recognizes this as a
   specialist task and delegates — watch the "⚙ delegated
   divergence_assessment" pill. The reply is written for a credit officer.
2. **Click "view run" → the child run.** This is the money screen:
   - **Routing badge** → the router's verbatim reasoning for choosing the
     model, the candidates it considered, prompt version, latency. "Model
     selection is a logged, auditable decision — not vibes."
   - The transcript: every `lookup_dataset` call. "Numbers can *only* enter
     this way. If the model cites a value it didn't retrieve, validation
     rejects it and it must repair."
3. **Approvals.** The verdict is a **draft**. Show the payload: verdict enum,
   reason code, cited values (verbatim, with row references), doctrine
   citations. Approve it — point out the approver is stamped from the login
   session; there is no field to claim approval.
4. **Deliverables.** Ask chat (or the Workbench) to draft a TCFD section,
   approve it, then download the **PDF**. Flip to the last page: the
   **provenance appendix** — per-section model, doctrine hash, approval
   status. "The audit story travels *inside* the document."
5. **Honest uncertainty.** Ask about **Hawthorn Quay × flood**. It files a
   data request and returns `insufficient_data`. Show Settings → data
   requests. "A gap becomes a work item, not a hallucination."
6. **Packs.** Open the climate-risk pack: the doctrine files (the analyst's
   rules, versioned and hashed), the verdict schemas, the task types. Close:
   "Swap this directory and bench is a workbench for a different profession —
   contract review, grant compliance, safety audits. That's the open-source
   pitch."

## Questions you'll get

- *"Which model does it use?"* — Whichever the router picks per task, within
  your cost tier: Claude, Kimi, GPT, Gemini, Llama, DeepSeek via three
  providers. Pin it per harness or override per run; either way it's logged.
- *"Can the AI approve its own work?"* — No. Findings are created as drafts;
  the only path to approved is a human session. The model is also instructed
  to never claim finality, and its tool results reiterate it.
- *"What stops it making up numbers?"* — Schema validation cross-checks every
  cited value against the run's actual dataset retrievals. Un-retrieved
  numbers are validation errors the model must fix.
- *"Is my data sent anywhere?"* — Only to the LLM providers you configure.
  No telemetry, env-only config, self-hosted.
