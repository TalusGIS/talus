/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { addressFieldChoices, fieldLabel, geocodeSourceChoices, type SourceLayerRow } from './batchGeocodeSource.ts'

function row(over: Partial<SourceLayerRow>): SourceLayerRow {
  return { id: 'tal_0000000a', canView: true, displayName: 'Permits', featureCount: 12, geomType: 'none', ...over }
}

test('a viewable hosted table is offered, with its row count', () => {
  assert.deepEqual(geocodeSourceChoices([row({})]), [
    { id: 'tal_0000000a', name: 'Permits', label: 'Permits', kind: 'table', rowCount: 12 },
  ])
})

test('two choices with one name are told apart by owner; a unique name is left alone', () => {
  const labels = geocodeSourceChoices([
    row({ id: 'tal_00000011', displayName: 'Stops', ownerName: 'A. Planner' }),
    row({ id: 'tal_00000012', displayName: 'Stops', ownerName: 'B. Clerk' }),
    row({ id: 'tal_00000013', displayName: 'Permits', ownerName: 'A. Planner' }),
  ]).map((c) => c.label)
  assert.deepEqual(labels, ['Permits', 'Stops — A. Planner', 'Stops — B. Clerk'])
})

test('what the run door would refuse is not offered', () => {
  const offered = geocodeSourceChoices([
    row({ id: 'tal_0000000b', canView: false }),                      // not viewable
    row({ id: 'tal_0000000c', renderMode: 'features' }),              // a view layer
    row({ id: 'tal_0000000d', sourceLayerId: 'tal_0000000a' }),       // a view layer, by lineage
    row({ id: 'street_lights' }),                                     // not a hosted slot id
    row({ id: 'tal_0000000e', displayName: null }),                   // an unclaimed slot
    row({ id: 'tal_0000000f', displayName: '   ' }),
  ])
  assert.deepEqual(offered, [])
})

test('tables come first, then layers, each by name', () => {
  const names = geocodeSourceChoices([
    row({ id: 'tal_00000001', displayName: 'Zoning cases', geomType: 'polygon' }),
    row({ id: 'tal_00000002', displayName: 'Work orders', geomType: 'none' }),
    row({ id: 'tal_00000003', displayName: 'Addresses', geomType: 'point' }),
    row({ id: 'tal_00000004', displayName: 'Billing', geomType: 'none' }),
  ]).map((c) => `${c.kind}:${c.name}`)
  assert.deepEqual(names, ['table:Billing', 'table:Work orders', 'layer:Addresses', 'layer:Zoning cases'])
})

test('an unknown row count stays unknown, never zero', () => {
  assert.equal(geocodeSourceChoices([row({ featureCount: null })])[0].rowCount, null)
  assert.equal(geocodeSourceChoices([row({ featureCount: undefined })])[0].rowCount, null)
  assert.equal(geocodeSourceChoices([row({ featureCount: 0 })])[0].rowCount, 0)
})

test('only owner-defined fields are address choices, keyed by field key, shown by label', () => {
  const r = addressFieldChoices([
    { name: 'id' },
    { name: 'created_at' },
    { name: 'site_addr', props: true, label: 'Site address' },
    { name: 'city', props: true },
    { name: 'zip', props: true, label: 'zip' },
    { name: 'site_addr', props: true, label: 'duplicate' },
  ])
  assert.deepEqual(r.keys, ['site_addr', 'city', 'zip'])
  assert.deepEqual(r.labels, { site_addr: 'Site address' })
})

test('a layer with no owner-defined fields offers nothing', () => {
  assert.deepEqual(addressFieldChoices([{ name: 'id' }, { name: 'notes' }]), { keys: [], labels: {} })
})

test('a column named like an Object.prototype member shows its own name, and sorts', () => {
  // A CSV header or field key can be "constructor"; a bare labels[key] would return a function.
  for (const key of ['constructor', 'toString', 'valueOf', 'hasOwnProperty']) {
    assert.equal(fieldLabel({}, key), key)
  }
  assert.equal(fieldLabel({ constructor: 'Built by' }, 'constructor'), 'Built by')
  const sorted = ['zip', 'constructor', 'addr'].sort((a, b) => fieldLabel({}, a).localeCompare(fieldLabel({}, b)))
  assert.deepEqual(sorted, ['addr', 'constructor', 'zip'])
})

test('layers named like an Object.prototype member are still counted and told apart', () => {
  const labels = geocodeSourceChoices([
    row({ id: 'tal_00000021', displayName: 'constructor', ownerName: 'A. Planner' }),
    row({ id: 'tal_00000022', displayName: 'constructor', ownerName: 'B. Clerk' }),
    row({ id: 'tal_00000023', displayName: 'toString', ownerName: 'A. Planner' }),
  ]).map((c) => c.label)
  assert.deepEqual(labels, ['constructor — A. Planner', 'constructor — B. Clerk', 'toString'])
})
