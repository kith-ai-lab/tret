# Authoring a Domain Pack

A pack turns bench into a workbench for *your* domain — contract review,
grant compliance, safety audits, anything where structured judgment over
documents and data needs to be trustworthy. No backend code required.

## Layout

```
my-pack/
├── pack.yaml            # the manifest (below)
├── doctrine/            # ordered markdown rules the agent follows and cites
│   └── 01-principles.md
├── schemas/             # JSON Schema (draft 2020-12) per structured output
│   └── my_verdict.schema.json
├── templates/           # optional deliverable skeletons
└── sample-data/         # optional CSVs seeded as datasets on install
```

Validate any time:

```bash
bench packs validate ./my-pack
```

## pack.yaml

```yaml
pack: my-pack            # slug
version: 0.1.0
display_name: My Domain
description: One paragraph.
doctrine:                # ordered; hashed together into doctrine_sha
  - doctrine/01-principles.md
task_types:
  - slug: my_assessment
    display_name: My assessment
    shape: verdict       # verdict|extraction|drafting|qa_review|freeform
    input_schema:        # FLAT fields only — they become the run form
      subject_id: { type: string, description: "..." }
      category:   { type: string, enum: [a, b, c] }
    output_schema: schemas/my_verdict.schema.json
    terminal_tool: record_verdict
    tools: [lookup_dataset, read_document, file_data_request, record_verdict]
    output_contract: One line the model router reads.
    instructions: |
      Step-by-step instructions for the task. Reference your doctrine.
datasets:
  - { name: reference_scores, file: sample-data/reference_scores.csv }
```

## The pieces that make it trustworthy

- **shape** drives the router's deterministic fallback and tells the LLM
  router what the task demands. Pick honestly.
- **doctrine** is where your profession's judgment lives. Write rules with
  *evidence tests* — "you may only use this code if you retrieved X" — so
  compliance is checkable. The agent must cite headings; make them citable.
- **output_schema** is enforced mechanically, with in-loop repair. Prefer
  enums over free text wherever a value is decision-relevant. If your schema
  includes a `cited_values` array (objects with `dataset`, `row_ref`,
  `value`), bench cross-checks every entry against what the run actually
  retrieved via `lookup_dataset` — use it for any output that carries numbers.
- **terminal_tool** (`record_verdict`, `record_finding`, or `draft_section`)
  makes the structured output the *terminal action* of the run. If the model
  stops without recording, the engine nudges it once, then the run completes
  without a finding rather than fabricating one.
- **datasets** are the deterministic lane: CSVs whose rows the model can
  retrieve (exact-match filters) but never edit or compute over.

## Builtin tools you can grant

| Tool | Use |
|---|---|
| `read_document` / `search_documents` | attached evidence documents |
| `lookup_dataset` | the only source of numbers |
| `list_prior_findings` | reference earlier verdicts/extractions |
| `record_verdict` / `record_finding` | schema-validated structured outputs |
| `draft_section` | store a markdown deliverable section |
| `file_data_request` | declare a gap instead of guessing |

## Input schema → form

`input_schema` fields must be flat (`string`, `number`, or `string` with
`enum`). The Workbench generates the run form from them — keep them few and
analyst-friendly.

## Sample data

Ship enough fictional data that a stranger can run every task type
end-to-end after `docker compose up`. Seed at least one case where the
*interesting* outcome occurs (a divergence, a violation, a gap) — demos that
always say "everything agrees" teach nothing.
