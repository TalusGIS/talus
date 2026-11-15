/// <reference types="node" />
// A raster recorded before the map object exists must still be added to the map.
//
// The defect (found 2026-11-14 with the first raster ever uploaded to the development stack): a
// raster opened from the catalog (/map?raster=<id>) sat in the layer list undrawn. The effect that
// adds raster layers gives up while there is no map, and was re-run only by the raster list,
// visibility and layer order — all of which had settled before the map was built.
//
// Nothing in this repository renders the map component, so this is a check of the source: the
// effect must be re-run by `mapBuilt`, and the effect that builds the map must raise it. The proof
// that the layer draws is a browser pass (recorded in the decision document); this keeps the two
// lines from being tidied away.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const source = readFileSync(new URL('./AssetMap.tsx', import.meta.url), 'utf8')

/** The dependency list that closes the effect containing `marker`. */
function depsOfEffectContaining(marker: string): string {
  const at = source.indexOf(marker)
  assert.notEqual(at, -1, `marker not found: ${marker}`)
  const close = /\n {2}\}, \[([^\]]*)\]\)\n/g
  close.lastIndex = at
  const m = close.exec(source)
  assert.ok(m, `no dependency list after: ${marker}`)
  return m[1]
}

test('the effect that adds raster layers runs again once the map is built', () => {
  const deps = depsOfEffectContaining("{ type: 'raster', url, tileSize: 256 }")
  assert.match(deps, /\bmapBuilt\b/, `the raster effect is re-run only by: ${deps}`)
  // …and still by everything it was re-run by before.
  for (const dep of ['rasterMeta', 'visible', 'selected', 'restack']) assert.match(deps, new RegExp(`\\b${dep}\\b`))
})

test('the effect that builds the map raises mapBuilt, right where the map object is kept', () => {
  assert.match(source, /const \[mapBuilt, setMapBuilt\] = useState\(0\)/)
  assert.match(source, /mapRef\.current = map\n\s*setMapBuilt\(\(n\) => n \+ 1\)/)
})

test('the raster effect still gives up while there is no map, and still waits for the style', () => {
  const start = source.indexOf('const rasterUrlRef = useRef')
  const end = source.indexOf("{ type: 'raster', url, tileSize: 256 }")
  assert.ok(start !== -1 && end > start)
  const head = source.slice(start, end)
  assert.match(head, /const map = mapRef\.current\n\s*if \(!map\) return/)
  assert.match(source.slice(end, end + 1600), /return whenStyleReady\(map, apply\)/)
})
