/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { fieldDefsFromColumns, slotFeatureFromList } from './publicFeature.ts'

const FID = '0b9d2c1e-7a55-4f0e-9d0a-3f3f5b1c2a10'
const OTHER = '11111111-2222-4333-8444-555555555555'

function listAnswer(id: string, properties: Record<string, unknown>): unknown {
  return {
    type: 'FeatureCollection', numberMatched: 1, numberReturned: 1,
    features: [{ type: 'Feature', id, geometry: { type: 'Point', coordinates: [-82.5, 27.3] }, properties }],
  }
}

test('slotFeatureFromList: the list row becomes the flat feature the popup reads', () => {
  const got = slotFeatureFromList(
    listAnswer(FID, {
      notes: 'near the corner', objectid: 12, row_version: 'abc',
      created_at: '2026-11-06T15:33:11Z', updated_at: '2026-11-06T15:33:12Z',
      props: { stopname: 'MAIN ST @ 1ST AVE', lastupdate: '2026-04-23' },
    }),
    FID,
  )
  assert.deepEqual(got, {
    id: FID,
    geometry: { type: 'Point', coordinates: [-82.5, 27.3] },
    notes: 'near the corner',
    props: { stopname: 'MAIN ST @ 1ST AVE', lastupdate: '2026-04-23' },
    created_at: '2026-11-06T15:33:11Z',
    updated_at: '2026-11-06T15:33:12Z',
  })
})

test('slotFeatureFromList: only named keys are copied', () => {
  // Nothing here is sent to a signed-out caller today. If one of them ever were, it must not ride
  // into the popup because the reshaping copied the whole row.
  const got = slotFeatureFromList(
    listAnswer(FID, {
      notes: null, props: {}, ai_provenance: { model: 'x' }, created_by: 'u1', created_by_name: 'A Person',
      updated_by_name: 'A Person', row_version: 'abc', objectid: 3,
    }),
    FID,
  )
  assert.deepEqual(got, { id: FID, geometry: { type: 'Point', coordinates: [-82.5, 27.3] }, notes: null, props: {} })
})

test('slotFeatureFromList: null unless the answer holds the feature that was asked for', () => {
  assert.equal(slotFeatureFromList(listAnswer(OTHER, { props: { a: 1 } }), FID), null)
  assert.equal(slotFeatureFromList({ type: 'FeatureCollection', features: [] }, FID), null)
  assert.equal(slotFeatureFromList({ detail: 'not found' }, FID), null)
  assert.equal(slotFeatureFromList(null, FID), null)
  assert.equal(slotFeatureFromList({ features: [null, 'x', { id: FID, properties: 'nope' }] }, FID)?.id, FID)
})

test('slotFeatureFromList: a malformed row degrades to an empty feature, never throws', () => {
  assert.deepEqual(slotFeatureFromList({ features: [{ id: FID, properties: { notes: 7, props: [1, 2] } }] }, FID), {
    id: FID, notes: null, props: {},
  })
})

test('fieldDefsFromColumns: owner-defined fields only, in the order given, with their labels', () => {
  const got = fieldDefsFromColumns({
    layerId: 'tal_00000000',
    columns: [
      { name: 'id', type: 'uuid', domain: null },
      { name: 'notes', type: 'text', domain: null },
      { name: 'stopname', type: 'text', domain: null, props: true, label: 'Stop name' },
      { name: 'shelter', type: 'text', domain: null, props: true, label: 'Shelter', customDomain: 'd1' },
      { name: 'bare', type: 'text', domain: null, props: true },
      { name: '', type: 'text', props: true, label: 'no key' },
      { type: 'text', props: true, label: 'no name' },
      null,
    ],
  })
  assert.deepEqual(got, [
    { fieldKey: 'stopname', label: 'Stop name', active: true, domainId: null },
    { fieldKey: 'shelter', label: 'Shelter', active: true, domainId: null }, // a picklist keeps its raw code
    { fieldKey: 'bare', label: 'bare', active: true, domainId: null },
  ])
  assert.deepEqual(fieldDefsFromColumns(null), [])
  assert.deepEqual(fieldDefsFromColumns({ columns: 'x' }), [])
})
