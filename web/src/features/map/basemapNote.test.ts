/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isStreamedBasemap, showNoBasemapNote, type Basemap } from './basemapNames.ts'

// Once-over C2: "No basemap installed yet" sat over every map on a new install, where nothing is
// downloaded and OpenStreetMap (streamed) is drawing — for signed-out viewers of a public embed too.
test('no archive + a streamed look drawing: the notice stays away', () => {
  for (const look of ['osm', 'imagery', 'usgstopo'] as Basemap[]) {
    if (!isStreamedBasemap(look)) continue
    assert.equal(showNoBasemapNote(true, false, look), false, look)
  }
})

test('the fixture can fail: at least one look here really is a streamed one', () => {
  assert.equal(isStreamedBasemap('osm' as Basemap), true)
})

test('no archive + a downloaded look drawing: the notice shows (the gray void it explains)', () => {
  for (const look of ['light', 'dark', 'grayscale'] as Basemap[]) {
    assert.equal(isStreamedBasemap(look), false, look)
    assert.equal(showNoBasemapNote(true, false, look), true, look)
  }
})

test('dismissed, or an archive present: no notice', () => {
  assert.equal(showNoBasemapNote(true, true, 'light' as Basemap), false)
  assert.equal(showNoBasemapNote(false, false, 'light' as Basemap), false)
})
