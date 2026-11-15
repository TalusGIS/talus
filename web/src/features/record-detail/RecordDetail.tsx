import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import { ApiError, fetchAttachmentBlob, fetchFeatureAttachments, fetchFeatureHistory, getCustomDomain } from '@/lib/api'
import type {
  FeatureAttachment,
  FeatureHistoryChange,
  FeatureHistoryEntry,
  GeometryType,
  LayerColumn,
  LayerFeature,
} from '@/lib/api'
import { HIDDEN_COLUMNS, cell, cellTitle, colLabel, columnKind, isBlank } from '@/features/catalog/format'
import PanelSection from '@/components/PanelSection'
import { Pencil } from 'lucide-react'
import { GeometryIcon } from '@/features/catalog/ItemTypeIcons'
import { RecordSheetAction } from '@paid'

import RelatedRecords from './RelatedRecords'
import RecordDrawer from './RecordDrawer'
import LocationStrip from './LocationStrip'

/**
 * Generic single-record detail drawer (Talus 16; generic attachments since V068). Fed the layer's column
 * schema + a feature's raw properties (the generic /layers/{id}/features shape), it renders every attribute
 * with labels resolved, plus a generic **Attachments** panel — image thumbnails (click to enlarge) and file
 * chips (click to download) for ANY layer, via /layers/{id}/features/{fid}/attachments. The panel is
 * view + download only; ADDING attachments lives in the feature edit form now
 * (decision-attachments-in-edit-form.md). One component, opened identically from the catalog Data table
 * and the map popup (via RecordDetailLoader).
 */
export interface RecordDetailProps {
  layerId: string
  layerLabel: string
  columns: LayerColumn[]
  recordId: string
  properties: Record<string, unknown>
  onClose: () => void
  /** When set, the header offers an Edit button (a non-spatial table's grid-edit path,
   *  decision-nonspatial-tables.md t4 — spatial layers keep the map as their edit surface). */
  onEdit?: () => void
  /** The feature's geometry from the same /features read (§3.2) — drives the location strip.
   *  Absent or null on a geometry-less hosted table, which renders no strip. */
  geometry?: LayerFeature['geometry']
  /** The layer's EFFECTIVE label field (V072, decision-layer-display-field.md) — the attribute whose
   *  VALUE names this record. /me/layers derives it server-side when the owner hasn't starred one, so
   *  the drawer title agrees with the popup title, Locate labels and the ESRI displayField instead of
   *  inventing a second rule. Null/absent ⇒ the title falls back to `{layerLabel} #{id}`. */
  displayField?: string | null
  /** Geometry family + symbology colour for the header mark, so the drawer, the TOC and the
   *  inspector agree on what this layer looks like. */
  geomType?: GeometryType | 'none' | null
  displayColor?: string | null
  /** Live-map PNG capture handed to the Talus+ "Generate sheet" action (the '@paid' seam; the
   *  public build's stub renders nothing). Absent on surfaces without a map. */
  captureMap?: () => Promise<Blob | null>
}

// Only these render as an <img> thumbnail; HEIC/TIFF/BMP + all docs show a file chip (no broken images).
const RENDERABLE = new Set(['image/jpeg', 'image/png', 'image/gif', 'image/webp'])


interface LoadedAttachment extends FeatureAttachment {
  thumbUrl: string | null // object URL for a renderable image; null for docs (downloaded on click)
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function extLabel(filename: string): string {
  const dot = filename.lastIndexOf('.')
  return dot >= 0 ? filename.slice(dot + 1, dot + 5).toUpperCase() : 'FILE'
}

export default function RecordDetail({
  layerId,
  layerLabel,
  columns,
  recordId,
  properties,
  onClose,
  onEdit,
  geometry,
  displayField,
  geomType,
  displayColor,
  captureMap,
}: RecordDetailProps) {
  const codeLabels = useCodeLabels(columns)
  const { items, loading, error, token } = useFeatureAttachments(layerId, recordId)
  const [enlarged, setEnlarged] = useState<string | null>(null)

  // Close on Escape (and the lightbox first, if open).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (enlarged) setEnlarged(null)
      else onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [enlarged, onClose])

  const attrCols = columns.filter((c) => !HIDDEN_COLUMNS.has(c.name))
  // The generic /features endpoint carries `id` on the feature, not in properties, so inject it back.
  const attrProps = useMemo(() => ({ ...properties, id: recordId }), [properties, recordId])

  // §3.2 — the header names the RECORD, not the layer. Every row of Signs used to be titled
  // "Signs detail". The label field is the layer's EFFECTIVE displayField (V072): the attribute the
  // owner starred, else the one the server derives — the SAME field the popup title and Locate
  // results use, so two surfaces never disagree about what a feature is called.
  const recordTitle = labelValue(displayField, attrProps) ?? `${layerLabel} #${recordId}`

  // §3.1 — History earned its high placement by answering "when was this last touched?" in one
  // click. `updated_at` answers it in zero, off data already in hand, so History goes last without
  // taking anything away.
  const lastEdited = relativeWhen(properties.updated_at ?? properties.created_at)

  return (
    <RecordDrawer
      ariaLabel={`${layerLabel} record detail`}
      swatch={
        geomType && geomType !== 'none' ? (
          <span className="record-detail__swatch" style={{ color: displayColor ?? undefined }}>
            <GeometryIcon geomType={geomType} className="record-detail__swatch-icon" />
          </span>
        ) : undefined
      }
      title={recordTitle}
      subtitle={
        <>
          {layerLabel}
          <span className="record-detail__id"> #{recordId}</span>
        </>
      }
      hint={lastEdited ? `last edited ${lastEdited}` : undefined}
      actions={
        <>
          {/* Talus+ reports: one feature's sheet, now (inert in a public build). */}
          <RecordSheetAction
            layerId={layerId}
            objectId={typeof properties.objectid === 'number' ? properties.objectid : null}
            captureMap={captureMap}
          />
          {onEdit && (
            <button
              type="button"
              className="record-detail__edit"
              onClick={onEdit}
              aria-label="Edit this row"
              title="Edit this row"
            >
              <Pencil size={15} aria-hidden="true" />
            </button>
          )}
        </>
      }
      onClose={onClose}
    >
      {/* Where the record IS (§3.2) — nothing for a geometry-less table. */}
      <LocationStrip geometry={geometry ?? null} />

      {/* Attributes are the record; they stay open and are not a collapsible section (§3.1). */}
      <dl className="record-detail__attrs">
        {attrCols.map((c) => (
          <div className="record-detail__row" key={c.name}>
            <dt className="record-detail__k">{colLabel(c)}</dt>
            <dd
              className="record-detail__v"
              data-kind={columnKind(c)}
              data-null={isBlank(c, attrProps) || undefined}
              title={cellTitle(c, attrProps)}
            >
              {cell(c, attrProps, codeLabels)}
            </dd>
          </div>
        ))}
      </dl>

      <AttachmentsPanel
        layerId={layerId}
        token={token}
        items={items}
        loading={loading}
        error={error}
        onEnlarge={setEnlarged}
      />

      {/* Related records (decision-relates-and-join-views.md PR A) — one section per relate on
          this layer; renders nothing when there are none (or none the caller may see). */}
      <RelatedRecords layerId={layerId} recordId={recordId} properties={properties} />

      {/* Edit history (decision-edit-history-ui.md) — who/when/what changed for this feature.
          Signed-in + can_view only; hidden for anon (public-map, no token) since history is
          identity-bearing. Lazy-loads on first expand; last, per §3.1. */}
      {token && (
        <HistorySection
          key={`${layerId}:${recordId}`}
          layerId={layerId}
          featureId={recordId}
          token={token}
          columns={columns}
          codeLabels={codeLabels}
        />
      )}

      {enlarged && (
        <div
          className="record-detail__lightbox"
          onClick={() => setEnlarged(null)}
          role="dialog"
          aria-label="Enlarged photo"
        >
          <img src={enlarged} alt="" className="record-detail__lightbox-img" />
        </div>
      )}
    </RecordDrawer>
  )
}

/** code -> label maps keyed by custom-domain id (cell()'s keying), fetched once per drawer for
 * every coded column. Restores the label resolution the retired built-in /domains fetch used to
 * provide, from the V046 custom-domain system — the only coded system left
 * (decision-retire-builtins.md phase 2; same gap-shape as the StylesPanel fix in PR #136).
 * Unlike FilterPanel's picker, this is a RENDERER: retired values are included, since stored data
 * legitimately carries them (the V046 trigger accepts any DEFINED code, active or retired).
 * Anon (public map) has no token and /custom-domains is authenticated, so anon degrades to raw
 * codes exactly as before; a failed fetch degrades the same way via cell()'s fallback. */
function useCodeLabels(columns: LayerColumn[]): Record<string, Record<string, string>> {
  const auth = useAuth()
  const domainToken = auth.user?.access_token
  const [labels, setLabels] = useState<Record<string, Record<string, string>>>({})
  useEffect(() => {
    const ids = [...new Set(columns.map((c) => c.customDomain).filter((d): d is string => !!d))]
    if (!ids.length || !domainToken) return
    let active = true
    Promise.all(
      ids.map((id) =>
        getCustomDomain(id, domainToken)
          .then((d) => {
            const m: Record<string, string> = {}
            for (const v of d.values) m[v.code] = v.label
            return [id, m] as const
          })
          .catch(() => [id, {} as Record<string, string>] as const),
      ),
    ).then((pairs) => {
      if (active) setLabels(Object.fromEntries(pairs))
    })
    return () => {
      active = false
    }
  }, [columns, domainToken])
  return labels
}

/** The record's own label: the displayField's value when there is one and it isn't empty. Returns
 * null (not a guess) otherwise — the caller falls back to `{layer} #{id}`. Reads the nested props
 * object too, since an owner-defined virtual column can be the starred field. */
function labelValue(field: string | null | undefined, props: Record<string, unknown>): string | null {
  if (!field) return null
  const raw =
    field in props ? props[field] : (props.props as Record<string, unknown> | null | undefined)?.[field]
  if (raw === null || raw === undefined) return null
  const text = String(raw).trim()
  return text === '' ? null : text
}

/** "3 days ago" from an ISO timestamp, for the header hint. Null for anything unparseable so the
 * hint simply doesn't render rather than showing "Invalid Date". */
function relativeWhen(at: unknown): string | null {
  if (typeof at !== 'string' || at === '') return null
  const then = new Date(at).getTime()
  if (Number.isNaN(then)) return null
  const secs = Math.round((then - Date.now()) / 1000)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ['year', 31536000],
    ['month', 2592000],
    ['day', 86400],
    ['hour', 3600],
    ['minute', 60],
  ]
  for (const [unit, size] of units) {
    if (Math.abs(secs) >= size) return rtf.format(Math.round(secs / size), unit)
  }
  return rtf.format(Math.round(secs), 'second')
}

// --- edit history (decision-edit-history-ui.md) --------------------------------

const HISTORY_PAGE = 20

/** The originating client → a plain-language "via". A service-account edit is labelled as such
 * regardless of its client; humans map the known clients (web / ArcGIS Pro), else the raw string. */
function viaLabel(via: string | null, actorType: string): string {
  if (actorType === 'service_account') return 'service account'
  switch (via) {
    case 'portal-api':
      return 'web'
    case 'geoservices-api':
      return 'ArcGIS Pro / Esri'
    case 'qfield-sync':
      return 'QField'
    default:
      return via ?? 'unknown source'
  }
}

function fmtWhen(at: string | null): string {
  if (!at) return 'unknown time'
  const d = new Date(at)
  if (Number.isNaN(d.getTime())) return at
  return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

/**
 * The record-detail "History" section — a collapsible, lazy-loaded timeline of a feature's edits
 * (who/when/what changed) merged with its attachment add/remove events. Rendered only for a signed-in
 * caller (anon is denied server-side and the parent hides the section without a token). The server
 * sends the RAW changed-field delta; this resolves field + coded-value labels from the same
 * columns/domains the attribute list uses, and summarizes a geometry change (never before/after maps).
 * Pages via the changed_at cursor.
 */
function HistorySection({
  layerId,
  featureId,
  token,
  columns,
  codeLabels,
}: {
  layerId: string
  featureId: string
  token: string
  columns: LayerColumn[]
  codeLabels: Record<string, Record<string, string>>
}) {
  const [entries, setEntries] = useState<FeatureHistoryEntry[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)

  const colByName = useMemo(() => {
    const m = new Map<string, LayerColumn>()
    for (const c of columns) m.set(c.name, c)
    return m
  }, [columns])

  const loadPage = useCallback(
    (next: string | null) => {
      setLoading(true)
      setError(null)
      fetchFeatureHistory(layerId, featureId, { limit: HISTORY_PAGE, cursor: next }, token)
        .then((r) => {
          setEntries((prev) => (next == null ? r.entries : [...prev, ...r.entries]))
          setCursor(r.next_cursor)
          setLoaded(true)
        })
        .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
        .finally(() => setLoading(false))
    },
    [layerId, featureId, token],
  )

  // The drawer is reused across records, so History must forget the last one. The parent keys this
  // component on `${layerId}:${recordId}` — a remount, which drops entries/cursor/loaded and
  // re-collapses PanelSection in one move (§3.6's "needs a key or a reset path").

  function fieldLabel(field: string): string {
    if (field === '__geometry__') return 'Location'
    const c = colByName.get(field)
    if (c) return colLabel(c)
    if (field === 'notes') return 'Notes'
    return field
  }

  // Reuse the attribute cell renderer (coded-domain + virtual-props aware) for a single value.
  function valueText(field: string, value: unknown): string {
    if (value === null || value === undefined || value === '') return '—'
    const c = colByName.get(field)
    if (c) {
      const p = c.props ? { props: { [field]: value } } : { [field]: value }
      return cell(c, p as Record<string, unknown>, codeLabels)
    }
    if (typeof value === 'object') return JSON.stringify(value)
    return String(value)
  }

  function changeText(ch: FeatureHistoryChange): string {
    if (ch.field === '__geometry__' || ch.kind === 'geometry') {
      return ch.moved_m != null
        ? `Location changed (~${Math.round(ch.moved_m)} m)`
        : 'Location changed'
    }
    return `${fieldLabel(ch.field)}: ${valueText(ch.field, ch.old)} → ${valueText(ch.field, ch.new)}`
  }

  return (
    <PanelSection title="History" lazy onFirstOpen={() => loadPage(null)}>
        <div className="record-detail__history">
          {loading && entries.length === 0 && (
            <div className="panel-section__note">Loading history…</div>
          )}
          {error && <div className="panel-section__note">Couldn’t load history ({error})</div>}
          {loaded && !error && entries.length === 0 && (
            <div className="panel-section__note">No recorded changes.</div>
          )}

          {entries.map((e) => (
            <div className="record-detail__history-entry" key={`${e.kind}-${e.audit_id}`}>
              <div className="record-detail__history-meta">
                {fmtWhen(e.at)} · {e.actor.name ?? 'Unknown'} · via {viaLabel(e.via, e.actor_type)}
              </div>
              <div className="record-detail__history-what">
                {e.kind === 'attachment' ? (
                  `${e.operation === 'DELETE' ? 'Removed' : 'Added'} attachment: ${
                    e.attachment?.filename ?? 'file'
                  }`
                ) : e.operation === 'INSERT' ? (
                  'Created'
                ) : e.operation === 'DELETE' ? (
                  'Deleted'
                ) : e.changes.length === 0 ? (
                  'Updated'
                ) : (
                  <ul className="record-detail__history-changes">
                    {e.changes.map((ch, i) => (
                      <li key={i}>{changeText(ch)}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ))}

          {cursor != null && (
            <button className="btn btn--sm" onClick={() => loadPage(cursor)} disabled={loading}>
              {loading ? 'Loading…' : 'Show more'}
            </button>
          )}
        </div>
    </PanelSection>
  )
}

function AttachmentsPanel({
  layerId,
  token,
  items,
  loading,
  error,
  onEnlarge,
}: {
  layerId: string
  token: string | undefined
  items: LoadedAttachment[]
  loading: boolean
  error: string | null
  onEnlarge: (url: string) => void
}) {
  // View + download only. Adding attachments now lives in the feature EDIT form
  // (decision-attachments-in-edit-form.md); the read drawer no longer uploads or deletes.
  const [opError, setOpError] = useState<string | null>(null)

  // Nothing to show → render nothing (no empty box on a layer with no attachments).
  if (!loading && !error && items.length === 0) return null

  async function onDownload(a: LoadedAttachment) {
    if (!token) return
    try {
      const blob = await fetchAttachmentBlob(layerId, a.id, token)
      const url = URL.createObjectURL(blob)
      const el = document.createElement('a')
      el.href = url
      el.download = a.filename
      document.body.appendChild(el)
      el.click()
      el.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setOpError(err instanceof ApiError ? err.message : String(err))
    }
  }

  return (
    <PanelSection title="Attachments" count={items.length || undefined} defaultOpen>
      {loading && <div className="panel-section__note">Loading attachments…</div>}
      {error && <div className="panel-section__note">Couldn’t load attachments ({error})</div>}
      {opError && <p className="modal__field-error">{opError}</p>}

      {items.length > 0 && (
        <ul className="panel-section__list">
          {items.map((a) => (
            <li className="panel-section__row" key={a.id}>
              {a.thumbUrl ? (
                <button
                  className="record-detail__attach-thumb"
                  onClick={() => a.thumbUrl && onEnlarge(a.thumbUrl)}
                  title="Enlarge"
                >
                  <img src={a.thumbUrl} alt="" />
                </button>
              ) : (
                <button
                  className="record-detail__attach-thumb record-detail__attach-thumb--file"
                  onClick={() => void onDownload(a)}
                  title="Download"
                >
                  {extLabel(a.filename)}
                </button>
              )}
              <div className="panel-section__info">
                <button
                  className="record-detail__attach-name"
                  onClick={() => void onDownload(a)}
                  title="Download"
                >
                  {a.filename}
                </button>
                <span className="panel-section__meta">
                  {fmtBytes(a.byte_size)}
                  {a.created_by_name ? ` · ${a.created_by_name}` : ''}
                  {a.created_at ? ` · ${new Date(a.created_at).toLocaleDateString()}` : ''}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </PanelSection>
  )
}

/**
 * Fetch a feature's attachments and turn each renderable image into an object URL for its thumbnail (a
 * bearer-gated <img src> isn't possible, so the bytes are fetched-to-blob). Docs are downloaded on click,
 * not prefetched. `reload()` re-runs after an upload/delete. All object URLs are revoked on cleanup /
 * record change / reload to avoid leaks.
 */
function useFeatureAttachments(
  layerId: string,
  featureId: string | null,
): {
  items: LoadedAttachment[]
  loading: boolean
  error: string | null
  reload: () => void
  token: string | undefined
} {
  const auth = useAuth()
  const token = auth.user?.access_token
  const [items, setItems] = useState<LoadedAttachment[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const reload = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    setItems([])
    setError(null)
    if (!featureId || !token) return

    const ctrl = new AbortController()
    const urls: string[] = []
    let active = true
    setLoading(true)

    fetchFeatureAttachments(layerId, featureId, token, ctrl.signal)
      .then(async (res) => {
        const loaded = await Promise.all(
          res.attachments.map(async (a) => {
            let thumbUrl: string | null = null
            if (RENDERABLE.has(a.content_type)) {
              try {
                const blob = await fetchAttachmentBlob(layerId, a.id, token, ctrl.signal)
                thumbUrl = URL.createObjectURL(blob)
                urls.push(thumbUrl)
              } catch {
                thumbUrl = null // a missing/failed blob falls back to the file chip
              }
            }
            return { ...a, thumbUrl }
          }),
        )
        if (!active) {
          urls.forEach(URL.revokeObjectURL)
          return
        }
        setItems(loaded)
      })
      .catch((e) => {
        if (e?.name === 'AbortError' || !active) return
        setError(e instanceof ApiError ? e.message : String(e))
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => {
      active = false
      ctrl.abort()
      urls.forEach(URL.revokeObjectURL)
    }
  }, [layerId, featureId, token, nonce])

  return { items, loading, error, reload, token }
}
