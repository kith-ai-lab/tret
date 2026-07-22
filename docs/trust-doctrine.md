# The Trust Doctrine

bench exists for work where "the model said so" is not good enough: client
deliverables, credit decisions, disclosures that face auditors. These five
principles are product identity, and each one is enforced by architecture —
not by hoping the prompt holds.

## 1. Deterministic vs. interpretive separation

The AI reasons, compares, and drafts. It does not compute, estimate, or recall
numbers. Structured numeric inputs live in **datasets**; the only way a value
enters a run is the `lookup_dataset` tool, which records everything it
returns. When the model records a verdict, its `cited_values` are cross-checked
against that record — a value it never retrieved fails validation and is sent
back for repair. (`bench/engine/validation.py`)

## 2. The blessing gate

Every structured output is created with status `draft`. It becomes `approved`
only through an approvals endpoint that stamps the approver from the
authenticated session; the API schema has no approver field to spoof. The
platform preamble also forbids the model from claiming its own output is
approved. (`bench/api/findings.py`)

## 3. Provenance everywhere

Every run persists: the full **routing decision** (router model, candidates,
verbatim reasoning, prompt version, fallback state), the executing model, the
**doctrine content hash** current at run time, the complete message
transcript including tool calls, token counts, and cost. Every finding carries
the retrieved values and documents behind it. (`runs.routing`,
`findings.provenance`)

## 4. Doctrine-as-context

A pack's reasoning rules are versioned markdown files, hashed together into a
`doctrine_sha`. They are loaded — tagged with per-file hashes — at the top of
the system prompt, and the model must cite headings it relies on. Change the
doctrine and the sha changes; old runs remain interpretable against the
doctrine they actually ran under. (`bench/engine/context.py`)

## 5. Honest uncertainty

Verdict schemas include `insufficient_data`; confidence tiers are earned by
robustness rules in the doctrine, not fluency. When required data is missing
the model files a **data request** — a first-class record an operator can
fulfill — and completes the assessment with what exists, stating the gap's
effect on confidence.
