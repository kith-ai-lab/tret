# Third-party data bundled with tret

tret ships three third-party datasets used by the emissions and water
accounting paths. All are derived databases under their upstream licenses; this
file is their NOTICE.

## Ember Yearly Electricity Data

- **File:** `backend/tret/data/grid_ember_2025.json`
- **License:** CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)
- **Attribution:** Ember (2026). Yearly Electricity Data. Licensed under
  CC BY 4.0. https://ember-energy.org/data/yearly-electricity-data/
- **Source:** https://files.ember-energy.org/public-downloads/yearly_full_release_long_format.csv
- Provides the shipped global-default grid intensity (World 2025,
  458.49 gCO2e/kWh, lifecycle all-GHG, 100-year) and the explicit
  `country-ISO3` pins. Imported offline by
  `python -m tret.services.grid_ember`; see `docs/emissions-methodology.md`
  and `docs/grid-zones.md`.

## Electricity Maps zone table

- **File:** `backend/tret/data/grid_zones.json`
- **License:** Open Database License (ODbL) v1.0
  (https://opendatacommons.org/licenses/odbl/1-0/), which permits commercial
  use with attribution and requires a derived database — this JSON file is
  one — stay under ODbL too.
- **Attribution:** Contains data from Electricity Maps
  (https://www.electricitymaps.com), licensed under the Open Database
  License (ODbL) v1.0.
- **Source:** https://www.electricitymaps.com/data-portal
- Ships as a skeleton with **zero zones** — see `docs/grid-zones.md` for why
  and how an operator imports their own yearly CSV downloads to populate it.

## WRI water use embedded in purchased electricity

- **File:** `backend/tret/data/water_factors.json`
- **License:** CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)
- **Attribution:** Reig, P., Luo, T., Christensen, E., Sinistore, J. (2020).
  Guidance for Calculating Water Use Embedded in Purchased Electricity. World
  Resources Institute. Licensed under CC BY 4.0.
- **Source:** https://www.wri.org/research/guidance-calculating-water-use-embedded-purchased-electricity
- Provides the world-average grid water factor (1.27 gal/kWh = 4.81 L/kWh,
  Table 4) and a few country factors (Appendix 2). Only WRI's published
  country factors are republished; no underlying GaBi data is included. See
  `docs/water-methodology.md`.
- The same file also carries one cited constant, the site water-usage
  effectiveness default (0.375 L/kWh), derived from figures in Shehabi et al.,
  *2024 United States Data Center Energy Usage Report*, Lawrence Berkeley
  National Laboratory (a US government-funded report), with citation:
  https://escholarship.org/content/qt32d6m0d1/qt32d6m0d1.pdf
