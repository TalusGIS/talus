import { useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { Link } from 'react-router'

import HeroMotif from '@/features/shell/HeroMotif'
import {
  ApiError,
  downloadMetadata,
  type FavoriteItemType,
  type MetadataFormat,
} from '@/lib/api'

/**
 * The pieces OpenDataDetail and OpenDataRasterDetail both need (handoff-auth-opendata-4 §1.2).
 *
 * The two pages were one page written twice: `Fact`, `_META_LABELS`, `MetadataDownload`,
 * `ApiSection` and the 429 error mapping were near-identical copies, and they had already drifted
 * — the raster page grew a format-label map while the vector page still shouted `GEOJSON`.
 * Anything added here lands on both, which is the point: the raster page needs §3's treatment
 * every bit as much as the vector one.
 */

/**
 * The ONE format-name map, covering both families.
 *
 * §3.4 said to promote the raster page's existing map — but that map is `{geotiff, cog}`, which has
 * ZERO overlap with the vector formats, so promoting it verbatim would have left `GEOJSON` shouting
 * exactly as before (§6.4). What the raster page actually had was the PATTERN, not the contents.
 *
 * These are proper names: GeoJSON, Shapefile, GeoPackage. The design system's all-caps exception
 * covers metadata codes like `EPSG:4326`, not words — so CSV stays upper (an acronym) and the rest
 * do not.
 */
export const FORMAT_LABELS: Record<string, string> = {
  csv: 'CSV',
  geojson: 'GeoJSON',
  shapefile: 'Shapefile',
  gpkg: 'GeoPackage',
  geotiff: 'GeoTIFF',
  cog: 'Cloud-Optimized GeoTIFF',
}

/** `toUpperCase()` remains the fallback: the server gates formats, and a format we have no name for
 *  should still render rather than vanish. */
export function formatLabel(f: string): string {
  return FORMAT_LABELS[f] ?? f.toUpperCase()
}

/** The 429 copy is specific and correct and is kept verbatim — the export endpoint is rate-limited
 *  BECAUSE these are expensive, and "try again later" without the reason invites a retry storm. */
export function downloadErrorMessage(e: unknown): string {
  return e instanceof ApiError && e.status === 429
    ? 'Too many downloads — please wait a minute and try again.'
    : 'Download failed. Please try again.'
}

/** Null renders as empty, objects as JSON — the preview table's cell coercion. */
export function cell(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

const _META_LABELS: { format: MetadataFormat; label: string }[] = [
  { format: 'qmd', label: 'QGIS (.qmd)' },
  { format: 'iso19139', label: 'ISO 19139 (.xml)' },
]

/**
 * ISO-19115 metadata sidecars. `kind` is the only thing that differed between the two copies —
 * `'layer'` vs `'raster'`. Both labels stay exactly as they were: `QGIS (.qmd)` and
 * `ISO 19139 (.xml)` are precisely right for this audience.
 */
export function MetadataDownload({
  kind,
  id,
  token,
}: {
  kind: FavoriteItemType
  id: string
  token?: string
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function run(format: MetadataFormat) {
    setErr(null)
    setBusy(format)
    try {
      await downloadMetadata(kind, id, format, token)
    } catch {
      setErr('Metadata download failed. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="opendata__endpoint">
      <p className="opendata__muted opendata__muted--sm">
        Download this dataset’s ISO-19115 metadata as a QGIS sidecar or ISO 19139 XML.
      </p>
      <div className="opendata__downloads">
        {_META_LABELS.map(({ format, label }) => (
          <button
            key={format}
            className="btn btn--secondary btn--sm"
            disabled={busy !== null}
            onClick={() => void run(format)}
          >
            {busy === format ? 'Preparing…' : label}
          </button>
        ))}
      </div>
      {err && <p className="opendata__error">{err}</p>}
    </section>
  )
}

/**
 * A copyable endpoint URL. `standard` marks the endpoints a desktop GIS actually consumes
 * (OGC API — Features, WMTS, WCS) — they get a card and a badge because they are what this
 * audience came for, while the REST URL folds away with the metadata (§3.3).
 */
export function ApiSection({
  url,
  title = 'API',
  note,
  standard = false,
  heading = true,
}: {
  url: string
  title?: string
  note?: string
  standard?: boolean
  heading?: boolean
}) {
  const [copied, setCopied] = useState(false)
  return (
    <section className={standard ? 'opendata__endpoint opendata__endpoint--standard' : 'opendata__endpoint'}>
      {heading && (
        <h3 className="opendata__endpoint-title">
          {title}
          {standard && <span className="opendata__badge">STANDARD</span>}
        </h3>
      )}
      {note && <p className="opendata__muted opendata__muted--sm">{note}</p>}
      <div className="opendata__api">
        <code className="opendata__api-url">{url}</code>
        <button
          className="btn btn--secondary btn--sm"
          onClick={() => {
            void navigator.clipboard?.writeText(url)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    </section>
  )
}


/**
 * The detail pages' hero (§3.1). Same `.hero--compact` vocabulary as the Open Data LIST — dark
 * --blue-950 band, TIN motif, scrim, inverse text — because the flow used to change design language
 * mid-click: the list got the branded hero and the detail page kept a centred reading column.
 *
 * The back affordance IS the eyebrow, occupying the slot the list's `hero__eyebrow` occupies, so
 * the two screens read as one place. That also retires `← Open Data`, the last hand-drawn arrow on
 * these pages (§1.3).
 */
export function DetailHero({ title, lede }: { title: string; lede?: string | null }) {
  return (
    <div className="hero hero--compact">
      <HeroMotif />
      <div className="hero__scrim" />
      <div className="hero__inner">
        <Link to="/open-data" className="opendata__hero-back">
          <ArrowLeft size={13} aria-hidden="true" />
          Open data
        </Link>
        <h1 className="hero__title opendata__hero-title">{title}</h1>
        {lede && <p className="hero__lede opendata__hero-lede">{lede}</p>}
      </div>
    </div>
  )
}

/**
 * Not found, INSIDE the page frame (§3.6).
 *
 * The 404 semantics are unchanged and must stay: the server returns 404 for an uncurated dataset
 * and for a nonexistent one alike (existence-hiding), and the copy stays as it was. What changes is
 * that an anon visitor arriving from a stale link or a search result now has somewhere to go
 * instead of a bare sentence and a back link.
 */
export function NotFoundPage() {
  return (
    <div className="opendata opendata--detail">
      <DetailHero title="Open data" />
      <div className="opendata__body">
        <p className="opendata__muted">That dataset isn’t available.</p>
        <Link className="btn" to="/open-data">
          Browse all datasets
        </Link>
      </div>
    </div>
  )
}

/** PostGIS spelling -> what a person calls it. Multi* collapses; anything unrecognised passes
 *  through unchanged rather than being forced into one of three buckets. */
export function geometryLabel(t: string): string {
  const s = t.toLowerCase()
  if (s.includes('point')) return 'Point'
  if (s.includes('linestring')) return 'Line'
  if (s.includes('polygon')) return 'Polygon'
  return t
}

/** A shape glyph for the geometry cell. Deliberately NOT a coloured `--geo-*` dot as §3.2
 *  suggested: those tokens are thematic (water/terrain/forest/route/boundary/heat), not geometry
 *  primitives, so a colour there would carry no meaning. The SHAPE carries it. */
function GeometryGlyph({ kind }: { kind: string }) {
  return (
    <svg className="opendata__geoglyph" viewBox="0 0 10 10" width="9" height="9" aria-hidden="true">
      {kind === 'Point' && <circle cx="5" cy="5" r="3.2" fill="currentColor" />}
      {kind === 'Line' && (
        <path d="M1 8 L4 3 L6.5 6 L9 2" stroke="currentColor" strokeWidth="1.6" fill="none" />
      )}
      {kind === 'Polygon' && <path d="M1.5 3 L5 1 L8.5 3.5 L7 8.5 L2.5 8 Z" fill="currentColor" />}
    </svg>
  )
}

export type FactCell = { label: string; value: string; mono?: boolean; geometry?: boolean }

/**
 * The download-decision strip (§3.2): the facts someone needs BEFORE committing to a 6 MB export —
 * what geometry, what projection, how many features. Previously none of these were scannable, so
 * checking a projection meant downloading the file first.
 *
 * Cells with no value are not rendered. That is the point of `srid`/`geometryType` being nullable
 * server-side: an unconstrained geometry column reports no CRS rather than a confident wrong one.
 * Browse metadata (owner, topic, keywords) is NOT here — it belongs below, as chips.
 */
export function FactStrip({ cells }: { cells: FactCell[] }) {
  const shown = cells.filter((c) => c.value)
  if (shown.length === 0) return null
  return (
    <dl className="opendata__strip">
      {shown.map((c) => (
        <div key={c.label} className="opendata__strip-cell">
          <dt className="opendata__strip-label">{c.label}</dt>
          <dd className={`opendata__strip-value${c.mono ? ' opendata__strip-value--mono' : ''}`}>
            {c.geometry && <GeometryGlyph kind={c.value} />}
            {c.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}


/**
 * Presentation kind for a preview value when the column SCHEMA is unavailable.
 *
 * `/layers/{id}/columns` carries `_validated_claims`, which hard-401s a missing bearer — so despite
 * its docstring ("Optional-auth: public layers are anon-readable") and its `get_optional_user`
 * dependency, an anonymous caller cannot read it. The public preview therefore has values but no
 * types, and `String(v)` for everything was what §3.5 set out to fix.
 *
 * Inferring FACE AND ALIGNMENT from the value is safe in a way that inferring a CRS is not (§3.2's
 * rule): this makes no claim about the data, it only decides whether digits line up. A signed-in
 * viewer still gets the real `columnKind()` off the schema.
 */
export function inferKind(v: unknown): 'number' | 'date' | 'text' {
  if (typeof v === 'number') return 'number'
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}([T ]|$)/.test(v)) return 'date'
  return 'text'
}
