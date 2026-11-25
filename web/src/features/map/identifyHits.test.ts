/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isPolygonType, keepIdentifyHits } from './identifyHits.ts'

const hit = (key: string, geometryType: string) => ({ key, geometryType })

test('a polygon near the click but not under it is left out', () => {
  // The Reddit case: a click inside one house lot, with the neighbour's edge inside the box.
  const inBox = [hit('parcels:neighbour', 'Polygon'), hit('parcels:clicked', 'Polygon')]
  const kept = keepIdentifyHits(inBox, new Set(['parcels:clicked']))
  assert.deepEqual(kept.map((h) => h.key), ['parcels:clicked'])
})

test('a click on a shared edge keeps both polygons', () => {
  const inBox = [hit('parcels:a', 'Polygon'), hit('parcels:b', 'MultiPolygon')]
  const kept = keepIdentifyHits(inBox, new Set(['parcels:a', 'parcels:b']))
  assert.deepEqual(kept.map((h) => h.key), ['parcels:a', 'parcels:b'])
})

test('polygons stacked on one footprint (condominium units) are all kept', () => {
  const inBox = ['u1', 'u2', 'u3', 'u4'].map((u) => hit(`parcels:${u}`, 'Polygon'))
  const kept = keepIdentifyHits(inBox, new Set(inBox.map((h) => h.key)))
  assert.equal(kept.length, 4)
})

test('a point or a line in the box counts without being under the click', () => {
  const inBox = [
    hit('hydrants:1', 'Point'),
    hit('streets:9', 'LineString'),
    hit('routes:2', 'MultiLineString'),
    hit('stops:5', 'MultiPoint'),
  ]
  const kept = keepIdentifyHits(inBox, new Set())
  assert.equal(kept.length, 4)
})

test('a point over a polygon keeps its place on top, and the polygon still needs the click', () => {
  const inBox = [hit('hydrants:1', 'Point'), hit('parcels:near', 'Polygon'), hit('parcels:under', 'Polygon')]
  const kept = keepIdentifyHits(inBox, new Set(['parcels:under']))
  assert.deepEqual(kept.map((h) => h.key), ['hydrants:1', 'parcels:under'])
})

test('one feature reported twice (fill and outline, or a tile seam) is kept once', () => {
  const inBox = [hit('parcels:a', 'Polygon'), hit('parcels:a', 'Polygon'), hit('streets:9', 'LineString'), hit('streets:9', 'LineString')]
  const kept = keepIdentifyHits(inBox, new Set(['parcels:a']))
  assert.deepEqual(kept.map((h) => h.key), ['parcels:a', 'streets:9'])
})

test('only Polygon and MultiPolygon are polygons', () => {
  assert.equal(isPolygonType('Polygon'), true)
  assert.equal(isPolygonType('MultiPolygon'), true)
  for (const t of ['Point', 'MultiPoint', 'LineString', 'MultiLineString', 'GeometryCollection', '']) {
    assert.equal(isPolygonType(t), false)
  }
})
