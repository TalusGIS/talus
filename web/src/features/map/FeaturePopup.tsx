import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

import AssetPopup from '@/features/map/AssetPopup'
import type { AttachmentStripProps } from '@/features/map/AttachmentStrip'
import type { LayerConfig } from '@/features/map/types'

/**
 * One resolved popup page. `openIdentifyPopup`'s `loadPage` builds this per identified item:
 * - `feature` — a vector feature: layer config + props + capability flags + actions (AssetPopup renders it).
 *   Read-only layers (views, reference polygons) come from the click's tile props; editable layers fetch.
 * - `raster` — the sampled band value(s) at the click (read-only readout).
 */
export type FeaturePage =
  | {
      kind: 'feature'
      config: LayerConfig<unknown>
      props: unknown
      /** Resolved title/rows (decision-map-popups.md): loadFeaturePage applies the map's per-layer popup
       * config (or the defaults — slots get notes+owner fields) so AssetPopup renders data, not policy. */
      title: string
      eyebrow?: string
      rows: Array<[string, string]>
      canEdit: boolean
      canMove: boolean
      canDelete: boolean
      onEdit: () => void
      onMove: () => void
      onDelete: () => void
      onEditShape?: () => void
      onDetails?: () => void
      /** A-4: the lazy attachments strip for this feature (absent on pages that can't carry attachments). */
      attachments?: AttachmentStripProps
      /** F11: title-row actions beside Details (the '@paid' Generate-sheet icon; absent on read-only pages). */
      titleActions?: ReactNode
    }
  | { kind: 'raster'; title: string; values: (number | null)[]; outOfBounds: boolean }

export interface FeaturePopupProps {
  /** Total identified features at the click. 1 → no pager (the header is just a grab strip). */
  count: number
  /** Resolve the page at `index` (fetches editable layers lazily); rejects with a user-facing message. */
  loadPage: (index: number) => Promise<FeaturePage>
  /** Fired whenever the active page changes (incl. first mount) — moves the map highlight to that feature. */
  onActive: (index: number) => void
  /** Drag-by-header (A-4 §3.5): openIdentifyPopup owns the popup instance, so it supplies the levers —
   * onStart (disconnect re-anchor, pin anchor) fires on the first move; onMove gets pointer deltas. */
  drag?: { onStart: () => void; onMove: (dx: number, dy: number) => void }
}

type PageState =
  | { status: 'loading' }
  | { status: 'ready'; page: FeaturePage }
  | { status: 'error'; message: string }

/**
 * The paged multi-layer identify popup (decision-multi-layer-popup.md). A click over overlapping layers
 * yields N features; this walks them one at a time with a ‹ n of N › pager, each page rendered by AssetPopup
 * (so every layer's curated rows + edit actions are preserved). Pages load lazily and are cached; the active
 * page is highlighted on the map via onActive. With a single feature it renders like the old popup — no pager.
 */
export default function FeaturePopup({ count, loadPage, onActive, drag }: FeaturePopupProps) {
  const [index, setIndex] = useState(0)
  const [state, setState] = useState<PageState>({ status: 'loading' })
  const cacheRef = useRef<Map<number, FeaturePage>>(new Map())
  const reqRef = useRef(0)
  // Active header drag: pointer id + last position; `started` defers onStart to the first real move.
  const dragRef = useRef<{ id: number; x: number; y: number; started: boolean } | null>(null)

  // Desktop-pointer-only (§3.5): touch keeps today's behavior. Pointerdown on the pager buttons must
  // not start a drag — the strip around them is the grab surface.
  function onHeaderPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if (!drag || (e.pointerType !== 'mouse' && e.pointerType !== 'pen')) return
    if ((e.target as HTMLElement).closest('button')) return
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { id: e.pointerId, x: e.clientX, y: e.clientY, started: false }
  }
  function onHeaderPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const d = dragRef.current
    if (!drag || !d || e.pointerId !== d.id) return
    if (!d.started) {
      d.started = true
      drag.onStart()
    }
    drag.onMove(e.clientX - d.x, e.clientY - d.y)
    d.x = e.clientX
    d.y = e.clientY
  }
  function onHeaderPointerEnd(e: React.PointerEvent<HTMLDivElement>) {
    if (dragRef.current?.id === e.pointerId) dragRef.current = null
  }

  // Load (or show cached) the active page and move the highlight. The component mounts once per popup into a
  // detached React root and is never re-rendered by a parent, so `index` is the only meaningful dependency;
  // loadPage/onActive are stable closures for the popup's life.
  useEffect(() => {
    onActive(index)
    const cached = cacheRef.current.get(index)
    if (cached) {
      setState({ status: 'ready', page: cached })
      return
    }
    const req = ++reqRef.current
    setState({ status: 'loading' })
    loadPage(index)
      .then((page) => {
        if (reqRef.current !== req) return // paged away before this resolved — drop the stale result
        cacheRef.current.set(index, page)
        setState({ status: 'ready', page })
      })
      .catch((err: unknown) => {
        if (reqRef.current !== req) return
        setState({ status: 'error', message: err instanceof Error ? err.message : 'Error loading feature' })
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index])

  return (
    <div className="feature-popup__paged">
      {/* Header bar (A-4 §3.5): always present — hosts the pager when count > 1, otherwise it's just
          the grab strip the popup drags by (desktop pointer only). */}
      <div
        className="feature-popup__header"
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={onHeaderPointerEnd}
        onPointerCancel={onHeaderPointerEnd}
      >
        {count > 1 && (
          <div className="feature-popup__pager">
            <button
              type="button"
              className="feature-popup__nav"
              disabled={index === 0}
              onClick={() => setIndex((i) => Math.max(0, i - 1))}
              aria-label="Previous feature"
            >
              <ChevronLeft size={14} aria-hidden="true" />
            </button>
            <span className="feature-popup__count">
              {index + 1} of {count}
            </span>
            <button
              type="button"
              className="feature-popup__nav"
              disabled={index === count - 1}
              onClick={() => setIndex((i) => Math.min(count - 1, i + 1))}
              aria-label="Next feature"
            >
              <ChevronRight size={14} aria-hidden="true" />
            </button>
          </div>
        )}
      </div>
      {state.status === 'loading' && <div className="sign-popup">Loading…</div>}
      {state.status === 'error' && <div className="sign-popup sign-popup--error">{state.message}</div>}
      {state.status === 'ready' && state.page.kind === 'feature' && (
        <AssetPopup
          config={state.page.config}
          props={state.page.props}
          title={state.page.title}
          eyebrow={state.page.eyebrow}
          rows={state.page.rows}
          canEdit={state.page.canEdit}
          canMove={state.page.canMove}
          canDelete={state.page.canDelete}
          onEdit={state.page.onEdit}
          onMove={state.page.onMove}
          onDelete={state.page.onDelete}
          onEditShape={state.page.onEditShape}
          onDetails={state.page.onDetails}
          attachments={state.page.attachments}
          titleActions={state.page.titleActions}
        />
      )}
      {state.status === 'ready' && state.page.kind === 'raster' && (
        <RasterReadout title={state.page.title} values={state.page.values} outOfBounds={state.page.outOfBounds} />
      )}
    </div>
  )
}

function formatValue(v: number | null): string {
  if (v === null) return '—'
  return Number.isInteger(v) ? String(v) : v.toLocaleString(undefined, { maximumFractionDigits: 4 })
}

/** Read-only readout of a raster's sampled band value(s) at the clicked point. */
function RasterReadout({
  title,
  values,
  outOfBounds,
}: {
  title: string
  values: (number | null)[]
  outOfBounds: boolean
}) {
  return (
    <div className="sign-popup">
      <div className="sign-popup__title">{title}</div>
      {outOfBounds || values.length === 0 ? (
        <div className="sign-popup__row">
          <span className="sign-popup__v">No data at this location</span>
        </div>
      ) : (
        values.map((v, i) => (
          <div className="sign-popup__row" key={`band-${i}`}>
            <span className="sign-popup__k">{values.length > 1 ? `Band ${i + 1}` : 'Value'}</span>
            <span className="sign-popup__v">{formatValue(v)}</span>
          </div>
        ))
      )}
    </div>
  )
}
