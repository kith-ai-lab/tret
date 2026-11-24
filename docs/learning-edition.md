# The Learning edition: numbers, datasets and the unvetted lane

This page covers what the cohort edition adds around numbers. (Sessions, saving to
folders and guardrails are described where they live: `docs/connections.md`,
Settings → Guardrails.)

## Two vetted ways and one labelled way to get a number

1. `lookup_dataset`: rows from a project dataset, cited by row reference.
2. `run_method`: a reviewed pack method. The cohort pack `kith-cohort` ships
   `facility_inventory` (below).
3. `run_analysis` (only when the operator sets `TRET_UNVETTED_ANALYSIS=on`): the
   model's own calculation. Always labelled UNVETTED; never citable in a verdict.
   See `docs/trust-doctrine.md` §6.

## The facility inventory method (vetted)

`kith-cohort` installs into cohort-edition workspaces only (manifest `edition:
cohort`; other workspaces skip it quietly). Method `facility_inventory` computes
Scope 1 (stationary fuel, mobile fuel, refrigerant) and Scope 2 location-based per
facility and per portfolio company from a table with the Northwood carbon template
columns:

`facility_id, portco_slug, portco_name, facility_type, city, state, reporting_year,
electricity_kwh, natural_gas_therms, diesel_gallons, gasoline_gallons,
propane_gallons, refrigerant_r22_lbs, refrigerant_r410a_lbs, fleet_diesel_gallons,
fleet_gasoline_gallons, data_source, notes`

Empty cell = no activity. `diesel_gallons`, `gasoline_gallons`, `propane_gallons` are
treated as stationary combustion, `fleet_*` as mobile combustion.

Workflow in chat:

1. The member attaches or points at the file; the model calls
   `import_table(name, item_ref | document_id)`. This creates a project dataset and
   records the source document id and the file's SHA-256 (`datasets.schema_json
   ["source"]`). Importing the same file again under the same name is a no-op;
   different contents under an existing name are refused (pick a new name).
2. The model calls `run_method("facility_inventory", inputs={"activity":
   "<dataset>"}, params={...})`. The method run records the dataset names and
   hashes in `input_summary`, as every method run does.

Output rows (`row_type`): `portfolio`, `portco`, `facility`, and one `skipped` row per
non-empty activity cell that could not be computed, with the reason (no factor, not a
number, conflicting factors). Skipped cells contribute nothing and are never estimated;
`fields_skipped` on each facility row and `facilities_incomplete` on the roll-ups say
when a total is partial.

Scope 2: the factor lookup uses `electricity / <eGRID subregion>` when
`params.subregion_map` ({facility_id: subregion}) gives one and the dataset has it;
otherwise the state rate `electricity / state:<ST>`. Each facility row's
`electricity_factor_basis` says which was used and why.

If the factor dataset is absent the method fails with a message pointing here.

## Loading the EPA spreadsheets

No emission factor is typed into tret. The method reads the dataset
`epa_ghg_factors_2025` (default; override with `inputs={"factors": "<name>"}`), which an
operator loads from the published spreadsheets.

### Normalised long format

One row per factor, CSV or XLSX (first sheet unless `--sheet`), header row required:

| column | meaning |
|---|---|
| `category` | `stationary_combustion`, `mobile_combustion`, `refrigerant`, `electricity` |
| `fuel_or_subregion` | fuel (`natural_gas`, `diesel`, `gasoline`, `propane`), refrigerant (`R-22`, `R-410A`), or for electricity the eGRID subregion code or `state:<ST>` (e.g. `state:TN`) |
| `unit` | the unit the factor is per: `therm`, `gallon`, `lb`, `kWh` or `MWh` |
| `co2_kg_per_unit`, `ch4_g_per_unit`, `n2o_g_per_unit` | per-gas factors where the source gives them |
| `co2e_kg_per_unit` | the CO2e factor the method uses (required, see below) |
| `gwp_basis` | which GWP vintage the CO2e uses, e.g. `IPCC AR5 100-year` (required) |
| `source`, `edition`, `table`, `retrieved_on`, `url` | provenance, on every row |

Keys match case-insensitively ignoring spaces, hyphens and underscores. Refrigerant
factors are kg CO2e **per lb**; a source quoted per kg or per short ton must be converted
in the spreadsheet before loading (the importer never converts units). Rows are rejected
(with the row number) for a non-numeric or negative factor, a blank category/fuel/unit,
or blank provenance. If `co2e_kg_per_unit` is blank it is computed only when you pass the
GWPs the source itself uses (`--gwp ch4=... --gwp n2o=...`, recorded in `gwp_basis`);
otherwise the row is rejected rather than guessed.

### Sources to assemble

- EPA GHG Emission Factors Hub (2025): stationary combustion, mobile combustion and
  refrigerant GWP tables.
- eGRID 2023: subregion and state output emission rates for electricity.
- GHG Protocol: cross-sector tool factors where the Hub has none.

### Command

```
cd backend
python -m tret.services.dataset_import --project <project-uuid> \
  --name epa_ghg_factors_2025 --file factors.xlsx \
  --source "EPA GHG Emission Factors Hub" --edition 2025 \
  --retrieved-on 2026-11-20 --table "Table 1" --url https://www.epa.gov/climateleadership/ghg-emission-factors-hub
```

`--source/--edition/--table/--retrieved-on/--url` fill only blanks, so a file that
already carries per-row provenance (mixed sources) is kept as is. Re-running with the
same file is a no-op; different contents for an existing name fail unless `--replace`
(which swaps the rows under that name; earlier method runs keep their recorded hashes).

## The unvetted lane: what the sandbox does and does not guarantee

`run_analysis` runs model-written Python (see `tret/services/analysis.py`). The
security boundary is **bubblewrap**; everything else is defence in depth.

- **Boundary: bubblewrap (`bwrap`)**. The child runs in new user, pid, net, ipc,
  uts and cgroup namespaces with a brand-new root that holds only read-only binds of
  the Python installation and the system library directories it links against. There
  is no host filesystem in it (no `/app`, `/data`, `/packs`, `/tmp`, `/run`, `/etc`,
  no storage volume), no network, a fresh `/proc` of its own pid namespace (the app's
  `/proc/<pid>/environ` does not exist for it), a minimal `/dev`, an empty environment,
  all capabilities dropped, a tmpfs working directory, `--die-with-parent` and its own
  session. Code and inputs arrive on stdin. Each analysis records `isolation: "bwrap"`.
- **It fails closed.** The first use in a process (and startup, when the lane is on)
  runs a probe through the same sandbox command and checks that host directories and
  the network are really absent. If `bwrap` is missing, unprivileged user namespaces
  are blocked, or the probe fails, `run_analysis` refuses: "The unvetted calculation
  lane is unavailable on this server: no sandbox (bubblewrap) is usable." and records
  a failed run with `isolation: "unavailable"`. The Docker images install `bubblewrap`;
  the container or VM must allow unprivileged user namespaces (the startup log says
  which outcome applied).
- **Development opt-out.** `TRET_UNVETTED_ANALYSIS_ALLOW_WEAK_SANDBOX=true` lets the
  lane run without bwrap (macOS, tests). The child is then a plain process under the
  process limits below, with NO filesystem or network isolation; it is recorded as
  `isolation: "weak"` and the tool result says so. Never set it on a server that holds
  other people's data.
- **Process limits** (defence in depth): `python -I`, empty environment, rlimits (CPU,
  memory, open files, file size), a 30 s wall clock after which the whole process group
  is killed, a 5 MiB output cap streamed, 2000 output rows, and at most
  `TRET_UNVETTED_ANALYSIS_MAX_CONCURRENT` (default 2) analyses at once; others wait up
  to 60 s and then get "busy, try again".
- **Language** (defence in depth, not a boundary): AST allow-list (imports only `math,
  statistics, json, csv, decimal, fractions, collections, itertools, functools,
  datetime, re`; no `open`, `exec`, `eval`, `compile`, `__import__`, `getattr`/
  `setattr`/`delattr`, `globals`, `locals`, `vars`, `input`, `breakpoint`, `type`,
  `object`, `super`, `dir`; no identifier or attribute starting with `_`; no
  `.format`/`.format_map`; no frame/code/traceback attributes; no `match` statements,
  whose class patterns read attributes with a real `getattr`; no `global`/`nonlocal`;
  no `functools.update_wrapper`/`wraps`-style helpers that copy attributes by name), a
  positive builtin allow-list, and import snapshots without module attributes. Checked
  in the parent and again in the child. A past escape went through this layer, which is
  why it is not trusted as the boundary.
- **Credentials**: when the app runs as root the child drops to uid/gid 65534. (The Fly
  image drops to uid 10001 before starting the app, so there this step is idle.)

Limits, honestly: a CPU-bound regular expression or loop is stopped by the CPU limit
and wall clock, not prevented. Inputs are the whole of the datasets named, so an
analysis can read everything in them. The sandbox shares the host kernel, so a kernel
namespace or privilege-escalation bug would defeat it; keep the host patched.

## Handing work between sessions (R09)

Week 3's session saves into `<session folder>/tret/` (create-only). The Week 4 session
is started from the same group, so it inherits the same folders and reads the earlier
output with `read_connected_file`. Nothing is carried in chat history.

## Roles and automatic approval

The cohort edition hides the Approvals page. It relies on the deploy's
`TRET_AUTO_APPROVE_DEFAULT` to approve agent output, including SharePoint and OneDrive
write-back, as soon as it is created. Automatic approval only signs off in the name of
someone who could approve by hand: a run started by an approver, admin or owner is approved
automatically, and a run started by an analyst leaves a draft.

Every member owns their personal workspace, so this changes nothing there. In a team
workspace, invite the people who produce outputs as approvers. An analyst's drafts would
otherwise wait on a page the edition does not show; switching the workspace's edition to
`standard` brings the page back for whoever reviews them.
