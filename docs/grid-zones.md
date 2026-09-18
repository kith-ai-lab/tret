# Grid zone table

`backend/tret/data/grid_zones.json`, read via `tret.services.grid_zones`, is
a bundled table of published annual grid carbon intensities, one entry per
[Electricity Maps](https://www.electricitymaps.com) zone (`DE`, `US-MIDA-PJM`,
`SE-SE3`, ...). Alongside it, `REGION_ALIASES` maps common cloud provider
region names (AWS/GCP/Azure) to the zone whose grid physically serves that
region's data centres. Together they let an operator-pinned region
(`grid.regions: {provider: region}`, see
`backend/tret/services/grid_regions.py`) resolve to a real published figure
instead of requiring the operator to type a `provider@region` grid entry by
hand.

As shipped, `grid_zones.json` has **zero zones** — it is a skeleton. A
maintainer generates the real data with the importer described below and
commits the result.

## Licensing

Electricity Maps publishes free downloadable datasets — hourly, daily,
monthly, and yearly carbon intensity, 2021-2025, 160+ zones — at
[electricitymaps.com/data-portal](https://www.electricitymaps.com/data-portal)
(a free account is required to download; the data itself costs nothing).
These datasets are licensed under the **Open Database License (ODbL) v1.0**
(<https://opendatacommons.org/licenses/odbl/1-0/>), which permits commercial
use with attribution, and requires that a *derived database* — which
`grid_zones.json` is — be shared under ODbL as well.

So: `backend/tret/data/grid_zones.json` is an ODbL-licensed derived
database, bundled alongside tret's Apache-2.0 code. The JSON file carries
its own attribution, license, and license-URL fields (`attribution`,
`license`, `license_url`, `download_url`) so that provenance travels with
the data itself, not just with this document. The Apache-2.0 license covers
`tret.services.grid_zones` (the code that reads the file); it does not, and
could not, relicense the data the file contains.

**The live Electricity Maps API, and tret's own internal endpoints, are not
a source for this table and must never be fetched.** The table is refreshed
offline, by a maintainer, from downloaded CSVs — see Generating below. tret's
promise of no network calls except to configured LLM providers (plus an
optional model-catalog fetch) holds for this table exactly as it does for
everything else: nothing in `tret.services.grid_zones` makes a network call.

## Why a zone only applies when pinned

Grid location is operator-supplied configuration, never inferred — the same
stance `docs/emissions-methodology.md` takes for provider grid factors
generally (see its ["Why this is configuration and not
geolocation"](./emissions-methodology.md#why-this-is-configuration-and-not-geolocation)
section). Hosted LLM API providers do not disclose which region actually
served a given request, and a router can send two identical calls to two
different regions. So a zone figure from this table is only ever *reached* —
after an operator has explicitly pinned a provider to a region in workspace
settings — never guessed from a provider name, an IP address, or any other
signal.

## Generating the table

1. Download one or more **yearly** CSVs from the
   [data portal](https://www.electricitymaps.com/data-portal) (per zone, or
   the bulk yearly export — either works; the importer merges by zone and
   keeps the most recent year it finds for each).
2. Run the importer:

   ```
   cd backend && .venv/bin/python -m tret.services.grid_zones import path/to/*.csv
   ```

   This writes `backend/tret/data/grid_zones.json` (pass `--out PATH` to
   write elsewhere). Column headers are matched by regex, not exact name,
   because Electricity Maps has varied the wording across releases (e.g.
   `(Life cycle)` vs `(LCA)`); a file missing a recognisable zone-id or
   lifecycle-intensity column, or carrying a non-numeric value, aborts the
   import naming the offending file (and row, for a bad value).
3. Inspect what got resolved:

   ```
   .venv/bin/python -m tret.services.grid_zones show eu-central-1 us-east-1
   ```

   With no region arguments, `show` prints the zone count and the ODbL
   attribution string.

Every value in the table is the **lifecycle** (well-to-wheel) figure —
`g_per_kwh` — matching the basis tret's other bundled factors use. The
direct (combustion-only) figure is kept as `direct_g_per_kwh` for reference
but is never what gets surfaced.

## Region aliases, by cloud

`REGION_ALIASES` is a maintained best-effort table of each cloud provider's
region *name* to the Electricity Maps zone physically hosting it — not a
provider disclosure (providers don't publish this), just public information
about where a region's data centres sit. Summary by cloud (see
`backend/tret/services/grid_zones.py` for the full, current table):

- **AWS** — all standard `us-*`, `ca-*`, `eu-*`, `ap-*`, `sa-east-1`,
  `me-*`, `il-central-1`, and `af-south-1` regions.
- **GCP** — all standard `us-*`, `northamerica-*`, `europe-*`, `asia-*`,
  `australia-*`, `southamerica-*`, `me-*`, and `africa-south1` regions.
- **Azure** — the standard `eastus`/`westus`/etc. US regions, `canada*`,
  the European regions, `japaneast`/`japanwest`, `koreacentral`,
  `eastasia`/`southeastasia`, the `australia*` regions, `*india` regions,
  `brazilsouth`, `southafricanorth`, `uaenorth`, `qatarcentral`, and
  `israelcentral`.

An operator can also pin straight to an Electricity Maps zone id itself
(e.g. `"DE"`, `"US-MIDA-PJM"`) instead of a cloud region name: a pinned
region that matches no alias but has the shape of a zone id is looked up in
the table as-is (`zone_for_region` in `grid_zones.py`). Aliases are checked
first because cloud region names such as `eu-central-1` also fit that shape.

## Where it sits in the factor ladder

`_resolve_grid` in `tret.services.emission_factors` consults the table as the
`dataset` rung, **between `env` and `global_default`**:

```
run_override > harness > workspace > managed > env > dataset > global_default
```

It is reached only when (a) a workspace has pinned the run's provider to a
region (`grid.regions`) and (b) nothing an operator set priced that provider
— not a `provider@region` entry, a bare `provider` entry or a `grid.default`
in a harness/workspace/managed document, not a `TRET_GRID_FACTORS` entry,
not the legacy `TRET_LOCAL_GRID_CO2E_G_PER_KWH`, and not an explicitly set
`TRET_GRID_CO2E_G_PER_KWH`. Each of those is something an operator wrote
down for this deployment, and a region pin says where the load ran, not
that the operator's figure (or its GHG Protocol basis — a market-based PPA
figure must never be silently swapped for a location-based average) should
be discarded. The dataset therefore displaces only tret's shipped Ember world
default. A pinned region the table has no zone for, or a table that cannot
be read, falls through to `global_default` exactly as before. An unpinned
provider never touches this rung.

The run records `grid_co2e_layer = "dataset"`, `grid_co2e_source =
"dataset:zone:<ZONE>"`, the zone's label/url/as-of, and the region that was
pinned. While the bundled table is empty (see "Generating the table"), the
rung is inert and every run resolves as it did before.
