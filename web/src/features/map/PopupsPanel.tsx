/**
 * Per-layer popup config panel (decision-map-popups.md). Opened from the Pop-ups rail tool's layer
 * picker, it configures the STRUCTURAL v1 surface: popups on/off for the layer, the title field (whose
 * value titles the page + relabels Locate results), and the ordered visible-field checklist (▲/▼
 * reorder — array order IS display order). Apply persists with the map; "Reset to defaults" clears the
 * config entirely (built-ins return to their curated rows, slots to notes+owner fields, views to all).
 *
 * Field list = the same /layers/{id}/columns source Filter/Labels use (HIDDEN_FIELDS filtered). When
 * the layer is UNCONFIGURED the checklist seeds all-visible in column order — Apply then persists that
 * explicit list (predictable: what you see checked is what viewers get).
 */
import { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'

import { HIDDEN_FIELDS, fieldLabel } from '@/features/map/fields'
import { ApiError, fetchLayerColumns } from '@/lib/api'
import type { LayerColumn, WebMapPopupConfig } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

const MAX_FIELDS = 30 // mirrors PopupConfig bounds (web_maps.py _MAX_POPUP_FIELDS)

interface Row {
  name: string
  visible: boolean
}

export interface PopupsPanelProps {
  layerId: string
  layerLabel: string
  accessToken: string
  initial: WebMapPopupConfig | null // the layer's current config (null = defaults)
  onApply: (cfg: WebMapPopupConfig | null) => void // apply + persist-in-state; null resets to defaults
  onClose: () => void
  /** EXTERNAL layers (decision-add-from-url.md rail parity): the caller supplies the column
   *  schema from the remote service's metadata — no Talus /columns fetch. */
  columnsOverride?: LayerColumn[]
}

export default function PopupsPanel({
  layerId,
  layerLabel,
  accessToken,
  initial,
  onApply,
  onClose,
  columnsOverride,
}: PopupsPanelProps) {
  const [rows, setRows] = useState<Row[] | null>(null)
  const [columnsError, setColumnsError] = useState<string | null>(null)
  const [enabled, setEnabled] = useState(initial?.enabled !== false)
  const [titleField, setTitleField] = useState(initial?.titleField ?? '')
  const [error, setError] = useState<string | null>(null)

  // Load the layer's columns once, then seed the ordered checklist: a saved config's fields first (in
  // their saved order, visible), remaining columns after (hidden); no config → all visible, column order.
  useEffect(() => {
    let cancelled = false
    const seed = (cols: LayerColumn[]): void => {
      const names = cols.map((c) => c.name).filter((n) => !HIDDEN_FIELDS.has(n))
        const saved = (initial?.fields ?? null)?.map((f) => f.name).filter((n) => names.includes(n))
        if (saved) {
          setRows([
            ...saved.map((name) => ({ name, visible: true })),
            ...names.filter((n) => !saved.includes(n)).map((name) => ({ name, visible: false })),
          ])
        } else {
          setRows(names.map((name) => ({ name, visible: true })))
        }
    }
    if (columnsOverride) {
      seed(columnsOverride) // external layer — schema supplied by the caller (remote service metadata)
      return
    }
    fetchLayerColumns(layerId, accessToken)
      .then((res) => {
        if (!cancelled) seed(res.columns)
      })
      .catch((err) => {
        if (!cancelled) setColumnsError(err instanceof ApiError ? `HTTP ${err.status}` : 'request failed')
      })
    return () => {
      cancelled = true
    }
    // Seed once per layer open — `initial` is stable for the panel's life (AssetMap remounts on layer change).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layerId, accessToken, columnsOverride])

  function toggle(name: string): void {
    setRows((rs) => (rs ? rs.map((r) => (r.name === name ? { ...r, visible: !r.visible } : r)) : rs))
    setError(null)
  }

  // Bulk visibility: with a wide schema, "none, then check the 3 I want"
  // beats 50 individual unchecks. Order is preserved — only the checkboxes flip.
  function setAll(visible: boolean): void {
    setRows((rs) => (rs ? rs.map((r) => ({ ...r, visible })) : rs))
    setError(null)
  }

  function move(name: string, dir: -1 | 1): void {
    setRows((rs) => {
      if (!rs) return rs
      const i = rs.findIndex((r) => r.name === name)
      const j = i + dir
      if (i < 0 || j < 0 || j >= rs.length) return rs
      const next = [...rs]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }

  function apply(): void {
    if (!rows) return
    const visible = rows.filter((r) => r.visible)
    if (visible.length > MAX_FIELDS) {
      setError(`At most ${MAX_FIELDS} visible fields (${visible.length} checked)`)
      return
    }
    onApply({
      enabled,
      ...(titleField ? { titleField } : {}),
      fields: visible.map((r) => ({ name: r.name })),
    })
  }

  return (
    <div className="styles-panel popups-panel">
      <div className="styles-panel__header">
        <h3 className="styles-panel__title">Pop-ups · {layerLabel}</h3>
        <CloseButton onClick={onClose} />
      </div>

      {columnsError ? (
        <p className="sign-form__error">Could not load fields ({columnsError})</p>
      ) : !rows ? (
        <p className="styles-panel__muted">Loading fields…</p>
      ) : (
        <>
          <label className="popups-panel__enable">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            Show pop-ups for this layer
          </label>

          <div className="labels-panel__grid">
            <label htmlFor="popups-title">Title</label>
            <select
              id="popups-title"
              value={titleField}
              onChange={(e) => setTitleField(e.target.value)}
              disabled={!enabled}
            >
              <option value="">Layer name ({layerLabel})</option>
              {rows.map((r) => (
                <option key={r.name} value={r.name}>
                  {fieldLabel(r.name)}
                </option>
              ))}
            </select>
          </div>

          <div className="popups-panel__fieldsbar">
            <p className="styles-panel__muted">Fields shown, top to bottom:</p>
            <span className="popups-panel__bulk">
              <button type="button" className="btn btn--sm" disabled={!enabled} onClick={() => setAll(true)}>
                All
              </button>
              <button type="button" className="btn btn--sm" disabled={!enabled} onClick={() => setAll(false)}>
                None
              </button>
            </span>
          </div>
          <ul className="popups-panel__rows">
            {rows.map((r, i) => (
              <li className="popups-panel__row" key={r.name}>
                <label className="popups-panel__check">
                  <input
                    type="checkbox"
                    checked={r.visible}
                    disabled={!enabled}
                    onChange={() => toggle(r.name)}
                  />
                  <span className={r.visible ? '' : 'popups-panel__off'}>{fieldLabel(r.name)}</span>
                </label>
                <span className="popups-panel__ud">
                  <button
                    type="button"
                    aria-label={`Move ${fieldLabel(r.name)} up`}
                    disabled={!enabled || i === 0}
                    onClick={() => move(r.name, -1)}
                  >
                    <ChevronUp size={13} aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    aria-label={`Move ${fieldLabel(r.name)} down`}
                    disabled={!enabled || i === rows.length - 1}
                    onClick={() => move(r.name, 1)}
                  >
                    <ChevronDown size={13} aria-hidden="true" />
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {error && <p className="sign-form__error">{error}</p>}

      <div className="sign-form__actions">
        {initial && (
          <button type="button" className="btn btn--sm popups-panel__reset" onClick={() => onApply(null)}>
            Reset to defaults
          </button>
        )}
        <button type="button" className="btn btn--sm" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="btn btn--sm" onClick={apply} disabled={!rows}>
          Apply
        </button>
      </div>
    </div>
  )
}
