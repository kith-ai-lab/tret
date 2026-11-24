// Which bundled dataset a `dataset`-rung grid factor came from, read off what the
// run itself recorded. No imports on purpose: tests/gridDataset.test.ts loads this
// file directly under node, so it must not pull in React or the API client.
//
// The backend records the choice twice (tret/services/emission_factors.py):
//   * `grid_co2e_source`  — "dataset:ember:country-<ISO3>" or "dataset:zone:<zone>"
//   * `grid_dataset_version` — "ember-yearly-…" or "electricity-maps-bundled-yearly"
// The source key is checked first because it names the rule that actually chose
// the factor; the version is the fallback for a record whose key was trimmed.
// Anything else is `null`, and the UI then says neither name nor licence.

export type GridDataset = 'ember' | 'electricity_maps'

export interface GridDatasetMeta {
  /** Short noun for the table cell, e.g. "Ember country average". */
  label: string
  /** The dataset's name as an attribution line spells it. */
  name: string
  /** Attribution + licence, worded as THIRD_PARTY_DATA.md expects. */
  attribution: string
}

export const GRID_DATASET_META: Record<GridDataset, GridDatasetMeta> = {
  ember: {
    label: 'Ember country average',
    name: 'Ember',
    attribution: 'Ember (2026). Yearly Electricity Data. Licensed under CC BY 4.0.',
  },
  electricity_maps: {
    label: 'Electricity Maps zone average',
    name: 'Electricity Maps',
    attribution:
      'Contains data from Electricity Maps (https://www.electricitymaps.com), licensed under the Open Database License (ODbL) v1.0.',
  },
}

export function gridDatasetOf(
  source: string | null | undefined,
  datasetVersion?: string | null,
): GridDataset | null {
  const parts = (source ?? '').split(':')
  if (parts[0] === 'dataset') {
    if (parts[1] === 'ember') return 'ember'
    if (parts[1] === 'zone') return 'electricity_maps'
  }
  const version = (datasetVersion ?? '').toLowerCase()
  if (parts[0] === 'dataset' || (source ?? '') === '') {
    if (version.startsWith('ember')) return 'ember'
    if (version.startsWith('electricity-maps')) return 'electricity_maps'
  }
  return null
}
