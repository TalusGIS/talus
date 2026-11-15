import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, Check, ChevronLeft, ChevronRight, Minus, Star } from 'lucide-react'
import { useAuth } from 'react-oidc-context'

import {
  ApiError,
  downloadLayerExport,
  fetchLayerColumns,
  fetchLayerDisplayField,
  getCustomDomain,
  queryLayerFeatures,
  searchSpatialRefs,
  setLayerDisplayField,
  editOpsOf,
} from '@/lib/api'
import type { DomainValue, FilterCondition, LayerColumn, LayerFeature, LayerFeatureCollection, MyLayer, SpatialRef } from '@/lib/api'

import RecordDetail from '@/features/record-detail/RecordDetail'
import CalculateDialog from '@/features/catalog/CalculateDialog'
import ImportFeaturesDialog from '@/features/catalog/ImportFeaturesDialog'
import RowFormDialog from '@/features/catalog/RowFormDialog'
import Badge from '@/components/Badge'

import { cell, cellTitle, colLabel, columnKind, displayName, filterKind, HIDDEN_COLUMNS, isBlank } from './format'

const DEFAULT_PAGE = 50
const MAX_PAGE = 5000 // matches the api's per-request cap (GET /layers/{id}/features le=5000)

// Default-hidden noise columns (toggleable back on). created_by/updated_by stay visible but
// render the resolved name, not the uuid. The raw `props` jsonb blob is hidden by default now
// that owner-defined fields render as their own columns (decision-geometry-calc.md §3.4).
const DEFAULT_HIDDEN = HIDDEN_COLUMNS

// Columns that can never NAME a feature — identity / geometry / audit / plumbing (mirrors the server's
// app/display_field.py LABEL_SKIP_COLS). The display-field star is offered only on other columns; the server
// still validates independently. Owner-defined virtual fields (props: true) are always offerable.
const STAR_SKIP = new Set([
  'id', 'objectid', 'geom', 'row_version', 'created_at', 'created_by', 'updated_at', 'updated_by',
  'ai_provenance', 'props', 'cover_photo_id',
])

type FieldFilter =
  | { kind: 'in'; codes: string[] } // coded + boolean (checkbox option list)
  | { kind: 'text'; val: string } // text contains -> ilike
  | { kind: 'range'; from: string; to: string } // date or number

// Column-list ordering in the Columns panel (also applied to the table + filter columns):
// 'db' = the table's native column sequence; 'name' = by field name; 'alias' = by Display Name.
type ColSort = 'db' | 'name' | 'alias'

/**
 * The catalog Data > Table view (Talus 15). Context-agnostic: data comes from the generic
 * GET /layers/{id}/features (server-side filter/sort/paginate); `scopeBbox` lets a future map
 * drive the spatial extent (when set, the in-panel bounds control is hidden). Personal,
 * non-shared view state — sort/filters/column-visibility/selection never touch the data or
 * anyone else.
 */
/** Friendly op names for the pinned-condition chips (mirrors the FilterPanel's labels). */
const OP_LABELS: Record<string, string> = {
  eq: 'is',
  ne: 'is not',
  ilike: 'contains',
  gte: '≥',
  lte: '≤',
  in: 'is any of',
}

export default function TableView({
  layer,
  scopeBbox,
  initialFilters,
  onApplyFilters,
  hideImport,
  selection,
  onSelectionChange,
  onZoomToSelected,
  onDeleteSelected,
}: {
  layer: MyLayer
  scopeBbox?: string
  /** Filter-sync seeding (decision-feature-filter.md): the layer's CURRENT map-filter conditions.
   *  Representable ops become filter cards; the remainder (ne / eq-on-text/number — no card form)
   *  rides as read-only PINNED conditions applied to every fetch, so grid and map never silently
   *  diverge. Read once per layer at metadata load. */
  initialFilters?: FilterCondition[]
  /** Filter write-through: called with the FULL condition set (cards + pinned)
   *  whenever the user COMMITS filters (Apply / Clear all / the empty-state clear). In the map's
   *  docked panel this writes the layer's ONE shared map filter — it repaints the map and PERSISTS
   *  after the table closes (clear it here or in the Filter tool). The grid's bounds/bbox card is
   *  never included (the map has its own viewport). */
  onApplyFilters?: (conds: FilterCondition[]) => void
  /** Hide the Import button (the map-hosted panel; data imports live in the Catalog). */
  hideImport?: boolean
  /** Controlled row selection (decision-zoom-to-selected.md F2): when BOTH props are provided the
   *  docked map panel owns the checked-row set (it outlives this component — zoom-to-selected +
   *  the map highlight read it, and it survives closing the panel). Omitted (the Catalog), the
   *  grid keeps its own internal selection exactly as before. */
  selection?: Set<string>
  onSelectionChange?: (next: Set<string>) => void
  /** Selection actions the HOST already owns (handoff-record-table-3 §4.3). The grid re-homes them
   *  into the selection bar; it does not implement them. Absent in the Catalog, which has no map to
   *  zoom and — deliberately — no bulk delete: that would be a new destructive data path, not a
   *  presentation change (§7.12). Non-spatial tables get no Zoom to either (§4.6). */
  onZoomToSelected?: () => void
  onDeleteSelected?: () => void
}) {
  // V109 (decision-layer-edit-capabilities.md): the owner's flags hide the write affordances per op —
  // Add row / Import ⇔ add, Calculate + row edit ⇔ attributes. The doors decide; this only hides.
  const ops = editOpsOf(layer)
  const auth = useAuth()
  const token = auth.user?.access_token

  const [columns, setColumns] = useState<LayerColumn[] | null>(null)
  // Display field (decision-layer-display-field.md): the RAW starred column (null = unset → the map derives a
  // default) + whether THIS caller may move it. A viewer sees the filled star but can't change it.
  const [displayField, setDisplayField] = useState<string | null>(null)
  const [canManageDisplay, setCanManageDisplay] = useState(false)
  // Custom-domain values for coded OWNER fields (virtual props columns carry a customDomain id) —
  // fetched per distinct id at metadata load; a failed fetch degrades to raw codes, never blocks.
  const [customDomains, setCustomDomains] = useState<Record<string, DomainValue[]>>({})
  const [data, setData] = useState<LayerFeatureCollection | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const [filters, setFilters] = useState<Record<string, FieldFilter>>({})
  // Map-filter conditions with no card form (ne / eq-on-text/number), seeded from initialFilters —
  // applied to every fetch and re-emitted on commit so nothing authored in the Filter tool is lost.
  const [pinned, setPinned] = useState<FilterCondition[]>([])
  // The Filters flyout STAGES edits (FilterPanel parity): cards edit the draft;
  // Apply commits, Cancel discards, Clear all commits empty immediately.
  const [draftFilters, setDraftFilters] = useState<Record<string, FieldFilter>>({})
  const [draftPinned, setDraftPinned] = useState<FilterCondition[]>([])
  const [sort, setSort] = useState<{ field: string; dir: 'asc' | 'desc' } | null>(null)
  const [offset, setOffset] = useState(0)
  // Map-extent scoping (arc close-out 2026-07-03): every scopeBbox change — a pan with the toggle on,
  // or the toggle flipping either way — re-scopes the query, so paging restarts at page 1 (reset, don't
  // stitch: the settled answer to the pan-refetch/paging fork from decision-map-layer-table.md §3.3).
  useEffect(() => {
    setOffset(0)
  }, [scopeBbox])
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE)
  const [pageSizeText, setPageSizeText] = useState(String(DEFAULT_PAGE)) // the input's raw text
  const [hidden, setHidden] = useState<Set<string>>(new Set(DEFAULT_HIDDEN))
  // Controlled when the docked panel provides `selection` (F2); internal otherwise (the Catalog).
  // The setter keeps the existing functional-update call sites working in both modes.
  const [internalSelected, setInternalSelected] = useState<Set<string>>(new Set())
  const controlled = selection !== undefined && onSelectionChange !== undefined
  const selected = controlled ? selection : internalSelected
  const setSelected = (next: Set<string> | ((prev: Set<string>) => Set<string>)): void => {
    if (controlled) onSelectionChange(typeof next === 'function' ? next(selected) : next)
    else setInternalSelected(next)
  }
  // Show-selected-records mode (decision-select-features.md F5, Pro parity): ON → the grid pages
  // ONLY the selected ids, server-side, AND-composed with filters/bbox — numberMatched becomes the
  // selected-set size so the pager stays honest. Per-table-session state (resets on layer switch);
  // auto-reverts when the selection empties. Refining the selection while ON refetches (Pro: "the
  // table updates to reflect the current selection set").
  const [showSelectedOnly, setShowSelectedOnly] = useState(false)
  const [bounds, setBounds] = useState({ lonMin: '', latMin: '', lonMax: '', latMax: '' })
  const [showCols, setShowCols] = useState(false)
  const [showFilters, setShowFilters] = useState(false)
  const [showExport, setShowExport] = useState(false)
  const [showImport, setShowImport] = useState(false) // E4: bulk feature-ingest dialog
  const [showCalc, setShowCalc] = useState(false) // geometry calc / bulk set field value dialog
  const [reloadKey, setReloadKey] = useState(0) // bump to refetch after an import commit
  // Output-CRS reproject for exports (E1, decision-working-crs-reproject.md). null = the layer's storage CRS.
  const [exportSrid, setExportSrid] = useState<number | null>(null)
  const [exportSridName, setExportSridName] = useState('')
  const [crsQuery, setCrsQuery] = useState('')
  const [crsResults, setCrsResults] = useState<SpatialRef[]>([])
  const [colSort, setColSort] = useState<ColSort>('db')
  const [detail, setDetail] = useState<LayerFeature | null>(null) // the row whose detail drawer is open
  // Row create/edit dialog for a standalone non-spatial TABLE (decision-nonspatial-tables.md t4):
  // 'new' = add a row; an object = edit that row. Spatial layers keep the map as their edit surface.
  const [rowForm, setRowForm] = useState<'new' | { recordId: string; properties: Record<string, unknown> } | null>(null)
  const isTable = layer.geomType === 'none'

  // initialFilters rides a ref: the metadata effect seeds from it once per layer without taking a
  // dep on the array identity (the parent re-creates it every render).
  const initialFiltersRef = useRef(initialFilters)
  initialFiltersRef.current = initialFilters
  // Canonical key of the conditions this grid last seeded from or committed — the echo detector for
  // the external-reseed effect below (own commits round-trip through the parent as an identical key).
  const lastCondsKeyRef = useRef('')

  // Metadata: columns + coded picklists. Loaded once per layer; seeds the filter cards + pinned
  // remainder from the layer's current map filter (filter-sync).
  useEffect(() => {
    if (!token) return
    let active = true
    setColumns(null)
    setError(null)
    Promise.all([
      fetchLayerColumns(layer.id, token),
      // Never block the grid on the star state — degrade to "no star" if it fails (e.g. unprovisioned layer).
      fetchLayerDisplayField(layer.id, token).catch(() => ({ displayField: null, canManage: false })),
    ])
      .then(([cols, df]) => {
        if (!active) return
        setColumns(cols.columns)
        setDisplayField(df.displayField)
        setCanManageDisplay(df.canManage)
        // Coded owner fields: load each bound custom domain's values (labels + filter options).
        setCustomDomains({})
        const domainIds = [...new Set(cols.columns.map((c) => c.customDomain).filter((d): d is string => !!d))]
        for (const id of domainIds) {
          getCustomDomain(id, token)
            .then((d) => active && setCustomDomains((prev) => ({ ...prev, [id]: d.values })))
            .catch(() => {
              /* raw codes render for this domain's fields */
            })
        }
        setHidden(new Set(DEFAULT_HIDDEN))
        const seed = seedFromConditions(initialFiltersRef.current ?? [], cols.columns)
        lastCondsKeyRef.current = condsKey(initialFiltersRef.current ?? [])
        setFilters(seed.cards)
        setPinned(seed.rest)
        setShowFilters(false)
        setSort(null)
        setOffset(0)
        // Internal only: a controlled selection belongs to the docked panel and must SURVIVE this
        // remount-time reset — reopening the panel is exactly when it fires (F2 lifetime).
        setInternalSelected(new Set())
        setShowSelectedOnly(false)
        setColSort('db')
        setShowExport(false)
        setDetail(null)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [layer.id, token])

  // Panel↔table co-edit (the filter-sync remainder): when the layer's shared filter changes
  // EXTERNALLY — the funnel FilterPanel applied while this table is open — reseed the cards, the
  // pinned chips, AND any staged draft (the panel's Apply is an explicit commit; it wins over an
  // unapplied draft). Own commits echo back through the parent with an identical key and are skipped.
  const externalKey = condsKey(initialFilters ?? [])
  useEffect(() => {
    if (!columns || externalKey === lastCondsKeyRef.current) return
    lastCondsKeyRef.current = externalKey
    const seed = seedFromConditions(initialFiltersRef.current ?? [], columns)
    setFilters(seed.cards)
    setPinned(seed.rest)
    setDraftFilters(seed.cards)
    setDraftPinned(seed.rest)
    setOffset(0)
  }, [externalKey, columns])

  const filterStrings = useMemo(
    () => [...buildFilterStrings(filters), ...pinned.map((c) => `${c.field}:${c.op}:${c.value}`)],
    [filters, pinned],
  )
  const sortString = sort ? `${sort.field}:${sort.dir}` : undefined
  const bboxString =
    scopeBbox ??
    (bounds.lonMin && bounds.latMin && bounds.lonMax && bounds.latMax
      ? `${bounds.lonMin},${bounds.latMin},${bounds.lonMax},${bounds.latMax}`
      : undefined)

  // Feature fetch — debounced + abortable; re-runs on any filter/sort/page/bbox change.
  // Selected-mode scoping: the sorted-ids key makes membership changes (not Set identity) the
  // refetch/paging trigger, and stays '' while the mode is off so ordinary selecting never refetches.
  const selectedIdsKey = showSelectedOnly ? Array.from(selected).sort().join(',') : ''
  useEffect(() => {
    if (showSelectedOnly && selected.size === 0) setShowSelectedOnly(false) // auto-revert (F5)
  }, [showSelectedOnly, selected])
  useEffect(() => {
    setOffset(0) // the scoped set changed — paging restarts (the scopeBbox precedent: reset, don't stitch)
  }, [selectedIdsKey])
  // The UNSCOPED numberMatched, cached so the "N features" segment stays honest while the grid is
  // scoped to the selection (numberMatched is then the selected-set size). Null = unknown for the
  // current filter/bbox shape (renders as '…' until an unscoped fetch lands).
  const [allTotal, setAllTotal] = useState<number | null>(null)
  useEffect(() => {
    setAllTotal(null)
  }, [layer.id, filterStrings, bboxString, reloadKey])

  useEffect(() => {
    if (!token || !columns) return
    const scoped = showSelectedOnly && selected.size > 0
    const ctrl = new AbortController()
    const handle = setTimeout(() => {
      setLoading(true)
      queryLayerFeatures(
        layer.id,
        {
          filters: filterStrings,
          sort: sortString,
          bbox: bboxString,
          ids: scoped ? Array.from(selected) : undefined,
          limit: pageSize,
          offset,
        },
        token,
        ctrl.signal,
      )
        .then((d) => {
          setData(d)
          if (!scoped) setAllTotal(d.numberMatched)
          setError(null)
        })
        .catch((e) => {
          if (e?.name === 'AbortError') return
          setError(e instanceof ApiError ? e.message : String(e))
        })
        .finally(() => setLoading(false))
    }, 250)
    return () => {
      clearTimeout(handle)
      ctrl.abort()
    }
    // filterStrings is a useMemo'd array (stable unless `filters` changes), so it's a sound dep.
    // selectedIdsKey stands in for (showSelectedOnly, selected-membership) — '' while the mode is
    // off — so listing those two directly would re-fetch on every checkbox toggle for nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layer.id, token, columns, filterStrings, sortString, bboxString, selectedIdsKey, offset, pageSize, reloadKey])

  // code -> label maps, keyed by built-in domain table AND custom-domain id (one lookup in cell()).
  const customLabels = useMemo(() => {
    const out: Record<string, Record<string, string>> = {}
    for (const [id, values] of Object.entries(customDomains)) {
      const m: Record<string, string> = {}
      for (const v of values) m[v.code] = v.label
      out[id] = m
    }
    return out
  }, [customDomains])
  const codeLabels = customLabels

  if (error) return <p className="signs__error">{error}</p>
  if (!columns) return <p className="signs__empty">Loading…</p>

  const ordered = sortColumns(columns, colSort)
  const visible = ordered.filter((c) => !hidden.has(c.name))
  const total = data?.numberMatched ?? 0
  // The "N features" label ("N of M (filtered)" so a correct 0-match result doesn't read as a
  // broken/empty table; featureCount = the full unfiltered total from /me/layers). While the grid
  // is scoped to the selection, N comes from the cached UNSCOPED total ('…' until known).
  const featTotal = showSelectedOnly ? allTotal : total
  const featCount = featTotal !== null ? featTotal.toLocaleString() : '…'
  const featLabel = filterStrings.length
    ? `${featCount}${
        typeof layer.featureCount === 'number' ? ` of ${layer.featureCount.toLocaleString()}` : ''
      } features (filtered)`
    : `${featCount} features`
  const pageFeatures = data?.features ?? []
  const from = total === 0 ? 0 : offset + 1
  const to = offset + pageFeatures.length

  // Commit the typed rows-per-page: clamp to 1..MAX_PAGE and jump back to the first page
  // (paging math is offset-based, so a size change must reset the offset).
  const applyPageSize = () => {
    const n = Math.max(1, Math.min(MAX_PAGE, Math.floor(Number(pageSizeText) || DEFAULT_PAGE)))
    setPageSize(n)
    setPageSizeText(String(n))
    setOffset(0)
  }

  // Cards edit the DRAFT; nothing refetches until commit (FilterPanel parity).
  const setDraftFilter = (field: string, next: FieldFilter | null) => {
    setDraftFilters((prev) => {
      const copy = { ...prev }
      if (next === null) delete copy[field]
      else copy[field] = next
      return copy
    })
  }
  // Commit staged filters: refetch the grid (offset reset — never page past a shrunken result)
  // AND write through to the layer's map filter when hosted in the map panel.
  const commitFilters = (cards: Record<string, FieldFilter>, pins: FilterCondition[]): void => {
    setFilters(cards)
    setPinned(pins)
    setOffset(0)
    const conds = [...buildFilterConditions(cards), ...pins]
    lastCondsKeyRef.current = condsKey(conds) // our own echo — the external-reseed effect skips it
    onApplyFilters?.(conds)
  }
  const openFiltersFlyout = (): void => {
    setShowFilters((v) => {
      if (!v) {
        // (Re)stage from the committed state on every open.
        setDraftFilters(filters)
        setDraftPinned(pinned)
      }
      return !v
    })
  }
  const toggleSort = (field: string) => {
    setSort((prev) =>
      prev?.field !== field ? { field, dir: 'asc' } : prev.dir === 'asc' ? { field, dir: 'desc' } : null,
    )
    setOffset(0)
  }
  // Star / unstar a column as the layer display field (manage-gated). Clicking the current star clears it
  // (→ the map falls back to a derived default). Optimistic with revert-on-error; the map picks up the new
  // value on its next /me/layers read.
  const toggleDisplayField = (field: string) => {
    if (!token || !canManageDisplay) return
    const next = displayField === field ? null : field
    const prev = displayField
    setDisplayField(next)
    setLayerDisplayField(layer.id, next, token)
      .then((saved) => setDisplayField(saved.displayField))
      .catch((e) => {
        setDisplayField(prev)
        setError(e instanceof ApiError ? e.message : 'Could not update the display field')
      })
  }
  const canStar = (name: string) => !STAR_SKIP.has(name)
  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const copy = new Set(prev)
      if (copy.has(id)) copy.delete(id)
      else copy.add(id)
      return copy
    })
  const selectAllOnPage = (on: boolean) =>
    setSelected((prev) => {
      const copy = new Set(prev)
      for (const f of pageFeatures) {
        if (!f.id) continue
        if (on) copy.add(f.id)
        else copy.delete(f.id)
      }
      return copy
    })

  const exportIds = selected.size > 0 ? Array.from(selected) : undefined
  // GeoJSON is always WGS-84, so the chosen output CRS only applies to CSV/Shapefile/GeoPackage.
  const doExport = (format: 'csv' | 'geojson' | 'gpkg') => {
    if (!token) return
    void downloadLayerExport(
      layer.id,
      {
        filters: filterStrings,
        sort: sortString,
        bbox: bboxString,
        ids: exportIds,
        srid: format === 'geojson' ? null : exportSrid,
      },
      format,
      token,
    )
      .then((r) => {
        // A silently incomplete download is the failure this warning exists to prevent
        // (decision-export-truncation.md) — say it where the person who clicked is looking.
        if (r.truncated) {
          setError(
            `Export INCOMPLETE: the file contains ${r.returned.toLocaleString()} features — the ` +
            `server's per-export limit. Narrow the export with a filter, a bounding box, or a ` +
            `selection and export again to get the rest. The file is named …-TRUNCATED-at-${r.cap}.`,
          )
        }
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
  }
  async function runCrsSearch(q: string): Promise<void> {
    setCrsQuery(q)
    if (!token) return
    try {
      setCrsResults(await searchSpatialRefs(q, token))
    } catch {
      setCrsResults([])
    }
  }

  const allPageSelected = pageFeatures.length > 0 && pageFeatures.every((f) => f.id && selected.has(f.id))
  // Filter columns track the table's visible columns (hidden columns drop out of the filter
  // panel too) and the chosen column order.
  const filterable = ordered.filter((c) => filterKind(c) !== 'none' && !hidden.has(c.name))

  return (
    <div className="dview__body">
      <div className="dtable__toolbar">
        {/* Toolbar order: Columns first, Filters second. */}
        <button className="btn btn--sm" onClick={() => setShowCols((v) => !v)}>
          Columns
        </button>
        <button className="btn btn--sm" onClick={openFiltersFlyout}>
          Filters{filterStrings.length ? ` (${filterStrings.length})` : ''}
        </button>
        {layer.canEdit && ops.add && isTable && (
          <button className="btn btn--sm" onClick={() => setRowForm('new')} title="Add a row to this table">
            + Add row
          </button>
        )}
        {/* Feature import needs geometry — a non-spatial table adds rows via + Add row (attribute-only
            import is a queued follow-on), so don't offer a button the server would 422. hideImport:
            the map-hosted panel never offers it (imports live in the Catalog). */}
        {layer.canEdit && ops.add && !isTable && !hideImport && (
          <button className="btn btn--sm" onClick={() => setShowImport(true)} title="Import features from GeoJSON">
            Import
          </button>
        )}
        {/* Geometry calc + bulk set field value (decision-geometry-calc.md) — edit-gated, BOTH grid
            surfaces (a table gets the 'set' op only; the dialog gates ops by geometry). */}
        {layer.canEdit && ops.attributes && (
          <button
            className="btn btn--sm"
            onClick={() => setShowCalc(true)}
            title="Populate a field with X/Y, length, area — or set a value on the scoped rows"
          >
            Calculate
          </button>
        )}
        <span className="signs__count">
          {layer.id}
          {selected.size === 0 ? ` · ${featLabel}` : ''}
          {loading ? ' · …' : ''}
        </span>
        <div className="dtable__export">
          <button
            className="btn btn--sm dtable__export-btn"
            onClick={() => setShowExport((v) => !v)}
            aria-label="Export"
            title="Export"
          >
            <DownloadIcon />
            {exportIds ? ` ${exportIds.length}` : ''} ▾
          </button>
          {showExport && (
            <div className="dtable__export-menu">
              {/* Output CRS (E1): default = the layer's storage CRS; search to reproject CSV/Shapefile/GeoPackage.
                  A non-spatial TABLE has no geometry — no CRS to output (and no GeoPackage below). */}
              {!isTable && <div style={{ padding: '0.4rem 0.6rem', borderBottom: '1px solid var(--border-default)' }}>
                <div className="modal__muted" style={{ fontSize: '0.8em', marginBottom: '0.25rem' }}>Output CRS</div>
                <div style={{ fontSize: '0.85em', marginBottom: '0.25rem' }}>
                  {exportSrid == null
                    ? `Layer’s CRS (EPSG:${layer.nativeStorage ? layer.workingSrid : 4326})`
                    : `${exportSridName} (EPSG:${exportSrid})`}
                  {exportSrid != null && (
                    <button
                      type="button"
                      style={{ marginLeft: '0.4rem', fontSize: '0.85em' }}
                      onClick={() => { setExportSrid(null); setExportSridName(''); setCrsQuery(''); setCrsResults([]) }}
                    >
                      × use layer CRS
                    </button>
                  )}
                </div>
                <input
                  type="text"
                  style={{ width: '100%' }}
                  placeholder="Reproject — search CRS (e.g. UTM, 3857)"
                  value={crsQuery}
                  onChange={(e) => void runCrsSearch(e.target.value)}
                />
                {crsResults.length > 0 && (
                  <ul style={{ listStyle: 'none', margin: '0.25rem 0 0', padding: 0, maxHeight: '8rem', overflow: 'auto' }}>
                    {crsResults.map((r) => (
                      <li key={r.srid}>
                        <button
                          type="button"
                          style={{ width: '100%', textAlign: 'left', fontSize: '0.85em' }}
                          onClick={() => { setExportSrid(r.srid); setExportSridName(r.name); setCrsQuery(''); setCrsResults([]) }}
                        >
                          {r.name} <span className="modal__muted">(EPSG:{r.srid})</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>}
              <button onClick={() => { doExport('csv'); setShowExport(false) }}>
                CSV{exportIds ? ` · ${exportIds.length} selected` : ''}
              </button>
              <button onClick={() => { doExport('geojson'); setShowExport(false) }}>
                GeoJSON{' '}
                {!isTable && <span className="modal__muted" style={{ fontSize: '0.8em' }}>(WGS-84)</span>}
                {exportIds ? ` · ${exportIds.length} selected` : ''}
              </button>
              {/* GeoPackage/Shapefile need geometry — the server 422s them for a table, so don't offer it. */}
              {!isTable && (
                <button onClick={() => { doExport('gpkg'); setShowExport(false) }}>
                  GeoPackage{exportIds ? ` · ${exportIds.length} selected` : ''}
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {showCols && (
        <div className="dtable__cols">
          <div className="dtable__cols-bulk">
            <input
              type="checkbox"
              ref={(el) => {
                if (el) el.indeterminate = visible.length > 0 && visible.length < ordered.length
              }}
              checked={ordered.length > 0 && visible.length === ordered.length}
              onChange={() =>
                setHidden(
                  visible.length === ordered.length ? new Set(columns.map((c) => c.name)) : new Set(),
                )
              }
              aria-label="Toggle all columns"
            />
            <button className="dtable__seg" onClick={() => setHidden(new Set())}>
              Select all
            </button>
            <button className="dtable__seg" onClick={() => setHidden(new Set(columns.map((c) => c.name)))}>
              Select none
            </button>
            <span className="dtable__cols-count">
              {visible.length}/{ordered.length} shown
            </span>
          </div>
          <div className="dtable__cols-sort">
            <span className="dtable__filter-label">Sort columns</span>
            {(['db', 'name', 'alias'] as ColSort[]).map((mode) => (
              <button
                key={mode}
                className={`dtable__seg${colSort === mode ? ' is-active' : ''}`}
                onClick={() => setColSort(mode)}
              >
                {mode === 'db' ? 'DB order' : mode === 'name' ? 'Name' : 'Alias'}
              </button>
            ))}
          </div>
          <div className="dtable__cols-list">
            {ordered.map((c) => {
              const starred = displayField === c.name
              const showStar = starred || (canManageDisplay && canStar(c.name))
              return (
                <div className="dtable__cols-row" key={c.name}>
                  <label>
                    <input
                      type="checkbox"
                      checked={!hidden.has(c.name)}
                      onChange={(e) =>
                        setHidden((prev) => {
                          const copy = new Set(prev)
                          if (e.target.checked) copy.delete(c.name)
                          else copy.add(c.name)
                          return copy
                        })
                      }
                    />
                    {colLabel(c)}
                  </label>
                  {showStar && (
                    <button
                      type="button"
                      className={`dtable__star${starred ? ' is-set' : ''}`}
                      onClick={canManageDisplay ? () => toggleDisplayField(c.name) : undefined}
                      disabled={!canManageDisplay}
                      aria-pressed={starred}
                      title={
                        starred
                          ? canManageDisplay
                            ? 'Display field — click to clear'
                            : 'This is the layer’s display field (set by the owner)'
                          : 'Set as the display field'
                      }
                    >
                      <Star size={13} fill={starred ? 'currentColor' : 'none'} aria-hidden="true" />
                    </button>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {showFilters && (
        <div className="dtable__filters-bar">
          <span className="dtable__filters-hint">
            {onApplyFilters
              ? 'Apply also filters this layer on the map — the filter stays until cleared here or in the Filter tool.'
              : 'Build filters below, then Apply.'}
          </span>
          <div className="dtable__filters-btns">
            {/* FilterPanel parity: Clear all commits [] immediately; Cancel discards the draft. */}
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => {
                setDraftFilters({})
                setDraftPinned([])
                commitFilters({}, [])
              }}
            >
              Clear all
            </button>
            <button type="button" className="btn btn--sm" onClick={() => setShowFilters(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => {
                commitFilters(draftFilters, draftPinned)
                setShowFilters(false)
              }}
            >
              Apply
            </button>
          </div>
        </div>
      )}
      {showFilters && (
        <div className="dtable__filters">
          {draftPinned.length > 0 && (
            <div className="dtable__fcol dtable__fcol--pinned">
              <span className="dtable__filter-label">From the map filter</span>
              {draftPinned.map((c, i) => (
                <span className="dtable__pinchip" key={`${c.field}:${c.op}:${i}`}>
                  {displayName(c.field)} {OP_LABELS[c.op] ?? c.op} {c.value}
                </span>
              ))}
              <span className="dtable__pinnote">
                No card form for these — edit in the map Filter tool; Clear all removes them.
              </span>
            </div>
          )}
          {filterable.map((c) => (
            <FilterColumn
              key={c.name}
              col={c}
              value={draftFilters[c.name]}
              options={optionsFor(c, customDomains)}
              onChange={(v) => setDraftFilter(c.name, v)}
            />
          ))}
          {!scopeBbox && (
            <BoundsColumn bounds={bounds} onChange={(b) => { setBounds(b); setOffset(0) }} />
          )}
        </div>
      )}

      {/* §4.3 — the selection had no state of its own: rows highlighted and the toolbar grew a
          pill, but nothing said what could be DONE with them. Amber = armed, the rule Map 2a set.
          The actions are the host's (Zoom to, Delete) or already in the toolbar (Calculate) — this
          bar re-homes them at the scope they act on, it does not add capability. */}
      {selected.size > 0 && (
        <div className="dtable__selbar" role="group" aria-label="Selection">
          <span className="dtable__selcount">{selected.size.toLocaleString()} selected</span>
          {/* With a selection the counts BECOME the scope control: "N selected"
              scopes the grid to the selected set, "N features" restores all. Server-side scoping
              (F5), so paging/sort/filters stay honest either way. */}
          <div className="seg seg--sm" role="group" aria-label="Rows shown">
            <button
              type="button"
              className={showSelectedOnly ? '' : 'on'}
              title="Show all records"
              onClick={() => setShowSelectedOnly(false)}
            >
              {featLabel}
            </button>
            <button
              type="button"
              className={showSelectedOnly ? 'on' : ''}
              title="Show only the selected records"
              onClick={() => setShowSelectedOnly(true)}
            >
              {selected.size.toLocaleString()} selected
            </button>
          </div>
          <div className="dtable__selacts">
            {/* A geometry-less table has nothing to zoom to (§4.6). */}
            {onZoomToSelected && !isTable && (
              <button type="button" className="btn btn--sm" onClick={onZoomToSelected}>
                Zoom to
              </button>
            )}
            {layer.canEdit && ops.attributes && (
              <button type="button" className="btn btn--sm" onClick={() => setShowCalc(true)}>
                Calculate field
              </button>
            )}
            <button type="button" className="btn btn--sm" onClick={() => setSelected(new Set())}>
              Clear
            </button>
            {/* A destructive action states its scope — "Delete 3", never "Delete". */}
            {onDeleteSelected && (
              <button type="button" className="btn btn--sm btn--danger" onClick={onDeleteSelected}>
                Delete {selected.size.toLocaleString()}
              </button>
            )}
          </div>
        </div>
      )}

      <div className="dtable__scroll">
        <table className="signs__table dtable">
          <thead>
            <tr>
              <th className="dtable__check">
                <input
                  type="checkbox"
                  checked={allPageSelected}
                  onChange={(e) => selectAllOnPage(e.target.checked)}
                  aria-label="Select all on page"
                />
              </th>
              {visible.map((c) => {
                const starred = displayField === c.name
                // Show the star when this caller can set it (on a name-able column) OR it's the active star
                // (so viewers/editors see which field names the layer, disabled — decision fork F3/F4).
                const showStar = starred || (canManageDisplay && canStar(c.name))
                return (
                  <th key={c.name}>
                    <span className="dtable__th">
                      {showStar && (
                        <button
                          type="button"
                          className={`dtable__star${starred ? ' is-set' : ''}`}
                          onClick={canManageDisplay ? () => toggleDisplayField(c.name) : undefined}
                          disabled={!canManageDisplay}
                          aria-pressed={starred}
                          title={
                            starred
                              ? canManageDisplay
                                ? 'Display field — click to clear'
                                : 'This is the layer’s display field (set by the owner)'
                              : 'Set as the display field — names each feature in popups and search'
                          }
                        >
                          <Star size={13} fill={starred ? 'currentColor' : 'none'} aria-hidden="true" />
                        </button>
                      )}
                      <button
                        className={`dtable__sort${sort?.field === c.name ? ' is-sorted' : ''}`}
                        onClick={() => toggleSort(c.name)}
                      >
                        {colLabel(c)}
                        {sort?.field === c.name &&
                          (sort.dir === 'asc' ? (
                            <ArrowUp size={12} aria-hidden="true" />
                          ) : (
                            <ArrowDown size={12} aria-hidden="true" />
                          ))}
                      </button>
                    </span>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {pageFeatures.map((f) => (
              <tr
                key={f.id ?? Math.random()}
                className={`dtable__row${f.id && selected.has(f.id) ? ' is-selected' : ''}${
                  f.id && detail?.id === f.id ? ' is-open' : ''
                }`}
                onClick={() => f.id && setDetail(f)}
                title="View record detail"
              >
                <td className="dtable__check" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={!!f.id && selected.has(f.id)}
                    onChange={() => f.id && toggleSelect(f.id)}
                    aria-label="Select row"
                  />
                </td>
                {visible.map((c) => (
                  <td key={c.name} data-kind={columnKind(c)} data-null={isBlank(c, f.properties) || undefined} title={cellTitle(c, f.properties)}>
                    <CellValue col={c} props={f.properties} codeLabels={codeLabels} />
                  </td>
                ))}
              </tr>
            ))}
            {pageFeatures.length === 0 && (
              <tr>
                <td colSpan={visible.length + 1} className="signs__empty">
                  {filterStrings.length ? (
                    <>
                      No rows match the current filters.{' '}
                      <button
                        type="button"
                        className="dtable__clearfilters"
                        onClick={() => {
                          setDraftFilters({})
                          setDraftPinned([])
                          commitFilters({}, [])
                        }}
                      >
                        Clear filters
                      </button>
                    </>
                  ) : (
                    'No features match.'
                  )}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="dtable__pager">
        {/* §4.4 — the "filtered from" clause renders ONLY while a filter is on, so a filtered grid
            can never be mistaken for the whole layer. Mono + tabular: these are counts to compare. */}
        <span className="dtable__pagecount">
          {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}
          {filterStrings.length > 0 && typeof layer.featureCount === 'number' && (
            <span className="dtable__filteredfrom">
              {' · '}filtered from {layer.featureCount.toLocaleString()}
            </span>
          )}
        </span>
        <label className="dtable__pagesize">
          Rows
          <input
            type="number"
            min={1}
            max={MAX_PAGE}
            value={pageSizeText}
            onChange={(e) => setPageSizeText(e.target.value)}
            onBlur={applyPageSize}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                applyPageSize()
                ;(e.target as HTMLInputElement).blur()
              }
            }}
            aria-label="Rows per page"
            title={`Rows per page (1–${MAX_PAGE.toLocaleString()})`}
          />
        </label>
        <button className="btn btn--sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>
          <ChevronLeft size={13} aria-hidden="true" /> Prev
        </button>
        <button className="btn btn--sm" disabled={to >= total} onClick={() => setOffset(offset + pageSize)}>
          Next <ChevronRight size={13} aria-hidden="true" />
        </button>
      </div>

      {detail?.id && (
        <RecordDetail
          layerId={layer.id}
          layerLabel={layer.displayName ?? layer.id}
          columns={columns}
          recordId={detail.id}
          properties={detail.properties}
          onClose={() => setDetail(null)}
          geometry={detail.geometry}
          displayField={layer.displayField}
          geomType={layer.geomType}
          displayColor={layer.displayColor}
          // Table rows edit from the grid (no map surface); spatial layers keep the map edit flow.
          onEdit={
            layer.canEdit && ops.attributes && isTable
              ? () => {
                  setRowForm({ recordId: detail.id as string, properties: detail.properties })
                  setDetail(null)
                }
              : undefined
          }
        />
      )}

      {rowForm !== null && token && (
        <RowFormDialog
          layer={layer}
          accessToken={token}
          editing={rowForm === 'new' ? null : rowForm}
          onClose={() => setRowForm(null)}
          onSaved={() => {
            setDetail(null)
            setReloadKey((k) => k + 1)
          }}
        />
      )}

      {showImport && (
        <ImportFeaturesDialog
          layer={layer}
          token={token}
          onClose={() => setShowImport(false)}
          onImported={() => {
            setOffset(0)
            setReloadKey((k) => k + 1)
          }}
        />
      )}

      {showCalc && (
        <CalculateDialog
          layer={layer}
          columns={columns}
          customDomains={customDomains}
          token={token}
          // The grid's live predicates, export parity: selection (when any) narrows the filtered set.
          scope={{ filters: filterStrings, ids: exportIds, bbox: bboxString }}
          scopeLabel={
            selected.size
              ? `${selected.size.toLocaleString()} selected row${selected.size === 1 ? '' : 's'}`
              : filterStrings.length || bboxString
                ? `all ${total.toLocaleString()} filtered rows`
                : `all ${total.toLocaleString()} rows`
          }
          onClose={() => setShowCalc(false)}
          onApplied={() => setReloadKey((k) => k + 1)}
        />
      )}
    </div>
  )
}

// --- helpers --------------------------------------------------------------------

function sortColumns(cols: LayerColumn[], mode: ColSort): LayerColumn[] {
  if (mode === 'db') return cols // native table sequence (ordinal_position)
  const copy = [...cols]
  if (mode === 'name') copy.sort((a, b) => a.name.localeCompare(b.name))
  else copy.sort((a, b) => colLabel(a).localeCompare(colLabel(b))) // alias
  return copy
}

function DownloadIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ verticalAlign: '-2px' }}
    >
      <path d="M12 3v12" />
      <path d="M7 11l5 5 5-5" />
      <path d="M5 21h14" />
    </svg>
  )
}

/** Order-insensitive canonical key for a condition set — the echo detector for panel↔table co-edit. */
function condsKey(conds: FilterCondition[]): string {
  return conds
    .map((c) => `${c.field}:${c.op}:${c.value}`)
    .sort()
    .join('|')
}

/** Filter-sync seeding: split a layer's map-filter conditions into grid filter CARDS (representable
 * ops: in/eq on coded/boolean → checkbox card; ilike on text → contains card; gte/lte on date/number
 * → range card) + a pinned REST (ne, eq-on-text/number, duplicates a card can't hold) that rides
 * every fetch unchanged so nothing authored in the Filter tool is silently dropped. */
function seedFromConditions(
  conds: FilterCondition[],
  columns: LayerColumn[],
): { cards: Record<string, FieldFilter>; rest: FilterCondition[] } {
  const byName = new Map(columns.map((c) => [c.name, c]))
  const cards: Record<string, FieldFilter> = {}
  const rest: FilterCondition[] = []
  for (const c of conds) {
    const col = byName.get(c.field)
    const kind = col ? filterKind(col) : 'none'
    const cur = cards[c.field]
    if ((kind === 'coded' || kind === 'boolean') && (c.op === 'in' || c.op === 'eq')) {
      const codes = c.op === 'in' ? c.value.split(',') : [c.value]
      if (!cur) cards[c.field] = { kind: 'in', codes }
      else if (cur.kind === 'in') cards[c.field] = { kind: 'in', codes: [...new Set([...cur.codes, ...codes])] }
      else rest.push(c)
    } else if (kind === 'text' && c.op === 'ilike' && !cur) {
      cards[c.field] = { kind: 'text', val: c.value }
    } else if ((kind === 'date' || kind === 'number') && (c.op === 'gte' || c.op === 'lte')) {
      const range = cur?.kind === 'range' ? cur : { kind: 'range' as const, from: '', to: '' }
      if (c.op === 'gte' && !range.from) cards[c.field] = { ...range, from: c.value }
      else if (c.op === 'lte' && !range.to) cards[c.field] = { ...range, to: c.value }
      else rest.push(c)
    } else {
      rest.push(c)
    }
  }
  return { cards, rest }
}

/** The same predicates as buildFilterStrings, structured as map FilterConditions (the filter
 * write-through's currency). Identical semantics — both feed the one /features grammar. */
function buildFilterConditions(filters: Record<string, FieldFilter>): FilterCondition[] {
  const out: FilterCondition[] = []
  for (const [field, f] of Object.entries(filters)) {
    if (f.kind === 'in' && f.codes.length) out.push({ field, op: 'in', value: f.codes.join(',') })
    else if (f.kind === 'text' && f.val.trim()) out.push({ field, op: 'ilike', value: f.val.trim() })
    else if (f.kind === 'range') {
      if (f.from) out.push({ field, op: 'gte', value: f.from })
      if (f.to) out.push({ field, op: 'lte', value: f.to })
    }
  }
  return out
}

function buildFilterStrings(filters: Record<string, FieldFilter>): string[] {
  const out: string[] = []
  for (const [field, f] of Object.entries(filters)) {
    if (f.kind === 'in' && f.codes.length) out.push(`${field}:in:${f.codes.join(',')}`)
    else if (f.kind === 'text' && f.val.trim()) out.push(`${field}:ilike:${f.val.trim()}`)
    else if (f.kind === 'range') {
      if (f.from) out.push(`${field}:gte:${f.from}`)
      if (f.to) out.push(`${field}:lte:${f.to}`)
    }
  }
  return out
}

// --- filter columns -------------------------------------------------------------
//
// The filter panel is a horizontally-scrollable row of per-field "columns":
// each field gets its own column with the breakdown of values beneath it,
// vertically scrollable — so a field with hundreds of values stays usable, and many fields
// just scroll sideways. Coded/boolean -> checkbox option list (contains-box searches the
// options); text -> a contains box (-> ilike); date/number -> a from/to range.

/** The option list for a column's checkbox breakdown: a coded field's domain (built-in table
 * or a coded owner field's custom domain), or Yes/No for a boolean. Empty for text/date/number
 * (those don't use a checkbox list). */
function optionsFor(
  col: LayerColumn,
  customDomains: Record<string, DomainValue[]>,
): { code: string; label: string }[] {
  if (col.customDomain) return (customDomains[col.customDomain] ?? []).map((v) => ({ code: v.code, label: v.label }))
  if (col.type === 'boolean') {
    return [
      { code: 'true', label: 'Yes' },
      { code: 'false', label: 'No' },
    ]
  }
  return []
}

function FilterColumn({
  col,
  value,
  options,
  onChange,
}: {
  col: LayerColumn
  value: FieldFilter | undefined
  options: { code: string; label: string }[]
  onChange: (v: FieldFilter | null) => void
}) {
  const [search, setSearch] = useState('')
  const kind = filterKind(col)
  const header = <span className="dtable__filter-label">{colLabel(col)}</span>

  if (kind === 'coded' || kind === 'boolean') {
    const codes = value?.kind === 'in' ? value.codes : []
    const q = search.trim().toLowerCase()
    const shown = q
      ? options.filter((o) => o.label.toLowerCase().includes(q) || o.code.toLowerCase().includes(q))
      : options
    return (
      <div className="dtable__fcol">
        {header}
        {kind === 'coded' && (
          <input
            className="dtable__fsearch"
            placeholder="contains…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label={`Search ${col.name} values`}
          />
        )}
        <div className="dtable__foptions">
          {shown.map((o) => (
            <label key={o.code}>
              <input
                type="checkbox"
                checked={codes.includes(o.code)}
                onChange={(e) => {
                  const next = e.target.checked ? [...codes, o.code] : codes.filter((c) => c !== o.code)
                  onChange(next.length ? { kind: 'in', codes: next } : null)
                }}
              />
              {o.label}
            </label>
          ))}
          {shown.length === 0 && <span className="dtable__fempty">No matches</span>}
        </div>
      </div>
    )
  }

  if (kind === 'text') {
    const v = value?.kind === 'text' ? value.val : ''
    return (
      <div className="dtable__fcol">
        {header}
        <input
          className="dtable__fsearch"
          placeholder="contains…"
          value={v}
          onChange={(e) => onChange(e.target.value ? { kind: 'text', val: e.target.value } : null)}
        />
      </div>
    )
  }

  // date / number -> a from/to range
  const range = value?.kind === 'range' ? value : { from: '', to: '' }
  const inputType = kind === 'date' ? 'date' : 'number'
  const setRange = (next: { from: string; to: string }) =>
    onChange(next.from || next.to ? { kind: 'range', ...next } : null)
  return (
    <div className="dtable__fcol">
      {header}
      <input
        type={inputType}
        value={range.from}
        onChange={(e) => setRange({ from: e.target.value, to: range.to })}
        aria-label={`${col.name} from`}
      />
      <input
        type={inputType}
        value={range.to}
        onChange={(e) => setRange({ from: range.from, to: e.target.value })}
        aria-label={`${col.name} to`}
      />
    </div>
  )
}

function BoundsColumn({
  bounds,
  onChange,
}: {
  bounds: { lonMin: string; latMin: string; lonMax: string; latMax: string }
  onChange: (b: { lonMin: string; latMin: string; lonMax: string; latMax: string }) => void
}) {
  const field = (key: keyof typeof bounds) => (
    <input
      type="number"
      placeholder={key}
      value={bounds[key]}
      onChange={(e) => onChange({ ...bounds, [key]: e.target.value })}
      aria-label={key}
    />
  )
  return (
    <div className="dtable__fcol">
      <span className="dtable__filter-label">Bounds (lon/lat)</span>
      {field('lonMin')}
      {field('latMin')}
      {field('lonMax')}
      {field('latMax')}
    </div>
  )
}

/** How many codes a domain may have before chips stop helping (§4.2). Above a handful the column
 * becomes a wall of pills and the chip stops meaning "one of a short list". */
const BADGE_MAX_CODES = 6

/**
 * One grid cell's VALUE (handoff-record-table-3 §4.1/§4.2). Every cell used to render as the same
 * sans string, so a reflectivity reading and a sign type looked alike. `cell()` is still the single
 * source of truth for the TEXT; this only decides how that text is presented — a boolean as a mark
 * rather than the word "true", a short coded domain as a chip.
 */
function CellValue({
  col,
  props,
  codeLabels,
}: {
  col: LayerColumn
  props: Record<string, unknown>
  codeLabels: Record<string, Record<string, string>>
}) {
  const text = cell(col, props, codeLabels)
  if (isBlank(col, props)) return <>{text}</>

  const kind = columnKind(col)
  if (kind === 'boolean') {
    const raw = col.props
      ? (props.props as Record<string, unknown> | null | undefined)?.[col.name]
      : props[col.name]
    const on = raw === true || raw === 't' || raw === 'true' || raw === 1
    return on ? <Check size={14} aria-label="Yes" /> : <Minus size={14} aria-label="No" />
  }

  if (kind === 'coded') {
    const dom = col.domain ?? col.customDomain
    const codes = dom ? Object.keys(codeLabels[dom] ?? {}).length : 0
    if (codes > 0 && codes <= BADGE_MAX_CODES) return <Badge>{text}</Badge>
  }

  return <>{text}</>
}
