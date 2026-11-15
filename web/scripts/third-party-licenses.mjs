#!/usr/bin/env node
/* ============================================================
   third-party-licenses.mjs — writes dist/third-party-licenses.txt

   The minified bundle carries NO licence comments (Vite's minifier strips every one), yet MIT, BSD
   and ISC all require the copyright and permission notice to travel with copies. So every build
   writes this file beside the bundle; the portal links it ("Open-source licenses") and it is
   readable signed out — Caddy serves it as a static file.

   Contents: every production package in package-lock.json that is installed (an optional package
   not installed for this platform is not in the bundle), each with the licence files from its own
   published package; then the vendored assets in public/. A package that publishes no licence file
   takes its text from third-party/licenses/<name, "/" as "__">/ (SOURCES.toml there records where
   each came from). Each distinct text is reproduced once.

   FAILS CLOSED: a production package with no licence text anywhere stops the build — add its text
   under third-party/licenses/ first. Usage (wired into `npm run build`, after `vite build`):
     node scripts/third-party-licenses.mjs
   VITE_TALUS_VERSION names the release in the header (the release build sets it).
   ============================================================ */
import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const WEB = join(dirname(fileURLToPath(import.meta.url)), '..')
const DIST = join(WEB, 'dist')
const FALLBACK = join(WEB, 'third-party', 'licenses')
const LICENCE_FILE = /^(LICEN[CS]E|COPYING|NOTICE)([-_.].*)?$/i
const RULE = '='.repeat(100)

// Where a package offers a choice, the licence Talus distributes it under.
const ELECTIONS = {
  dompurify: 'Apache-2.0',
  rgbcolor: 'MIT',
}

// Vendored assets under public/, copied into the bundle verbatim.
const ASSETS = [
  {
    what: 'Noto Sans map label glyphs (basemaps-assets/fonts)',
    license: 'OFL-1.1',
    files: [join(WEB, 'public', 'basemaps-assets', 'fonts', 'OFL.txt')],
  },
  {
    what: 'Basemap icon sprites (basemaps-assets/sprites), derived from tangrams/icons',
    license: 'MIT',
    files: listDir(join(FALLBACK, 'basemap-sprites')),
  },
]
// Point symbols (Maki + Temaki) are CC0-1.0 — no notice is required; credited below all the same.

function listDir(dir) {
  return existsSync(dir) ? readdirSync(dir).sort().map((f) => join(dir, f)) : []
}

function fail(msg) {
  console.error(`[third-party-licenses] FAIL — ${msg}`)
  process.exit(1)
}

if (!existsSync(join(DIST, 'index.html'))) fail('dist/index.html is missing — run after `vite build`')

const lock = JSON.parse(readFileSync(join(WEB, 'package-lock.json'), 'utf8'))
const seen = new Map() // sha256 of a text -> where it was first reproduced
const out = []

function emit(label, text) {
  const h = createHash('sha256').update(text.trim()).digest('hex')
  if (seen.has(h)) return `--- ${label}: identical to the text reproduced above for ${seen.get(h)}\n`
  seen.set(h, label)
  return `--- ${label}\n\n${text.trimEnd()}\n`
}

const version = process.env.VITE_TALUS_VERSION || 'development build'
out.push(
  RULE,
  `OPEN-SOURCE LICENSES — the Talus GIS browser application (${version})`,
  RULE,
  '',
  'The Talus GIS browser application includes the open-source software below, each under its own',
  'license, reproduced here as each package publishes it. Talus GIS itself is licensed under the GNU',
  'Affero General Public License v3; its source code is at https://github.com/TalusGIS/talus.',
  'The notices for the server components are inside the api image at /usr/share/doc/talus/NOTICES.',
  '',
  RULE,
  'JAVASCRIPT PACKAGES',
  RULE,
  '',
)

let count = 0
for (const [path, entry] of Object.entries(lock.packages).sort(([a], [b]) => a.localeCompare(b))) {
  if (!path || entry.dev || entry.devOptional) continue
  const dir = join(WEB, path)
  const name = path.split('node_modules/').pop()
  if (!existsSync(dir)) {
    if (entry.optional) continue // a platform-specific optional package: not installed, not bundled
    fail(`${name} is a production dependency but is not installed — run npm ci first`)
  }
  count++
  const election = ELECTIONS[name]
  out.push(`* ${name} ${entry.version} — ${entry.license ?? '<no license field>'}${election ? `; distributed under ${election}` : ''}`)
  let files = readdirSync(dir).filter((f) => LICENCE_FILE.test(f) && statSync(join(dir, f)).isFile()).sort()
  let base = dir
  if (files.length === 0) {
    base = join(FALLBACK, name.replace('/', '__'))
    files = existsSync(base) ? readdirSync(base).sort() : []
    if (files.length === 0) fail(`${name} publishes no licence file and third-party/licenses/ has none for it`)
  }
  for (const f of files) out.push(emit(`${name} ${f}`, readFileSync(join(base, f), 'utf8')))
}

out.push('', RULE, 'ASSETS', RULE, '')
for (const a of ASSETS) {
  if (a.files.length === 0 || !a.files.every(existsSync)) fail(`no licence text for ${a.what}`)
  out.push(`* ${a.what} — ${a.license}`)
  for (const f of a.files) out.push(emit(a.what, readFileSync(f, 'utf8')))
}
out.push(
  '* Point symbols (Maki and Temaki icon sets) — CC0-1.0, dedicated to the public domain; no notice',
  '  is required. With thanks to the Maki (Mapbox) and Temaki (iD editor) projects.',
  '',
)

// A UTF-8 byte-order mark: browsers then decode the file correctly even from a server that sends
// text/plain without a charset (licence texts carry names and © that would otherwise garble).
writeFileSync(join(DIST, 'third-party-licenses.txt'), '\ufeff' + out.join('\n'))
console.log(`[third-party-licenses] wrote dist/third-party-licenses.txt — ${count} packages, ${seen.size} distinct texts`)
