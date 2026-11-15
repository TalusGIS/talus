import { useEffect, useState } from 'react'
import type { ChangeEvent, FormEvent, ReactNode } from 'react'

import { transformPoint } from '@/lib/api'
import type { CustomField, DomainValue, SlotProperties, SlotUpdate } from '@/lib/api'
import AttachmentStage from '@/features/map/AttachmentStage'
import {
  applyFormRules,
  assembleProps,
  blocksSave,
  codedState,
  fieldError,
  loadFormFields,
  loadFormFieldsError,
  seedValues,
  type Val,
} from '@/features/map/slotFormModel'
import type { FormConfig } from '@/lib/api'
import type { AssetFormProps } from '@/features/map/types'

/**
 * Feature form for a publisher slot (assets.tal_<8hex>). Notes + the layer's owner-defined custom
 * fields: the active fields are fetched from /layers/{id}/custom-fields and rendered as typed inputs
 * (text / number / date / true-false / **picklist**), seeded from the feature's existing `props`
 * (edit) or each field's default (create). A `coded` field renders a `<select>` of its bound domain's
 * active values (label shown, code stored, slice 2); one with a CONTINGENCY (V077) re-filters those
 * options live by its parent field's current value — an existing value the mapping now forbids stays
 * selectable but is flagged, never silently cleared (the server rejects the pair only if either side
 * actually changes). On save the values are assembled into a `props` object alongside notes; the server
 * validates (app-side + the V044/V046/V077 trigger). The `domains` prop (legacy sign picklists) is
 * unused here. Reuses the sign-form chrome classes.
 */

// The form MODEL (seed / coerce / constraints / contingency / assembly) lives in slotFormModel.ts,
// shared with the Talus+ field app (decision-talus-field-f1.md §4.3.4). This file is the chrome.

/** A form section (f2 groups): titled groups fold as <details>; the untitled single group is a plain
 *  fragment so a layer with no document renders exactly as before. */
function FormGroup({ title, collapsed, children }: { title: string | null; collapsed: boolean; children: ReactNode }) {
  if (title === null) return <>{children}</>
  return (
    <details className="sign-form__group" open={!collapsed}>
      <summary className="sign-form__group-title">{title}</summary>
      {children}
    </details>
  )
}

export default function SlotForm({
  mode,
  initial,
  busy,
  error,
  onSubmit,
  onCancel,
  extraAction,
  layerId,
  accessToken,
  coord,
  noun,
  editViaAdmin,
  attachmentsEnabled = false,
}: AssetFormProps<SlotProperties, SlotUpdate>) {
  const featureNoun = noun ?? 'feature' // line/polygon slice c6a: title reflects the geometry, not always "point"
  const [staged, setStaged] = useState<File[]>([])
  const [notes, setNotes] = useState(initial?.notes ?? '')
  // E3 coordinate entry (decision-coord-entry.md): on CREATE, editable fields in the layer's working CRS.
  // Seeded from the click — a no-op for WGS-84, else reprojected 4326→srid so the projected pair pre-fills.
  const projected = !!coord && coord.srid !== 4326
  const [cx, setCx] = useState('')
  const [cy, setCy] = useState('')
  const [coordReady, setCoordReady] = useState(false)
  useEffect(() => {
    if (!coord) return
    let cancelled = false
    if (!projected) {
      setCx(String(coord.lon))
      setCy(String(coord.lat))
      setCoordReady(true)
      return
    }
    if (!accessToken) return
    transformPoint(coord.lon, coord.lat, coord.srid, accessToken)
      .then((t) => {
        if (cancelled) return
        setCx(String(Number(t.x.toFixed(3))))
        setCy(String(Number(t.y.toFixed(3))))
        setCoordReady(true)
      })
      .catch(() => {
        if (!cancelled) setCoordReady(true) // fall back to empty fields; the user can type
      })
    return () => {
      cancelled = true
    }
    // coord is captured once on open (form remounts per open).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const [fields, setFields] = useState<CustomField[]>([])
  const [values, setValues] = useState<Record<string, Val>>({})
  const [domainValues, setDomainValues] = useState<Record<string, DomainValue[]>>({}) // domainId -> active values
  const [formConfig, setFormConfig] = useState<FormConfig | null>(null) // the layer's form document (f2)
  const [fieldsError, setFieldsError] = useState<string | null>(null)

  useEffect(() => {
    if (!layerId || !accessToken) return
    let cancelled = false
    loadFormFields(layerId, accessToken)
      .then((r) => {
        if (cancelled) return
        setFields(r.fields)
        setValues(seedValues(r.fields, initial?.props))
        setDomainValues(r.domainValues)
        setFormConfig(r.formConfig)
      })
      .catch((e) => setFieldsError(loadFormFieldsError(e)))
    return () => {
      cancelled = true
    }
    // initial is captured once on open (the form is remounted per open); layer/token are stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layerId, accessToken])

  // E3: on create, the coordinate fields must hold valid numbers (and the seed transform must have settled).
  const coordInvalid =
    !!coord && (!coordReady || cx.trim() === '' || cy.trim() === '' || Number.isNaN(Number(cx)) || Number.isNaN(Number(cy)))
  // The form document (f2): groups, hidden / conditionally-required / read-only fields — evaluated once here,
  // exactly as the field app evaluates it (the same model), and mirrored by the V104 trigger for requiredIf.
  const layout = applyFormRules(formConfig, fields, values)
  // Required-empty / constraint violations block save (the server also enforces both) — the model's rule.
  const blockSave = blocksSave(fields, values, layout) || coordInvalid

  function setVal(key: string, v: Val): void {
    setValues((prev) => ({ ...prev, [key]: v }))
  }

  async function submit(e: FormEvent): Promise<void> {
    e.preventDefault()
    if (blockSave) return
    const props = assembleProps(fields, values, layout.hidden)
    const base: SlotUpdate = { notes: notes === '' ? null : notes, props: fields.length ? props : undefined }
    // E3: on create, emit backend-ready geometry in the working CRS. Native → {x,y} EXACT (typed values go
    // straight through, never round-tripped). Default-projected → reproject the typed working coords → 4326.
    // Plain WGS-84 → {lon,lat}. On edit (no coord), geometry is untouched (move is a separate drag).
    if (coord) {
      const x = Number(cx)
      const y = Number(cy)
      let geom: SlotUpdate
      if (coord.srid === 4326) geom = { lon: x, lat: y }
      else if (coord.native) geom = { x, y }
      else {
        const t = accessToken
          ? await transformPoint(x, y, 4326, accessToken, coord.srid)
          : { x, y }
        geom = { lon: t.x, lat: t.y }
      }
      onSubmit({ ...base, ...geom }, staged)
    } else {
      onSubmit(base, staged)
    }
  }

  return (
    <form className="sign-form" onSubmit={(e) => void submit(e)}>
      {editViaAdmin && (
        <div className="info-banner info-banner--info" role="status">
          <span className="info-banner__text">
            Editing is restricted but you have admin privileges to edit this layer.
          </span>
        </div>
      )}
      <h2 className="sign-form__title">{mode === 'create' ? `New ${featureNoun}` : `Edit ${featureNoun}`}</h2>

      {coord && (
        <fieldset className="sign-form__field" style={{ border: 'none', padding: 0, margin: 0 }}>
          <span>
            {projected ? 'Coordinates' : 'Location'}
            {projected && (
              <span className="modal__muted">
                {' '}
                · EPSG:{coord.srid}
                {coord.native ? ' (native)' : ''}
              </span>
            )}
          </span>
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <label style={{ flex: 1 }}>
              <span className="modal__muted" style={{ fontSize: '0.78em' }}>{projected ? 'X (easting)' : 'Longitude'}</span>
              <input
                type="text"
                inputMode="decimal"
                value={cx}
                onChange={(e) => setCx(e.target.value)}
                placeholder={coordReady ? '' : '…'}
              />
            </label>
            <label style={{ flex: 1 }}>
              <span className="modal__muted" style={{ fontSize: '0.78em' }}>{projected ? 'Y (northing)' : 'Latitude'}</span>
              <input
                type="text"
                inputMode="decimal"
                value={cy}
                onChange={(e) => setCy(e.target.value)}
                placeholder={coordReady ? '' : '…'}
              />
            </label>
          </div>
          {projected && !coord.native && (
            <span className="modal__muted" style={{ fontSize: '0.75em' }}>
              Stored as WGS 84 (reprojected). For exact survey coordinates, use a native-storage layer.
            </span>
          )}
        </fieldset>
      )}

      <label className="sign-form__field">
        <span>Notes</span>
        <textarea
          value={notes}
          onChange={(e: ChangeEvent<HTMLTextAreaElement>) => setNotes(e.target.value)}
        />
      </label>

      {layout.groups.map((g, gi) => (
        <FormGroup key={g.title ?? `g${gi}`} title={g.title} collapsed={g.collapsed}>
      {g.fields.filter((f) => !layout.hidden.has(f.fieldKey)).map((f) => {
        const cur = (values[f.fieldKey] as string) ?? ''
        const ro = layout.readOnly.has(f.fieldKey)
        const req = f.required || layout.conditionallyRequired.has(f.fieldKey)
        const allOpts = f.dataType === 'coded' ? (domainValues[f.domainId ?? ''] ?? []) : []
        // Contingency (V077) + retired/forbidden current values: the model's rules (slotFormModel.ts).
        const { opts, pairInvalid, showRetired, parentField, parentVal } = codedState(f, values, domainValues, fields)
        const err = fieldError(f, values[f.fieldKey] ?? '')
        return (
          <label key={f.id} className="sign-form__field">
            <span>
              {f.label}
              {req ? ' *' : ''}
            </span>
            {f.dataType === 'boolean' ? (
              <input
                type="checkbox"
                checked={values[f.fieldKey] === true}
                disabled={ro}
                onChange={(e) => setVal(f.fieldKey, e.target.checked)}
              />
            ) : f.dataType === 'coded' ? (
              <select value={cur} disabled={ro} onChange={(e) => setVal(f.fieldKey, e.target.value)}>
                {!f.required && <option value="">— none —</option>}
                {opts.map((v) => (
                  <option key={v.code} value={v.code}>
                    {v.label}
                  </option>
                ))}
                {pairInvalid && !showRetired && (
                  <option value={cur}>{allOpts.find((v) => v.code === cur)?.label ?? cur}</option>
                )}
                {showRetired && <option value={cur}>{cur} (retired)</option>}
              </select>
            ) : (
              <input
                type={f.dataType === 'number' ? 'number' : f.dataType === 'date' ? 'date' : 'text'}
                value={cur}
                readOnly={ro}
                onChange={(e) => setVal(f.fieldKey, e.target.value)}
              />
            )}
            {pairInvalid && (
              <span className="modal__field-error">
                Not valid when {parentField?.label ?? f.parentFieldKey} is “{parentVal}” — pick another value
                or change {parentField?.label ?? f.parentFieldKey}
              </span>
            )}
            {err !== null && <span className="modal__field-error">{err}</span>}
          </label>
        )
      })}
        </FormGroup>
      ))}

      {attachmentsEnabled && <AttachmentStage staged={staged} onChange={setStaged} disabled={busy} />}

      {fieldsError !== null && <p className="sign-form__error">{fieldsError}</p>}
      {error !== null && <p className="sign-form__error">{error}</p>}

      <div className="sign-form__actions">
        {/* A row-scoped destructive action (Delete row) belongs IN the action row, not in a second
            actions div tacked on below it (handoff-record-table-3 §4.5). It leads, away from Save. */}
        {extraAction}
        <button type="button" className="btn btn--sm" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="submit" className="btn btn--sm" disabled={busy || blockSave}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  )
}
