# Peer taxonomy data (TAXONOMY_VERSION `nace2-tret-1`)

| File | What it is | Source |
|---|---|---|
| `nace_rev2.csv` | NACE Rev. 2 structure: 21 sections, 88 divisions, 272 groups, English titles | Eurostat, *NACE Rev. 2, Statistical classification of economic activities in the European Community* (Regulation (EC) No 1893/2006). Reuse of Eurostat classification data is permitted with acknowledgement: "Source: Eurostat, NACE Rev. 2". |
| `nace_clusters.csv` | Tret industry clusters: one cluster per division, with group-level overrides where a division is mixed (a group code in this file overrides its division) | Tret (ours, not an official classification) |
| `sic_nace.csv` | SIC (1987, the SEC EDGAR list) to NACE Rev. 2 mapping with a `match` grade (`exact`, `close`, `broad`) and a note | Tret, curated by hand from the SIC 1987 and NACE Rev. 2 definitions |

`sic_nace.csv` is **not** an official concordance (there is no official SIC 1987 to NACE Rev. 2
table; the Eurostat and UN correspondence tables go through ISIC). It is division-accurate by
design: `nace_group` is filled only where the SIC heading falls inside one NACE group. `broad`
means the SIC heading spans several NACE divisions (or is a catch-all) and the division given is
the dominant one. Review `broad` rows by hand before relying on a peer pool built from them. `none` marks a code that
has no industry (blank checks 6770, depositary receipts 8880, foreign governments 8888,
non-operating establishments 9995): `division` and `group` are blank and nothing is classified.

GICS is a licensed MSCI/S&P classification and is not used to build pools. The sector names in the
register are used only as a coarse cross-check (`gics_disagrees`), never to assign a cluster.

Changing any file here changes classification results: bump `TAXONOMY_VERSION` in `core.py` (re-exported by `__init__.py`).
