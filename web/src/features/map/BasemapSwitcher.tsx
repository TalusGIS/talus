/**
 * The basemap switcher. It lists what the organization LISTS (V115, V116 /
 * decision-streamed-basemap.md §14, §16), in up to two groups:
 *
 * - DOWNLOADED — Light / Dark / Gray, Protomaps vector flavors drawn from the organization's own
 *   archive. Listed when the organization lists its downloaded looks.
 * - STREAMED — OpenStreetMap (standard, gray, dark), USGS Topo, USGS Imagery. Each is fetched by
 *   the viewer's browser from its publisher, so it appears only when the organization's offered
 *   list names that publisher.
 *
 * Each group carries a caption saying which kind it is (a visible
 * identifier of streamed versus local).
 * One more entry can appear: the basemap the map is drawing right now, when it is a downloaded
 * look the organization no longer lists. A saved map keeps its own look (§16, G3), and a switcher
 * that did not show the look on the canvas would read as broken.
 *
 * Switching swaps the basemap in place; the user's data layers are preserved.
 *
 * Two placements (lower-right-untangle, 2026-07-23): the default `float` = the original absolute
 * bottom-right pill (still used by the embed, which has no rail); `pop` = a vertical list flowing
 * inside the left rail's basemap popout — position comes from the popout wrapper, not from here.
 * The pill is one row with no room for captions, so there the kind is in each button's tooltip.
 */

import type { StreamedBasemap } from '@/lib/api'
import {
  BASEMAP_LABELS,
  DOWNLOADED_BASEMAPS,
  KIND_LABELS,
  isStreamedBasemap,
  offeredLooks,
  type Basemap,
} from '@/features/map/basemapNames'

// The names, the offered-list rules and the labels live in basemapNames.ts (no component there, so
// they can be shared). Re-exported because this was their home through V093 and callers — Talus
// Field among them — import the type and the imagery flag from here.
export { IMAGERY_ENABLED, type Basemap } from '@/features/map/basemapNames'

// The embed's floating pill is one row on a page we do not control the width of.
const SHORT_LABELS: Partial<Record<Basemap, string>> = {
  osm: 'OSM',
  'osm-gray': 'OSM Gray',
  'osm-dark': 'OSM Dark',
  usgstopo: 'Topo',
}

export interface BasemapSwitcherProps {
  value: Basemap
  onChange: (basemap: Basemap) => void
  /** The streamed PUBLISHERS this portal offers (already through `offeredStreamed`). */
  streamed?: readonly StreamedBasemap[]
  /** Whether this portal lists its downloaded looks (already through `downloadedListed`). */
  downloaded?: boolean
  variant?: 'float' | 'pop'
}

export default function BasemapSwitcher({
  value,
  onChange,
  streamed = [],
  downloaded = true,
  variant = 'float',
}: BasemapSwitcherProps) {
  // The downloaded group: all three when listed; otherwise only the one on the canvas, if it is one.
  const local: Basemap[] = downloaded
    ? [...DOWNLOADED_BASEMAPS]
    : isStreamedBasemap(value)
      ? []
      : [value]
  const groups: { kind: keyof typeof KIND_LABELS; options: Basemap[] }[] = [
    { kind: 'downloaded' as const, options: local },
    { kind: 'streamed' as const, options: offeredLooks(streamed) },
  ].filter((g) => g.options.length > 0)

  const option = (key: Basemap, kind: keyof typeof KIND_LABELS) => (
    <button
      key={key}
      type="button"
      className={`basemap-switcher__opt${value === key ? ' is-active' : ''}`}
      aria-pressed={value === key}
      title={`${BASEMAP_LABELS[key]} (${KIND_LABELS[kind].toLowerCase()})`}
      onClick={() => onChange(key)}
    >
      {variant === 'pop' ? BASEMAP_LABELS[key] : (SHORT_LABELS[key] ?? BASEMAP_LABELS[key])}
    </button>
  )

  if (variant === 'pop') {
    return (
      <div className="basemap-switcher basemap-switcher--pop" role="group" aria-label="Basemap">
        {groups.map((g) => (
          <div
            key={g.kind}
            className="basemap-switcher__group"
            role="group"
            aria-label={`${KIND_LABELS[g.kind]} basemaps`}
          >
            <div className="basemap-switcher__caption" aria-hidden="true">
              {KIND_LABELS[g.kind]}
            </div>
            {g.options.map((key) => option(key, g.kind))}
          </div>
        ))}
      </div>
    )
  }
  return (
    <div className="basemap-switcher" role="group" aria-label="Basemap">
      {groups.flatMap((g) => g.options.map((key) => option(key, g.kind)))}
    </div>
  )
}
