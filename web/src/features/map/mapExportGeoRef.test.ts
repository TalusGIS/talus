/// <reference types="node" />
// The acceptance the backlog asked for: parse /GPTS straight out of the PDF bytes, no PDF library.
// Builds a real jsPDF document in node and reads the geospatial dictionaries back. Also the guard
// on a jsPDF bump: the writer rides jsPDF's plugin surface (internal.events / write /
// newAdditionalObject), which this test exercises end to end.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { jsPDF } from 'jspdf'

import { groundCorners, registerGeospatialViewport } from './mapExportGeoRef.ts'

const CORNERS = {
  ul: { lng: -78.88, lat: 43.0 },
  ll: { lng: -78.88, lat: 42.9 },
  lr: { lng: -78.8, lat: 42.9 },
  ur: { lng: -78.8, lat: 43.0 },
}

function bytesOf(doc: jsPDF): string {
  return Buffer.from(doc.output('arraybuffer')).toString('latin1')
}

/** The body of indirect object `id` ("id 0 obj … endobj"). */
function objectBody(pdf: string, id: number): string {
  const m = pdf.match(new RegExp(`(?:^|\\n)${id} 0 obj\\n([\\s\\S]*?)\\nendobj`))
  assert.ok(m, `object ${id} not found`)
  return m[1]
}

function numbers(arrayBody: string): number[] {
  return arrayBody.trim().split(/\s+/).map(Number)
}

test('a georeferenced page carries /VP → Viewport → Measure/GEO → PROJCS, readable from the bytes', () => {
  const doc = new jsPDF({ unit: 'in', format: 'letter', orientation: 'landscape' })
  doc.text('map', 1, 1)
  // The map image's rectangle, as addImage would be called: 0.5 in from the left, 1.12 in from the top.
  registerGeospatialViewport(doc, { x: 0.5, y: 1.12, w: 7.5, h: 5.6 }, CORNERS)
  const pdf = bytesOf(doc)

  // Exactly one page, and it points at exactly one viewport.
  const pages = pdf.match(/<<\/Type \/Page\n[\s\S]*?endobj/g) ?? []
  assert.equal(pages.length, 1)
  const vp = pages[0].match(/\/VP \[(\d+) 0 R\]/)
  assert.ok(vp, '/VP missing from the page dictionary')
  assert.equal((pdf.match(/\/VP \[/g) ?? []).length, 1)

  const viewport = objectBody(pdf, Number(vp[1]))
  assert.match(viewport, /\/Type \/Viewport/)
  // BBox in points, origin bottom-left: x 0.5→8 in, y (8.5-1.12-5.6)→(8.5-1.12) in.
  const bbox = numbers(viewport.match(/\/BBox \[([^\]]+)\]/)![1])
  assert.deepEqual(
    bbox.map((n) => Math.round(n * 100) / 100),
    [36, 128.16, 576, 531.36],
  )

  const measure = objectBody(pdf, Number(viewport.match(/\/Measure (\d+) 0 R/)![1]))
  assert.match(measure, /\/Type \/Measure/)
  assert.match(measure, /\/Subtype \/GEO/)
  assert.deepEqual(numbers(measure.match(/\/LPTS \[([^\]]+)\]/)![1]), [0, 1, 0, 0, 1, 0, 1, 1])
  assert.deepEqual(numbers(measure.match(/\/Bounds \[([^\]]+)\]/)![1]), [0, 1, 0, 0, 1, 0, 1, 1])
  // GPTS: UL, LL, LR, UR — latitude then longitude.
  const gpts = numbers(measure.match(/\/GPTS \[([^\]]+)\]/)![1])
  assert.deepEqual(gpts, [43, -78.88, 42.9, -78.88, 42.9, -78.8, 43, -78.8])

  const gcs = objectBody(pdf, Number(measure.match(/\/GCS (\d+) 0 R/)![1]))
  assert.match(gcs, /\/Type \/PROJCS/)
  assert.match(gcs, /\/EPSG 3857/)
  assert.match(gcs, /\/WKT \(PROJCS\["WGS_1984_Web_Mercator_Auxiliary_Sphere"/)
  assert.match(gcs, /PROJECTION\["Mercator_Auxiliary_Sphere"\]/)
})

test('an unregistered document has no geospatial dictionaries (the tilted-view case)', () => {
  const doc = new jsPDF({ unit: 'in', format: 'letter', orientation: 'landscape' })
  doc.text('map', 1, 1)
  const pdf = bytesOf(doc)
  assert.doesNotMatch(pdf, /\/VP \[/)
  assert.doesNotMatch(pdf, /\/GPTS/)
})

test('groundCorners reads the canvas corners in UL, LL, LR, UR order', () => {
  // A fake unproject: x → longitude, y → latitude (downwards = south).
  const c = groundCorners((x, y) => ({ lng: -80 + x / 100, lat: 43 - y / 100 }), 800, 600)
  assert.deepEqual(c, {
    ul: { lng: -80, lat: 43 },
    ll: { lng: -80, lat: 37 },
    lr: { lng: -72, lat: 37 },
    ur: { lng: -72, lat: 43 },
  })
})

test('coordinates are written without exponent notation or trailing zeros', () => {
  const doc = new jsPDF({ unit: 'in', format: 'letter', orientation: 'portrait' })
  doc.text('map', 1, 1)
  registerGeospatialViewport(
    doc,
    { x: 0.5, y: 0.5, w: 7.5, h: 9 },
    { ul: { lng: 0.00000001, lat: 0 }, ll: { lng: 0, lat: -0.5 }, lr: { lng: 1, lat: -0.5 }, ur: { lng: 1, lat: 0 } },
  )
  const pdf = bytesOf(doc)
  const gpts = pdf.match(/\/GPTS \[([^\]]+)\]/)![1]
  assert.equal(gpts, '0 0.00000001 -0.5 0 -0.5 1 0 1')
  assert.doesNotMatch(pdf.match(/\/BBox \[([^\]]+)\]/)![1], /e/i)
})
