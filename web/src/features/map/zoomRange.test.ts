/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  describeRange,
  editorFrom,
  editorProblem,
  formatScale,
  FULL_RANGE,
  hasRange,
  inRange,
  legendVisibility,
  layerBands,
  layerPairFrom,
  mapPairFrom,
  nearestInRange,
  outOfRangeNote,
  rangeInForce,
  scaleAt,
  widensOut,
  ZOOM_CEILING,
  ZOOM_STOPS,
  type ZoomPair,
} from './zoomRange.ts'
import type { TocItem } from './LayerToggle.tsx'

// --- which range is in force (decision-layer-zoom-range.md §3.1) ---------------------------

test('nothing set anywhere: every zoom', () => {
  assert.deepEqual(rangeInForce(undefined, undefined), FULL_RANGE)
  assert.deepEqual(rangeInForce({}, { visibleMinZoom: null, visibleMaxZoom: null }), FULL_RANGE)
})

test('the layer default applies when the map has no range of its own', () => {
  assert.deepEqual(rangeInForce({}, { visibleMinZoom: 14 }), { min: 14, max: ZOOM_CEILING })
})

test("the map's pair replaces the layer's AS A PAIR — never one end from each", () => {
  // Layer: 14–18. Map: only a top of 12. The map's range is 0–12, not 14–12 and not 14–18.
  assert.deepEqual(
    rangeInForce({ visibleMaxZoom: 12 }, { visibleMinZoom: 14, visibleMaxZoom: 18 }),
    { min: 0, max: 12 },
  )
})

test('a map may widen the layer default (it is not stopped, §3.1)', () => {
  assert.deepEqual(rangeInForce({ visibleMinZoom: 10 }, { visibleMinZoom: 14 }), { min: 10, max: ZOOM_CEILING })
})

test('a crossed pair (hand-edited document) draws nowhere rather than everywhere', () => {
  const r = rangeInForce({ visibleMinZoom: 16, visibleMaxZoom: 12 }, undefined)
  assert.equal(r.min, r.max)
})

test('out-of-scale numbers are clamped to 0–24', () => {
  assert.deepEqual(rangeInForce({ visibleMinZoom: -3, visibleMaxZoom: 99 }, undefined), FULL_RANGE)
})

test('hasRange: either end counts, nulls do not', () => {
  assert.equal(hasRange({ visibleMinZoom: 0 }), true)
  assert.equal(hasRange({ visibleMaxZoom: 12 }), true)
  assert.equal(hasRange({ visibleMinZoom: null }), false)
  assert.equal(hasRange(undefined), false)
})

test('inRange: lowest inclusive, highest exclusive; no top bound stays open at the ceiling', () => {
  const r = { min: 14, max: 18 }
  assert.equal(inRange(r, 13.99), false)
  assert.equal(inRange(r, 14), true)
  assert.equal(inRange(r, 17.99), true)
  assert.equal(inRange(r, 18), false)
  assert.equal(inRange(FULL_RANGE, ZOOM_CEILING), true)
})

// --- the bands for each render piece (§4.1) -----------------------------------------------

test('no range, no icon: today exactly (circle everywhere, icon sibling from the icon zoom)', () => {
  const b = layerBands(FULL_RANGE, { iconZoom: 14, hasIcon: false })
  assert.deepEqual(b.circle, FULL_RANGE)
  assert.deepEqual(b.icon, { min: 14, max: ZOOM_CEILING })
  assert.deepEqual(b.label, FULL_RANGE)
})

test('no range, with an icon: circles below the icon zoom, icons from it (today exactly)', () => {
  const b = layerBands(FULL_RANGE, { iconZoom: 14, hasIcon: true })
  assert.deepEqual(b.circle, { min: 0, max: 14 })
  assert.deepEqual(b.icon, { min: 14, max: ZOOM_CEILING })
})

test('range 12–20 with an icon at 14: circles 12–14, icons 14–20', () => {
  const b = layerBands({ min: 12, max: 20 }, { iconZoom: 14, hasIcon: true })
  assert.deepEqual(b.circle, { min: 12, max: 14 })
  assert.deepEqual(b.icon, { min: 14, max: 20 })
})

test('icon zoom below the range: no circles at all, icons across the whole range', () => {
  const b = layerBands({ min: 16, max: 20 }, { iconZoom: 10, hasIcon: true })
  assert.equal(b.circle.min, b.circle.max)
  assert.deepEqual(b.icon, { min: 16, max: 20 })
})

test('icon zoom above the range: circles across the whole range, no icons', () => {
  const b = layerBands({ min: 10, max: 13 }, { iconZoom: 14, hasIcon: true })
  assert.deepEqual(b.circle, { min: 10, max: 13 })
  assert.equal(b.icon.min, b.icon.max)
})

test('labels start at their own zoom or the layer lowest, whichever is higher, and stop with the layer', () => {
  assert.deepEqual(layerBands({ min: 14, max: 18 }, { iconZoom: 14, hasIcon: false, labelMinZoom: 16 }).label, {
    min: 16,
    max: 18,
  })
  assert.deepEqual(layerBands({ min: 14, max: 18 }, { iconZoom: 14, hasIcon: false, labelMinZoom: 10 }).label, {
    min: 14,
    max: 18,
  })
  const above = layerBands({ min: 10, max: 12 }, { iconZoom: 14, hasIcon: false, labelMinZoom: 15 }).label
  assert.equal(above.min, above.max) // a label never shows where its layer does not
})

test('every band lies inside the range and is never inverted', () => {
  for (const r of [FULL_RANGE, { min: 14, max: 18 }, { min: 0, max: 3 }, { min: 22, max: 24 }, { min: 9, max: 9 }]) {
    for (const iconZoom of [0, 9, 14, 23, 24]) {
      for (const labelMinZoom of [undefined, 0, 12, 20]) {
        for (const hasIcon of [true, false]) {
          const bands = layerBands(r, { iconZoom, hasIcon, labelMinZoom })
          for (const band of Object.values(bands)) {
            assert.ok(band.min <= band.max, `inverted ${JSON.stringify(band)}`)
            assert.ok(band.min >= r.min && band.max <= r.max, `outside ${JSON.stringify(band)} for ${JSON.stringify(r)}`)
          }
        }
      }
    }
  }
})

// --- the control's rules (§4.2) ---------------------------------------------------------------

test('scale: zoom 14 at the equator is about 1:18,000 (MapLibre 512-px tiles), smaller further north', () => {
  assert.equal(formatScale(scaleAt(14, 0)), '1:18,000')
  assert.ok(scaleAt(14, 45) < scaleAt(14, 0))
  assert.equal(formatScale(scaleAt(15, 0)), '1:9,000') // one zoom in halves the denominator
})

test('formatScale rounds to two significant figures', () => {
  assert.equal(formatScale(14_123), '1:14,000')
  assert.equal(formatScale(2_345_678), '1:2,300,000')
  assert.equal(formatScale(845), '1:850')
  assert.equal(formatScale(0), '')
})

test('stops run outward to inward, inside 0–24', () => {
  for (let i = 1; i < ZOOM_STOPS.length; i++) assert.ok(ZOOM_STOPS[i].zoom > ZOOM_STOPS[i - 1].zoom)
  for (const s of ZOOM_STOPS) assert.ok(s.zoom > 0 && s.zoom < ZOOM_CEILING)
})

test('editor: 0 and 24 read as no limit; a stored pair round-trips', () => {
  assert.deepEqual(editorFrom({ visibleMinZoom: 0, visibleMaxZoom: 24 }), { min: null, max: null })
  assert.deepEqual(editorFrom({ visibleMinZoom: 14 }), { min: 14, max: null })
  assert.deepEqual(editorFrom(null), { min: null, max: null })
})

test('editorProblem: crossed, equal and out-of-scale ends are refused; open ends are fine', () => {
  assert.equal(editorProblem({ min: null, max: null }), null)
  assert.equal(editorProblem({ min: 14, max: 18 }), null)
  assert.match(editorProblem({ min: 18, max: 14 }) ?? '', /below/)
  assert.match(editorProblem({ min: 14, max: 14 }) ?? '', /below/)
  assert.match(editorProblem({ min: 30, max: null }) ?? '', /0 to 24/)
})

test('a layer default with no limit at either end is no default at all', () => {
  assert.equal(layerPairFrom({ min: null, max: null }), null)
  assert.deepEqual(layerPairFrom({ min: 14, max: null }), { visibleMinZoom: 14, visibleMaxZoom: null })
})

test("a map's own 'every zoom' is still its own range, so it overrides (widens past) the default", () => {
  const p = mapPairFrom({ min: null, max: null })
  assert.equal(hasRange(p), true)
  assert.deepEqual(rangeInForce(p, { visibleMinZoom: 14 }), FULL_RANGE)
  assert.deepEqual(mapPairFrom({ min: null, max: 12 }), { visibleMinZoom: null, visibleMaxZoom: 12 })
})

test('widensOut: only when the map starts further out than the default', () => {
  assert.equal(widensOut({ visibleMinZoom: 10 }, { visibleMinZoom: 14 }), true)
  assert.equal(widensOut({ visibleMinZoom: 0 }, { visibleMinZoom: 14 }), true)
  assert.equal(widensOut({ visibleMinZoom: 16 }, { visibleMinZoom: 14 }), false)
  assert.equal(widensOut({ visibleMinZoom: 10 }, null), false)
  assert.equal(widensOut(undefined, { visibleMinZoom: 14 }), false)
})

test('describeRange: one line per shape', () => {
  assert.equal(describeRange(FULL_RANGE), 'Every zoom')
  assert.equal(describeRange({ min: 14, max: ZOOM_CEILING }), 'From zoom 14')
  assert.equal(describeRange({ min: 0, max: 12 }), 'Up to zoom 12')
  assert.equal(describeRange({ min: 14, max: 18 }), 'Zoom 14 to 18')
  assert.equal(describeRange({ min: 9, max: 9 }), 'Not drawn at any zoom')
})

test('outOfRangeNote: nothing when drawn, which end when not', () => {
  const r = rangeInForce(null, { visibleMinZoom: 14, visibleMaxZoom: 18 })
  assert.equal(outOfRangeNote(r, 14), null) // min is inclusive
  assert.equal(outOfRangeNote(r, 17.9), null)
  assert.equal(outOfRangeNote(r, 13.99), 'Shown from zoom 14')
  assert.equal(outOfRangeNote(r, 18), 'Shown up to zoom 18') // max is exclusive
  assert.equal(outOfRangeNote(FULL_RANGE, 0), null)
  assert.equal(outOfRangeNote(FULL_RANGE, ZOOM_CEILING), null)
  assert.equal(outOfRangeNote({ min: 12, max: 12 }, 12), 'Not drawn at any zoom')
  assert.equal(outOfRangeNote(rangeInForce(null, { visibleMinZoom: 14.25 }), 3), 'Shown from zoom 14.3')
})

test('nearestInRange: unchanged inside, the near end outside', () => {
  const r = rangeInForce(null, { visibleMinZoom: 14, visibleMaxZoom: 18 })
  assert.equal(nearestInRange(r, 15.2), 15.2)
  assert.equal(nearestInRange(r, 10), 14)
  assert.equal(nearestInRange(r, 19), 17.9)
  assert.ok(inRange(r, nearestInRange(r, 18))) // the exclusive top comes back inside
  const thin = { min: 14, max: 14.05 }
  assert.ok(inRange(thin, nearestInRange(thin, 20)))
  assert.equal(nearestInRange({ min: 12, max: 12 }, 5), 5) // drawn nowhere: leave the view alone
  assert.equal(nearestInRange(FULL_RANGE, 22), 22)
})

test('legendVisibility: an out-of-range layer is left off as if unticked; the rest untouched', () => {
  const layer = (id: string, renderer: ZoomPair | null) =>
    ({ kind: 'layer', config: { id, renderer } }) as unknown as TocItem
  const items: TocItem[] = [
    { kind: 'sketch', count: 2 },
    layer('parcels', { visibleMinZoom: 14 }),
    layer('limits', { visibleMaxZoom: 14 }),
    layer('roads', null),
    { kind: 'raster', id: 'ortho', title: 'Ortho', opacity: 1 },
  ]
  const ticks = { __sketch: true, roads: false }
  const at10 = legendVisibility(items, ticks, { ortho: { visibleMinZoom: 12 } }, 10)
  assert.deepEqual(at10, { __sketch: true, roads: false, parcels: false, ortho: false })
  const at15 = legendVisibility(items, ticks, {}, 15)
  assert.deepEqual(at15, { __sketch: true, roads: false, limits: false })
  // the map's own pair replaces the layer's: parcels widened to every zoom is on the legend at 10
  const widened = legendVisibility(items, ticks, { parcels: { visibleMinZoom: 0 } }, 10)
  assert.equal(widened.parcels, undefined)
  assert.equal(legendVisibility(items, ticks, {}, null), ticks) // no map yet: the tick boxes alone
})
