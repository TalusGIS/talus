import { useMemo, useRef, useState } from 'react'
import { Star } from 'lucide-react'
import { Link } from 'react-router'

import { GeometryIcon, ReferenceIcon, TableIcon } from '@/features/catalog/ItemTypeIcons'
import { ADD_FILE_ACCEPT, MAX_FILE_MB } from '@/features/map/addFromFile'
import { EXT_KIND_LABEL, EXT_PALETTE, newExternalId, probeExternalUrl } from '@/features/map/addFromUrl'
import type { ExternalLayerSpec, ExternalProbe } from '@/features/map/addFromUrl'
import type { ExternalRef } from '@/lib/api'
import type { GeometryType } from '@/lib/api/types'
import CloseButton from '@/components/CloseButton'

/**
 * Talus 18b — the New Map "+ Add layers" panel. AGO-style: a source list (only **Browse
 * catalog** is live; URL/file/sketch are greyed "Coming soon"), catalog-parity **scope tabs**
 * (All / Owned by me / Shared with me / Favorites — mirroring the Catalog page), a **search**, then
 * the picker. Click a layer to add it; added layers show ✓ and disable. A **Done** button closes the
 * panel (there's no "add all" — you never want every layer on one map).
 *
 * Add-only: removal lives in the TOC (LayerPane), so this panel never needs to deregister a layer.
 * Route layer is deliberately absent (routing is a North-Star drift alarm); media is omitted (lean-no).
 *
 * A MODAL since handoff-map-2a §2 (commit 4): it is a catalog browse — the one panel that wants
 * width — so it rides the .modal shell instead of the map__panel float.
 */

export interface AddableLayer {
  id: string
  label: string
  color: string
  favorite: boolean
  added: boolean
  // Catalog-parity scope tabs. owner = caller owns it; sharedWithMe = genuine share, not owned
  // (admin-override-only layers are false → they appear under "All" only).
  owner: boolean
  sharedWithMe: boolean
  // Geometry family, so the picker shows the same by-geometry glyph (tinted to `color`) as the TOC.
  // Absent on raster items (they render the striped raster swatch instead).
  geomType?: GeometryType
}

// Scope tabs mirroring the Catalog page filter (All / Owned by me / Shared with me / Favorites).
type AddTab = 'all' | 'owned' | 'shared' | 'favorites'
const ADD_TABS: { key: AddTab; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'owned', label: 'Owned by me' },
  { key: 'shared', label: 'Shared with me' },
  { key: 'favorites', label: 'Favorites' },
]

function inTab(l: AddableLayer, tab: AddTab): boolean {
  if (tab === 'owned') return l.owner
  if (tab === 'shared') return l.sharedWithMe
  if (tab === 'favorites') return l.favorite
  return true // all
}

// All three add-source slices (file → URL → sketch) are live; the greyed "Coming soon" list is
// empty but kept as the seam for any future deferred-but-planned source. NOT route/media (out of
// scope — see file docstring).
const COMING_SOON: string[] = []

/** A shared reference item in the picker's "References" group (decision-external-catalog-items.md):
 * the FULL item spec (the add seeds the op's snapshot straight from it) + picker state. */
export interface AddableReference {
  ref: ExternalRef
  added: boolean
  favorite: boolean
}

export interface AddLayersPanelProps {
  layers: AddableLayer[]
  /** The caller's rasters as addable items (V045+) — shown in a "Rasters" group below the layers. */
  rasters?: AddableLayer[]
  /** Standalone non-spatial tables (decision-map-tables.md) — a "Tables" group; adding one lists it in
   *  the map's Tables section (nothing drawn). */
  tables?: AddableLayer[]
  onAdd: (id: string) => void
  onAddRaster?: (id: string) => void
  onAddTable?: (id: string) => void
  onClose: () => void
  /** Add from file (decision-add-from-file.md) — absent props keep the entry greyed "Coming soon"
   *  (callers that haven't wired the flow). canAddFromFile=false greys it with the publisher hint. */
  onPickFile?: (file: File) => void
  canAddFromFile?: boolean
  fileBusy?: boolean
  fileError?: string | null
  /** The refusal's way forward is Catalog → Import data (an over-size file): offer it as a button. */
  fileErrorOffersImport?: boolean
  /** Add from URL (decision-add-from-url.md) — open to every signed-in user (nothing is created;
   *  the map stores a reference, and SAVING stays publisher-gated). Absent = greyed "Coming soon". */
  onAddExternal?: (spec: ExternalLayerSpec, extent: [number, number, number, number] | null) => void
  /** Shared reference items (decision-external-catalog-items.md) — a "References" group; adding one
   *  seeds an external op from the item (id + snapshot). */
  references?: AddableReference[]
  onAddReference?: (ref: ExternalRef) => void
  /** Save-to-catalog at add time (fork §3.6): publishers get an "also save to catalog" option on the
   *  URL confirm card — one probe, two outcomes. Returns the new item id. Absent/false = today's flow. */
  canSaveToCatalog?: boolean
  onSaveReference?: (payload: {
    title: string
    externalKind: ExternalLayerSpec['externalKind']
    url: string
    geometryType?: 'point' | 'line' | 'polygon'
    color?: string
    where?: string
  }) => Promise<string>
  /** Sketch layer (decision-sketch-layer.md) — opens the map-notes toolbar (and closes this
   *  panel). Absent = greyed "Coming soon" (callers that haven't wired the flow). */
  onSketch?: () => void
}

export default function AddLayersPanel({
  layers,
  rasters = [],
  tables = [],
  onAdd,
  onAddRaster,
  onAddTable,
  onClose,
  onPickFile,
  canAddFromFile = false,
  fileBusy = false,
  fileError = null,
  fileErrorOffersImport = false,
  onAddExternal,
  references = [],
  onAddReference,
  canSaveToCatalog = false,
  onSaveReference,
  onSketch,
}: AddLayersPanelProps) {
  const [query, setQuery] = useState('')
  const [tab, setTab] = useState<AddTab>('all')
  const fileInput = useRef<HTMLInputElement>(null)
  // Add-from-URL flow: paste → Check (probe the service, honest CORS/404 errors) → confirm card → add.
  const [urlOpen, setUrlOpen] = useState(false)
  const [urlText, setUrlText] = useState('')
  const [urlBusy, setUrlBusy] = useState(false)
  const [urlError, setUrlError] = useState<string | null>(null)
  const [probe, setProbe] = useState<ExternalProbe | null>(null)
  const [extTitle, setExtTitle] = useState('')
  const [extColor, setExtColor] = useState(EXT_PALETTE[0])
  // Fork §3.6 — "also save to catalog" (publishers only). Off by default: saving is a deliberate
  // act of curation, not a side effect of composing a map.
  const [saveToCatalog, setSaveToCatalog] = useState(false)
  const [saveBusy, setSaveBusy] = useState(false)

  const runProbe = (): void => {
    setUrlBusy(true)
    setUrlError(null)
    setProbe(null)
    void probeExternalUrl(urlText)
      .then((p) => {
        setProbe(p)
        setExtTitle(p.title.slice(0, 80))
      })
      .catch((e) => setUrlError(e instanceof Error ? e.message : 'Could not read that URL'))
      .finally(() => setUrlBusy(false))
  }

  const addExternal = async (): Promise<void> => {
    if (!probe || !onAddExternal) return
    const isVector = probe.externalKind === 'arcgis-feature' || probe.externalKind === 'geojson'
    const spec: ExternalLayerSpec = {
      externalId: newExternalId(),
      externalKind: probe.externalKind,
      url: probe.url,
      title: extTitle.trim() || probe.title.slice(0, 80) || 'External layer',
      ...(isVector ? { geometryType: probe.geometryType, color: extColor } : {}),
      ...(probe.where ? { where: probe.where } : {}),
    }
    // Fork §3.6 — one probe, two outcomes: create the catalog item first, then land the op linked
    // to it (id + snapshot). A failed save keeps the card open with an honest error — nothing is
    // half-done (no orphan op that claims to be saved).
    if (saveToCatalog && onSaveReference) {
      setSaveBusy(true)
      setUrlError(null)
      try {
        spec.externalItemId = await onSaveReference({
          title: spec.title,
          externalKind: spec.externalKind,
          url: spec.url,
          geometryType: spec.geometryType,
          color: spec.color,
          where: spec.where,
        })
      } catch (e) {
        setUrlError(e instanceof Error ? e.message : 'Could not save the reference to the catalog')
        setSaveBusy(false)
        return
      }
      setSaveBusy(false)
    }
    onAddExternal(spec, probe.extent4326)
    setProbe(null)
    setUrlText('')
    setUrlOpen(false)
    setSaveToCatalog(false)
  }
  const q = query.trim().toLowerCase()
  const filtered = useMemo(
    () => layers.filter((l) => inTab(l, tab) && (!q || l.label.toLowerCase().includes(q))),
    [layers, tab, q],
  )
  const rasterMatches = q ? rasters.filter((r) => r.label.toLowerCase().includes(q)) : rasters
  const tableMatches = q ? tables.filter((t) => t.label.toLowerCase().includes(q)) : tables
  const referenceMatches = q
    ? references.filter((r) => r.ref.title.toLowerCase().includes(q))
    : references

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
    <div
      className="modal modal--wide add-layers"
      role="dialog"
      aria-modal="true"
      aria-label="Add layers"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="add-layers__header">
        <span className="add-layers__title">Add layers</span>
        <CloseButton onClick={onClose} className="add-layers__close" />
      </div>

      <div className="add-layers__sources" role="group" aria-label="Add source">
        <button type="button" className="add-layers__source is-active" aria-pressed={true}>
          Browse catalog
        </button>
        {onPickFile ? (
          <>
            <input
              ref={fileInput}
              type="file"
              accept={ADD_FILE_ACCEPT}
              style={{ display: 'none' }}
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) onPickFile(f)
                e.target.value = '' // re-picking the same file must fire onChange again
              }}
            />
            <button
              type="button"
              className="add-layers__source"
              disabled={!canAddFromFile || fileBusy}
              title={
                !canAddFromFile
                  ? 'Creating layers needs the publisher role'
                  : `Shapefile (.zip), GeoPackage, GeoJSON, or CSV — up to ${MAX_FILE_MB} MB`
              }
              onClick={() => fileInput.current?.click()}
            >
              {fileBusy ? 'Reading file…' : 'Add from file'}
            </button>
          </>
        ) : (
          <button type="button" className="add-layers__source is-soon" disabled title="Coming soon">
            Add from file
            <span className="add-layers__soon">Soon</span>
          </button>
        )}
        {onAddExternal ? (
          <button
            type="button"
            className={`add-layers__source${urlOpen ? ' is-active' : ''}`}
            aria-pressed={urlOpen}
            title="ArcGIS feature/image service, GeoJSON URL, or an XYZ tile template"
            onClick={() => setUrlOpen((o) => !o)}
          >
            Add from URL
          </button>
        ) : (
          <button type="button" className="add-layers__source is-soon" disabled title="Coming soon">
            Add from URL
            <span className="add-layers__soon">Soon</span>
          </button>
        )}
        {onSketch ? (
          <button
            type="button"
            className="add-layers__source"
            title="Draw ad-hoc notes — points, lines, polygons with labels — that save with this map"
            onClick={onSketch}
          >
            Sketch layer
          </button>
        ) : (
          <button type="button" className="add-layers__source is-soon" disabled title="Coming soon">
            Sketch layer
            <span className="add-layers__soon">Soon</span>
          </button>
        )}
        {COMING_SOON.map((s) => (
          <button key={s} type="button" className="add-layers__source is-soon" disabled title="Coming soon">
            {s}
            <span className="add-layers__soon">Soon</span>
          </button>
        ))}
      </div>
      {fileError !== null && <p className="sign-form__error">{fileError}</p>}
      {fileError !== null && fileErrorOffersImport && (
        <p className="add-layers__file-next">
          {/* A plain in-app link: leaving a map with unsaved changes still meets the leave prompt. */}
          <Link className="btn btn--sm" to="/catalog?import=1">
            Open Import data
          </Link>
        </p>
      )}

      {urlOpen && onAddExternal && (
        <div className="add-layers__url">
          <div className="add-layers__url-row">
            <input
              type="url"
              placeholder="Paste a service or file URL…"
              value={urlText}
              onChange={(e) => setUrlText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && urlText.trim() && !urlBusy) runProbe()
              }}
              aria-label="Service URL"
            />
            <button type="button" className="btn btn--xs" disabled={urlBusy || !urlText.trim()} onClick={runProbe}>
              {urlBusy ? 'Checking…' : 'Check'}
            </button>
          </div>
          <p className="modal__muted add-layers__url-hint">
            ArcGIS feature or image service, GeoJSON URL, or an XYZ tile template. The layer stays on
            the remote server — this map just references it.
          </p>
          {urlError !== null && <p className="sign-form__error">{urlError}</p>}
          {probe !== null && (
            <div className="add-layers__url-card">
              <div className="modal__muted">{EXT_KIND_LABEL[probe.externalKind]}</div>
              <input
                type="text"
                value={extTitle}
                maxLength={80}
                onChange={(e) => setExtTitle(e.target.value)}
                aria-label="Layer title"
              />
              {(probe.externalKind === 'arcgis-feature' || probe.externalKind === 'geojson') && (
                <div className="modal__swatches">
                  {EXT_PALETTE.map((c) => (
                    <button
                      key={c}
                      type="button"
                      className={`modal__swatch${extColor === c ? ' is-selected' : ''}`}
                      style={{ background: c }}
                      aria-label={`Color ${c}`}
                      aria-pressed={extColor === c}
                      onClick={() => setExtColor(c)}
                    />
                  ))}
                </div>
              )}
              <div className="modal__muted">
                {probe.featureCount !== null && <>{probe.featureCount.toLocaleString()} features</>}
                {probe.where && <> · filtered: <code>{probe.where}</code></>}
              </div>
              {canSaveToCatalog && onSaveReference && (
                <label className="add-layers__savecat">
                  <input
                    type="checkbox"
                    checked={saveToCatalog}
                    onChange={(e) => setSaveToCatalog(e.target.checked)}
                  />
                  <span>
                    Also save to the catalog as a shared reference — others can find and add it
                    from Browse.
                  </span>
                </label>
              )}
              <button type="button" className="btn btn--sm" disabled={saveBusy} onClick={() => void addExternal()}>
                {saveBusy ? 'Saving…' : '+ Add to map'}
              </button>
            </div>
          )}
        </div>
      )}

      {layers.length === 0 ? (
        <p className="add-layers__empty">No layers available to add.</p>
      ) : (
        <>
          <input
            className="add-layers__search"
            type="search"
            placeholder="Search layers…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search layers"
          />

          {/* Scope tabs — catalog parity (All / Owned by me / Shared with me / Favorites). */}
          <div className="catalog__segmented add-layers__tabs" role="group" aria-label="Layer scope">
            {ADD_TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                className={`catalog__seg${tab === t.key ? ' catalog__seg--active' : ''}`}
                aria-pressed={tab === t.key}
                onClick={() => setTab(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>

          {filtered.length === 0 ? (
            <p className="add-layers__empty">
              {q ? `No layers match “${query.trim()}”.` : 'No layers in this filter.'}
            </p>
          ) : (
            <ul className="add-layers__list">
              {filtered.map((l) => (
                <li key={l.id}>
                  <button
                    type="button"
                    className={`add-layers__item${l.added ? ' is-added' : ''}`}
                    onClick={() => !l.added && onAdd(l.id)}
                    disabled={l.added}
                    aria-pressed={l.added}
                  >
                    <span className="add-layers__swatch add-layers__swatch--geom" style={{ color: l.color }}>
                      <GeometryIcon geomType={l.geomType} className="add-layers__geom" />
                    </span>
                    <span className="add-layers__name">{l.label}</span>
                    {l.favorite && (
                      <span className="add-layers__fav" title="Favorite">
                        <Star size={13} fill="currentColor" aria-hidden="true" />
                      </span>
                    )}
                    <span className="add-layers__state">{l.added ? '✓ Added' : '+ Add'}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {rasterMatches.length > 0 && onAddRaster && (
        <div className="add-layers__group">
          <div className="add-layers__grouphdr">Rasters</div>
          <ul className="add-layers__list">
            {rasterMatches.map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  className={`add-layers__item${r.added ? ' is-added' : ''}`}
                  onClick={() => !r.added && onAddRaster(r.id)}
                  disabled={r.added}
                  aria-pressed={r.added}
                >
                  <span className="add-layers__swatch add-layers__swatch--raster" />
                  <span className="add-layers__name">{r.label}</span>
                  {r.favorite && (
                    <span className="add-layers__fav" title="Favorite">
                      <Star size={13} fill="currentColor" aria-hidden="true" />
                    </span>
                  )}
                  <span className="add-layers__state">{r.added ? '✓ Added' : '+ Add'}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {referenceMatches.length > 0 && onAddReference && (
        <div className="add-layers__group">
          <div className="add-layers__grouphdr">References</div>
          <ul className="add-layers__list">
            {referenceMatches.map((r) => (
              <li key={r.ref.id}>
                <button
                  type="button"
                  className={`add-layers__item${r.added ? ' is-added' : ''}`}
                  onClick={() => !r.added && onAddReference(r.ref)}
                  disabled={r.added}
                  aria-pressed={r.added}
                  title={`${EXT_KIND_LABEL[r.ref.externalKind]} — referenced external service`}
                >
                  <span
                    className="add-layers__swatch add-layers__swatch--geom"
                    style={{ color: r.ref.color ?? 'var(--color-muted)' }}
                  >
                    <ReferenceIcon
                      externalKind={r.ref.externalKind}
                      geomType={r.ref.geometryType ?? undefined}
                      className="add-layers__geom"
                    />
                  </span>
                  <span className="add-layers__name">{r.ref.title}</span>
                  {r.favorite && (
                    <span className="add-layers__fav" title="Favorite">
                      <Star size={13} fill="currentColor" aria-hidden="true" />
                    </span>
                  )}
                  <span className="add-layers__state">{r.added ? '✓ Added' : '+ Add'}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {tableMatches.length > 0 && onAddTable && (
        <div className="add-layers__group">
          <div className="add-layers__grouphdr">Tables</div>
          <ul className="add-layers__list">
            {tableMatches.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  className={`add-layers__item${t.added ? ' is-added' : ''}`}
                  onClick={() => !t.added && onAddTable(t.id)}
                  disabled={t.added}
                  aria-pressed={t.added}
                >
                  <span className="add-layers__swatch add-layers__swatch--geom" style={{ color: t.color }}>
                    <TableIcon className="add-layers__geom" />
                  </span>
                  <span className="add-layers__name">{t.label}</span>
                  {t.favorite && (
                    <span className="add-layers__fav" title="Favorite">
                      <Star size={13} fill="currentColor" aria-hidden="true" />
                    </span>
                  )}
                  <span className="add-layers__state">{t.added ? '✓ Added' : '+ Add'}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Done closes the panel — there is no "add all" (you never want every layer on one map). */}
      <button type="button" className="btn btn--sm add-layers__done" onClick={onClose}>
        Done
      </button>
    </div>
    </div>
  )
}
