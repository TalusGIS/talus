/**
 * In-map feature search — the "Locate" tool (decision-feature-search.md). A standalone control in the
 * LEFT data-tools rail (a magnifier icon sized to match +/Save/Edit), SEPARATE from the address box
 * (GeocodeSearch, a location PIN in the bottom-left): that finds a PLACE on the basemap, this finds your
 * DATA (features by attribute) and zooms to it. One box, type a query, it searches the attributes of the
 * map's VISIBLE layers with ZERO per-layer config (the ArcGIS-Pro-enhanced-search feel).
 *
 * Pure UI, mirroring GeocodeSearch's request lifecycle (debounce; abort the in-flight request on each
 * keystroke so a slow response can't land after a newer one). The magnifier button lives IN the left rail;
 * clicking it opens a flyout (search box + grouped results) anchored to the button. AssetMap owns the map
 * (fly / fit-bounds + popup on pick). Scope = the layers passed in (the map's visible layers); server-side
 * each is view-gated and fail-closed, so non-viewable/non-slot layers simply yield nothing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Search } from 'lucide-react'

import { GeometryIcon } from '@/features/catalog/ItemTypeIcons'
import { ApiError, searchFeatures } from '@/lib/api'
import type { FeatureSearchResult, GeometryType } from '@/lib/api'
import CloseButton from '@/components/CloseButton'


const DEBOUNCE_MS = 300
// Feature queries can be short (an ID, a code), but fire only from 2 chars to keep it quiet on 1.
const MIN_CHARS = 2
// Display tier order within a layer group (mirrors the server's ranking tiers): exact > wildcard > fuzzy.
const TIER_ORDER: Record<string, number> = { exact: 0, partial: 1, similar: 2 }

// A result's display label: the layer's title field (e.g. a Sign's mutcd_code → "D10-1"), else the server's
// best-effort fallback, else a short id. Keeps a hit recognizable instead of showing an arbitrary field.
function pickLabel(r: FeatureSearchResult, layer: LocateLayer | undefined): string {
  const tf = layer?.titleField
  if (tf && r.properties) {
    const v = r.properties[tf]
    if (v != null && String(v).trim()) return String(v).trim()
  }
  return r.label || `#${r.id.slice(0, 8)}`
}

export interface LocateLayer {
  id: string
  label: string
  geometryType: GeometryType
  // The layer's title attribute (LayerDef.popupTitleField) — used to label each result recognizably
  // (e.g. a Sign's `mutcd_code` → "D10-1") instead of whatever the server's fallback picked.
  titleField?: string | null
  // EXTERNAL layer (decision-add-from-url.md rail parity): excluded from the GET /search ids (the
  // server knows nothing about it) — searched via `searchExternal` instead, results merged.
  external?: boolean
}

export interface LocatePanelProps {
  accessToken: string | undefined
  // The map's currently-visible layers (the search scope). Their ids are sent to GET /search; the
  // server view-gates and shape-gates every id, so passing any layer kind is harmless.
  layers: LocateLayer[]
  onPick: (result: FeatureSearchResult) => void
  onClear?: () => void
  /** Searches the map's external layers (remote services / cached documents) for the same text —
   *  runs alongside GET /search; per-layer failures inside are silent. */
  searchExternal?: (text: string) => Promise<FeatureSearchResult[]>
}

export default function LocatePanel({ accessToken, layers, onPick, onClear, searchExternal }: LocatePanelProps) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<FeatureSearchResult[]>([])
  const [capped, setCapped] = useState<string[]>([]) // layer ids whose results hit the per-layer cap
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searched, setSearched] = useState(false) // true once a query has returned (drives "No matches")
  const [open, setOpen] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  // layer_id -> label/geometry for the result group headers + row icons.
  const layerMeta = useMemo(() => {
    const m = new Map<string, LocateLayer>()
    for (const l of layers) m.set(l.id, l)
    return m
  }, [layers])
  // Server-searchable ids only — external ids go to `searchExternal` (the server would reject them).
  const layerIds = useMemo(() => layers.filter((l) => !l.external).map((l) => l.id), [layers])
  const hasExternals = layers.some((l) => l.external)
  // stable dep for the search effect (avoids re-firing when the array identity changes but ids don't)
  const layerIdsKey = layerIds.join(',') + (hasExternals ? '+ext' : '')

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  // Debounced search; the previous in-flight request is aborted on each keystroke (last-write-wins).
  useEffect(() => {
    const text = q.trim()
    if (!accessToken || text.length < MIN_CHARS || (layerIds.length === 0 && !(hasExternals && searchExternal))) {
      abortRef.current?.abort()
      setResults([])
      setCapped([])
      setSearched(false)
      setError(null)
      return
    }
    const timer = window.setTimeout(() => {
      abortRef.current?.abort()
      const ctrl = new AbortController()
      abortRef.current = ctrl
      setBusy(true)
      setError(null)
      // Talus layers via GET /search; external layers via the caller's remote search — merged, Talus
      // first (its ranking is real; external hits are best-effort). Either half failing alone still
      // shows the other's results.
      const serverP = layerIds.length
        ? searchFeatures(text, layerIds, accessToken, ctrl.signal)
        : Promise.resolve({ results: [] as FeatureSearchResult[], capped: [] as string[] })
      const externalP = hasExternals && searchExternal ? searchExternal(text).catch(() => []) : Promise.resolve([])
      Promise.all([serverP, externalP])
        .then(([resp, ext]) => {
          if (ctrl.signal.aborted) return // superseded — a newer keystroke owns the panel now
          setResults([...resp.results, ...ext])
          setCapped(resp.capped ?? [])
          setSearched(true)
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return // superseded
          setResults([])
          setError(err instanceof ApiError ? `Search failed (HTTP ${err.status})` : 'Search failed')
        })
        .finally(() => setBusy(false))
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
    // layerIdsKey stands in for layerIds (stable-by-value); accessToken/q drive re-search.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, accessToken, layerIdsKey])

  const groups = useMemo(() => {
    const byLayer = new Map<string, FeatureSearchResult[]>()
    for (const r of results) {
      const arr = byLayer.get(r.layer_id)
      if (arr) arr.push(r)
      else byLayer.set(r.layer_id, [r])
    }
    // Within a Talus layer, present by tier (exact > partial > similar), then score (rank) desc, then display
    // label — so equal-score rows (a typo matching many features with the same code) CLUSTER by label instead
    // of interleaving, and the strongest matches stay on top. External layers keep their own order (their rank
    // is a positional index, not a score).
    for (const [lid, hits] of byLayer) {
      const meta = layerMeta.get(lid)
      if (meta?.external) continue
      hits.sort((a, b) => {
        const t = (TIER_ORDER[a.match_kind] ?? 3) - (TIER_ORDER[b.match_kind] ?? 3)
        if (t !== 0) return t
        const s = (b.rank ?? 0) - (a.rank ?? 0)
        if (Math.abs(s) > 1e-9) return s
        return pickLabel(a, meta).localeCompare(pickLabel(b, meta))
      })
    }
    return [...byLayer.entries()]
  }, [results, layerMeta])

  const clear = useCallback((): void => {
    abortRef.current?.abort()
    setQ('')
    setResults([])
    setCapped([])
    setSearched(false)
    setError(null)
    onClear?.()
  }, [onClear])

  const collapse = (): void => {
    clear()
    setOpen(false)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'Escape') collapse()
  }

  const noLayers = layers.length === 0

  return (
    <div className="locate-panel">
      <button
        type="button"
        className={`map__iconbtn${open ? ' is-active' : ''}`}
        aria-label="Find features in your layers"
        aria-pressed={open}
        title="Find features in your layers"
        onClick={() => (open ? collapse() : setOpen(true))}
      >
        <Search size={16} aria-hidden="true" />
      </button>

      {open && (
        <div className="locate-panel__flyout">
          <div className="locate-panel__bar">
            <span className="locate-panel__icon" aria-hidden="true">
              <Search size={16} aria-hidden="true" />
            </span>
            <input
              ref={inputRef}
              className="locate-panel__input"
              type="text"
              value={q}
              placeholder={noLayers ? 'Add a layer to search' : 'Find features by attribute'}
              aria-label="Find features by attribute"
              autoComplete="off"
              disabled={noLayers}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={onKeyDown}
            />
            {busy && <span className="locate-panel__spinner" aria-hidden="true" />}
            <CloseButton onClick={collapse} label="Close feature search" className="locate-panel__clear" />
          </div>

          {!noLayers && (
            <>
              <div className="locate-panel__scope" aria-hidden="true">
                Searching {layers.length} visible layer{layers.length === 1 ? '' : 's'}
              </div>
              <div className="locate-panel__hint" aria-hidden="true">
                <code>*</code> / <code>?</code> wildcards · typos show <em>similar</em> matches
              </div>
            </>
          )}

          {(groups.length > 0 || (searched && !busy) || error !== null) && (
            <div className="locate-panel__results" role="listbox">
              {error !== null && (
                <div className="locate-panel__msg locate-panel__msg--error">{error}</div>
              )}
              {error === null && searched && !busy && results.length === 0 && (
                <div className="locate-panel__msg">No matches</div>
              )}
              {groups.map(([layerId, hits]) => {
                const meta = layerMeta.get(layerId)
                const isCapped = capped.includes(layerId)
                return (
                  <div key={layerId} className="locate-panel__group">
                    <div className="locate-panel__group-head">
                      <GeometryIcon geomType={meta?.geometryType} className="locate-panel__geom" />
                      <span className="locate-panel__group-name">{meta?.label ?? layerId}</span>
                      <span className="locate-panel__group-count">
                        {hits.length}
                        {isCapped ? '+' : ''}
                      </span>
                    </div>
                    {hits.map((r) => {
                      const text = pickLabel(r, meta)
                      const badge = r.match_kind === 'partial' ? 'partial' : r.match_kind === 'similar' ? 'similar' : null
                      // Confidence % only for fuzzy hits — that's where `rank` is a word_similarity (0–1). Exact
                      // hits are exact; wildcard hits are boolean, so a number there would mislead.
                      const score = r.match_kind === 'similar' ? Math.round((r.rank ?? 0) * 100) : null
                      return (
                        <button
                          key={`${r.layer_id}:${r.id}`}
                          type="button"
                          role="option"
                          aria-selected={false}
                          className="locate-panel__opt"
                          onClick={() => onPick(r)}
                          title={score != null ? `${text} (${score}% similar)` : badge ? `${text} (${badge} match)` : text}
                        >
                          <span className="locate-panel__opt-label">{text}</span>
                          {badge && (
                            <span className={`locate-panel__badge locate-panel__badge--${r.match_kind}`}>{badge}</span>
                          )}
                          {score != null && <span className="locate-panel__score">{score}%</span>}
                        </button>
                      )
                    })}
                    {isCapped && (
                      <div className="locate-panel__more">
                        Showing first {hits.length} — refine your search to narrow.
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
