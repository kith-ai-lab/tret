"""Open peer taxonomy for the disclosure evaluation engine: the NACE Rev. 2 ladder,
Tret industry clusters, a curated SIC to NACE mapping and a coarse GICS cross-check.

Pure functions over data files shipped in the package (`data/`, see its README for sources
and attribution); no database and no network. Peer pools are keyed on this ladder
(`pool_key`): group -> division -> cluster -> section. GICS is licensed and never builds a
pool; it only flags a classification a reader should look at (`gics_disagrees`).
"""
from tret.taxonomy.core import (
    CLUSTERS,
    SOURCE_PRECEDENCE,
    TAXONOMY_VERSION,
    SicMapping,
    classify_sic,
    divisions,
    gics_disagrees,
    groups,
    ladder,
    pool_key,
    resolve,
    sections,
)

__all__ = [
    "CLUSTERS",
    "SOURCE_PRECEDENCE",
    "TAXONOMY_VERSION",
    "SicMapping",
    "classify_sic",
    "divisions",
    "gics_disagrees",
    "groups",
    "ladder",
    "pool_key",
    "resolve",
    "sections",
]
