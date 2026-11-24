import { test } from 'node:test'
import assert from 'node:assert/strict'
import { gridDatasetOf, GRID_DATASET_META } from '../src/components/shared/gridDataset.ts'

test('Ember country factors are Ember, CC BY 4.0', () => {
  assert.equal(gridDatasetOf('dataset:ember:country-AUS', 'ember-yearly-2026-release'), 'ember')
  assert.match(GRID_DATASET_META.ember.attribution, /Ember/)
  assert.match(GRID_DATASET_META.ember.attribution, /CC BY 4\.0/)
  assert.doesNotMatch(GRID_DATASET_META.ember.attribution, /ODbL/)
})

test('Electricity Maps zone factors are ODbL', () => {
  assert.equal(gridDatasetOf('dataset:zone:DE', 'electricity-maps-bundled-yearly'), 'electricity_maps')
  assert.match(GRID_DATASET_META.electricity_maps.attribution, /Electricity Maps/)
  assert.match(GRID_DATASET_META.electricity_maps.attribution, /ODbL/)
})

test('the version identifies the dataset when the source key does not', () => {
  assert.equal(gridDatasetOf('dataset', 'ember-yearly-2026-release'), 'ember')
  assert.equal(gridDatasetOf('dataset', 'electricity-maps-bundled-yearly'), 'electricity_maps')
})

test('unknown stays unknown, and non-dataset rungs are never claimed', () => {
  assert.equal(gridDatasetOf('dataset:something-else:x', null), null)
  assert.equal(gridDatasetOf('dataset', null), null)
  assert.equal(gridDatasetOf('global_default', 'ember-yearly-2026-release'), null)
  assert.equal(gridDatasetOf('provider:anthropic', 'electricity-maps-bundled-yearly'), null)
})
