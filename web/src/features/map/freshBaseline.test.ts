/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { moveFreshBaseline } from './freshBaseline.ts'

const sig = (b: string): string => `fresh-map-on:${b}`

test('an untouched new map: the baseline follows the look the portal applies', () => {
  assert.equal(moveFreshBaseline(sig('light'), sig, 'light', 'osm'), sig('osm'))
})

test('…and follows a second move (organization default, then the cold-start fallback)', () => {
  const afterDefault = moveFreshBaseline(sig('light'), sig, 'light', 'gray')
  assert.equal(moveFreshBaseline(afterDefault, sig, 'gray', 'imagery'), sig('imagery'))
})

test('a map the user already changed keeps its baseline, so the change is still reported', () => {
  const changed = 'fresh-map-on:light+a-layer-the-user-added'
  assert.equal(moveFreshBaseline(changed, sig, 'light', 'osm'), changed)
})

test('a saved map or a draft baseline is never touched', () => {
  assert.equal(moveFreshBaseline('__draft__', sig, 'light', 'osm'), '__draft__')
  assert.equal(moveFreshBaseline('saved-map-signature', sig, 'light', 'osm'), 'saved-map-signature')
})

test('no baseline yet, or no move: unchanged', () => {
  assert.equal(moveFreshBaseline(null, sig, 'light', 'osm'), null)
  assert.equal(moveFreshBaseline(sig('osm'), sig, 'osm', 'osm'), sig('osm'))
})

test('the baseline does not follow a move away from a look it was not taken on', () => {
  // baseline taken on gray; the portal reports a move light -> osm (stale prev): leave it
  assert.equal(moveFreshBaseline(sig('gray'), sig, 'light', 'osm'), sig('gray'))
})
