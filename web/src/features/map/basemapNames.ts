import type { DefaultBasemap, StreamedBasemap, StreamedLook } from '@/lib/api'

/**
 * Basemap NAMES and what a portal may draw (V115, V116 / decision-streamed-basemap.md §14, §16). No
 * component lives here on purpose: the map, the switcher, the admin page and Talus Field all need
 * these, and a module that exports a component cannot share plain values without breaking fast
 * refresh.
 *
 * A basemap is exchanged and stored as a NAME from a closed list. Three names are DOWNLOADED looks,
 * drawn from the organization's own archive. Five are STREAMED looks, fetched by each viewer's
 * browser from one of three PUBLISHERS, and drawn only when the organization's offered list names
 * that publisher. The name-to-address step lives in basemapRuntime.ts and nowhere else.
 *
 * Two words, kept apart on purpose: a PUBLISHER (`StreamedBasemap`: osm, usgstopo, imagery) is
 * what an organization offers; a LOOK (`StreamedLook`) is what a map names. OpenStreetMap has
 * three looks from one publisher and one tile address.
 */

// One definition, three consumers (V093): the V093 CHECK, the API Literal, and this union. Aliasing
// the lib type instead of restating it is what keeps the org default and the switcher from drifting.
export type Basemap = DefaultBasemap
export type DownloadedBasemap = Exclude<Basemap, StreamedLook>

/** Build-time hard override for imagery (Talus 18d). It predates the run-time setting below and no
 *  release build sets it, so in practice the organization's offered list is the control; this stays
 *  as the switch a custom build can use to remove imagery whatever the setting says.
 *
 *  `env?.`: under `node --test` there is no `import.meta.env` at all, and Talus Field's unit tests
 *  load this file to exercise the real rules rather than a copy (decision-streamed-basemap.md
 *  §19.3). In a Vite build or dev server `import.meta.env` always exists, so nothing changes there. */
export const IMAGERY_ENABLED = (import.meta.env?.VITE_IMAGERY_BASEMAP ?? 'on') !== 'off'

// The three DOWNLOADED looks: @protomaps/basemaps flavors, curated (E11, ruled 2026-08-25). Black
// was dropped 2026-07-24 as too close to Dark to earn a slot, and White followed 2026-08-25 by the
// same logic beside Light and Gray. Maps saved on White fall back to Light at load (AssetMap's
// sanitizeBasemap). These draw from the organization's downloaded area.
export const DOWNLOADED_BASEMAPS: readonly DownloadedBasemap[] = ['light', 'dark', 'grayscale']

/** The streamed PUBLISHERS, in the order they are offered (V115 / §14). The offered list's
 *  vocabulary; it stays three entries. */
export const STREAMED_BASEMAPS: readonly StreamedBasemap[] = ['osm', 'usgstopo', 'imagery']

/** Every streamed LOOK and the publisher it belongs to, in the order a switcher lists them (V116 /
 *  §16). The gray and dark OpenStreetMap looks are the standard tiles recoloured as they are drawn:
 *  no new host, no new request. A look is offered exactly when its publisher is. */
const LOOK_PUBLISHER: Record<StreamedLook, StreamedBasemap> = {
  osm: 'osm',
  'osm-gray': 'osm',
  'osm-dark': 'osm',
  usgstopo: 'usgstopo',
  imagery: 'imagery',
}
export const STREAMED_LOOKS = Object.keys(LOOK_PUBLISHER) as StreamedLook[]

// OWN keys only, and only for a real string: `in` would accept "constructor" and "toString" off
// Object.prototype, and a one-element array like ['osm'] coerces to a matching key.
const isLook = (v: unknown): v is StreamedLook =>
  typeof v === 'string' && Object.prototype.hasOwnProperty.call(LOOK_PUBLISHER, v)

/** Is `v` one of the eight names? The guard a value from the api, a saved map or a stored draft
 *  passes before it is treated as a basemap at all (an address, a retired name, a future one or a
 *  non-string is not). */
export function isKnownBasemap(v: unknown): v is Basemap {
  return typeof v === 'string' && ((DOWNLOADED_BASEMAPS as readonly string[]).includes(v) || isLook(v))
}

/** A streamed look (any of the five), as opposed to a downloaded one. */
/** Whether to show "No basemap installed yet". Only when there is NO downloaded archive AND the look
 *  being drawn is a downloaded one — that is the gray void the notice explains. A streamed look
 *  (OpenStreetMap on every new install) draws without an archive; saying "no basemap" over it, on
 *  every map and for signed-out viewers of a public embed too, was wrong on day one (once-over C2). */
export function showNoBasemapNote(archiveAbsent: boolean, dismissed: boolean, drawing: Basemap): boolean {
  return archiveAbsent && !dismissed && !isStreamedBasemap(drawing)
}

export function isStreamedBasemap(b: Basemap): b is StreamedLook {
  return isLook(b)
}

/** The publisher whose tiles a streamed look draws. */
export function publisherOf(look: StreamedLook): StreamedBasemap {
  return LOOK_PUBLISHER[look]
}

/** What this portal may draw from the internet, from the `streamed` list on GET /basemap — as
 *  PUBLISHERS. Every reader goes through here so three rules hold everywhere: only KNOWN names
 *  survive (whatever the payload says), the build-time imagery override still wins, and an api that
 *  predates V115 (no list at all) means exactly what every install offered before it — imagery. */
export function offeredStreamed(list: readonly string[] | undefined): StreamedBasemap[] {
  const raw = list ?? ['imagery']
  return STREAMED_BASEMAPS.filter((b) => raw.includes(b) && (b !== 'imagery' || IMAGERY_ENABLED))
}

/** The streamed LOOKS a portal lists, given the publishers it offers. */
export function offeredLooks(offered: readonly StreamedBasemap[]): StreamedLook[] {
  return STREAMED_LOOKS.filter((look) => offered.includes(LOOK_PUBLISHER[look]))
}

/** Whether this portal LISTS its downloaded looks, from `downloadedListed` on GET /basemap (V116).
 *  Only a real `false` turns them off: an api that predates V116 sends nothing, and every install
 *  listed them then. Listing a downloaded look can never send a browser anywhere, so "listed" is
 *  also the safe reading of an answer that has not arrived. */
export function downloadedListed(flag: unknown): boolean {
  return flag !== false
}

/** A basemap this portal may DRAW: a streamed look whose publisher is not offered is replaced by
 *  `fallback` — what new maps open on — and by Light when that too is a streamed look the portal
 *  does not offer (or when no answer has arrived: Light fetches nothing). This is what stops a map
 *  saved on OpenStreetMap from sending viewers' browsers there after the organization has withdrawn
 *  it. A downloaded look always passes: it is local, and whether it is LISTED is the switcher's
 *  question, not a question of what may be drawn (§16, G3). */
export function resolveOffered(
  b: Basemap,
  offered: readonly StreamedBasemap[],
  fallback: Basemap = 'light',
): Basemap {
  const drawable = (x: Basemap): boolean => !isStreamedBasemap(x) || offered.includes(publisherOf(x))
  if (drawable(b)) return b
  return drawable(fallback) ? fallback : 'light'
}

/** What a SAVED map shows (§16, G3). Two
 *  rules. A streamed look the portal does not offer is replaced, as above. A downloaded look is
 *  KEPT even when the portal no longer lists the downloaded looks — the setting governs the
 *  switcher and what new maps open on, not what an existing map may draw — unless there is no
 *  downloaded area to draw it from, where it would be a blank canvas: then the map opens on what
 *  new maps open on. Shown, never saved: the map's own definition is untouched. */
export function resolveSaved(
  saved: Basemap,
  offered: readonly StreamedBasemap[],
  fallback: Basemap,
  listsDownloaded: boolean,
  hasArea: boolean,
): Basemap {
  const shown = resolveOffered(saved, offered, fallback)
  if (!isStreamedBasemap(shown) && !listsDownloaded && !hasArea) return resolveOffered(fallback, offered)
  return shown
}

/** What a small helper map (the extent picker) draws on when NO area is downloaded: the first
 *  streamed map the organization offers, or null when it offers none — in which case there is
 *  nothing to draw on and the caller must not mount a map at all. Takes the offered list (already
 *  through `offeredStreamed`), never a build flag, so the picker cannot reach a publisher the
 *  organization has withdrawn (decision-streamed-basemap.md §15.10). The order is the list's own:
 *  a street map with names is the better surface for framing a jurisdiction, and an install that
 *  offers only imagery (every upgrade from before V115) keeps the imagery it always drew. */
export function pickerStreamed(offered: readonly StreamedBasemap[]): StreamedBasemap | null {
  return offered[0] ?? null
}

/** What "new maps open on" becomes when its map stops being listed — the api's `plan_listing`,
 *  mirrored so the admin page can say what a save will do BEFORE it is sent. The api's answer is
 *  what is adopted; this only words the warning. Returns null when the settings would list
 *  nothing, which the api refuses. */
export function plannedDefault(
  current: Basemap,
  listsDownloaded: boolean,
  offered: readonly StreamedBasemap[],
): Basemap | null {
  if (!listsDownloaded && offered.length === 0) return null
  const stillListed = isStreamedBasemap(current)
    ? offered.includes(publisherOf(current))
    : listsDownloaded
  if (stillListed) return current
  return listsDownloaded ? 'light' : (STREAMED_BASEMAPS.find((p) => offered.includes(p)) ?? null)
}

export const BASEMAP_LABELS: Record<Basemap, string> = {
  light: 'Light',
  dark: 'Dark',
  grayscale: 'Gray',
  osm: 'OpenStreetMap',
  'osm-gray': 'OpenStreetMap Gray',
  'osm-dark': 'OpenStreetMap Dark',
  usgstopo: 'USGS Topo',
  imagery: 'Imagery',
}

/** The two kinds, as the words shown wherever both can appear (a
 *  visible identifier of streamed versus local). */
export const KIND_LABELS = { streamed: 'Streamed', downloaded: 'Downloaded' } as const
