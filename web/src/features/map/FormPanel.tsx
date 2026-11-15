/**
 * Per-layer FORM panel (decision-talus-field-f2.md §4.6) — the Inspector's fifth tab. Authors the
 * layer's form document (`PUT /layers/{id}/form`): sections, per-field rules, feature templates and
 * the photo prompt. Manage-gated like Style (the document saves to the LAYER, not the map).
 *
 * The vocabulary is CLOSED and every value is picked, never typed as an expression: a rule is one
 * sibling field + one operator + values chosen from that field's domain (or true/false, or a plain
 * text). The server re-validates the whole document against the layer (400 names each problem).
 *
 * "Preview" renders the DESK form (SlotForm, create mode) in a phone-width frame. SlotForm reads the
 * document from the server, so the preview shows the SAVED form — labelled as such — never the draft.
 */
import { useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronUp, Plus, Trash2, X } from 'lucide-react'

import CloseButton from '@/components/CloseButton'
import SlotForm from '@/features/map/SlotForm'
import { loadFormFields, loadFormFieldsError } from '@/features/map/slotFormModel'
import { ApiError, clearLayerForm, putLayerForm } from '@/lib/api'
import type { CustomField, DomainValue, FormCondition, FormConfig, FormTemplate } from '@/lib/api'

type Op = 'eq' | 'in' | 'isEmpty' | 'notEmpty'

interface RuleDraft {
  field: string
  op: Op
  values: string[] // eq → [v]; in → [v…]; boolean eq → ['true' | 'false']
}

interface FieldDraft {
  visibleIf: RuleDraft | null
  requiredIf: RuleDraft | null
  readOnly: boolean
}

interface GroupDraft {
  title: string
  fields: string[]
  collapsed: boolean
}

interface TemplateDraft {
  id: string
  name: string
  defaults: Record<string, string> // form-input strings; coerced on save
}

export interface FormPanelProps {
  layerId: string
  layerLabel: string
  noun: string
  accessToken: string
  initial: FormConfig | null
  attachmentsEnabled: boolean
  onSaved: (cfg: FormConfig | null) => void
  onClose: () => void
}

const CONDITION_TYPES = new Set(['coded', 'boolean', 'text'])

function toRuleDraft(c: FormCondition | undefined): RuleDraft | null {
  if (!c) return null
  if ('eq' in c) return { field: c.field, op: 'eq', values: [String(c.eq)] }
  if ('in' in c) return { field: c.field, op: 'in', values: [...c.in] }
  if ('isEmpty' in c) return { field: c.field, op: 'isEmpty', values: [] }
  return { field: c.field, op: 'notEmpty', values: [] }
}

function fromRuleDraft(r: RuleDraft | null, fields: CustomField[]): FormCondition | undefined {
  if (!r || !r.field) return undefined
  const f = fields.find((x) => x.fieldKey === r.field)
  if (r.op === 'isEmpty') return { field: r.field, isEmpty: true }
  if (r.op === 'notEmpty') return { field: r.field, notEmpty: true }
  if (r.op === 'in') return { field: r.field, in: r.values.filter((v) => v !== '') }
  const v = r.values[0] ?? ''
  if (f?.dataType === 'boolean') return { field: r.field, eq: v === 'true' }
  return { field: r.field, eq: v }
}

function slug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'template'
}

export default function FormPanel({
  layerId,
  layerLabel,
  noun,
  accessToken,
  initial,
  attachmentsEnabled,
  onSaved,
  onClose,
}: FormPanelProps) {
  const [fields, setFields] = useState<CustomField[] | null>(null)
  const [domainValues, setDomainValues] = useState<Record<string, DomainValue[]>>({})
  const [loadError, setLoadError] = useState<string | null>(null)
  const [groups, setGroups] = useState<GroupDraft[]>([])
  const [rules, setRules] = useState<Record<string, FieldDraft>>({})
  const [templates, setTemplates] = useState<TemplateDraft[]>([])
  const [photoPrompt, setPhotoPrompt] = useState(false)
  const [openField, setOpenField] = useState<string | null>(null)
  const [openTemplate, setOpenTemplate] = useState<string | null>(null)
  const [preview, setPreview] = useState(false)
  const [saving, setSaving] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const [savedFlash, setSavedFlash] = useState(false)

  // Seed from the layer's fields + the current document.
  useEffect(() => {
    let cancelled = false
    loadFormFields(layerId, accessToken)
      .then((r) => {
        if (cancelled) return
        setFields(r.fields)
        setDomainValues(r.domainValues)
        const cfg = initial ?? r.formConfig
        setGroups((cfg?.groups ?? []).map((g) => ({ title: g.title, fields: [...g.fields], collapsed: !!g.collapsed })))
        const rd: Record<string, FieldDraft> = {}
        for (const [k, v] of Object.entries(cfg?.fields ?? {})) {
          rd[k] = { visibleIf: toRuleDraft(v.visibleIf), requiredIf: toRuleDraft(v.requiredIf), readOnly: !!v.readOnly }
        }
        setRules(rd)
        setTemplates(
          (cfg?.templates ?? []).map((t) => ({
            id: t.id,
            name: t.name,
            defaults: Object.fromEntries(Object.entries(t.defaults).map(([k, v]) => [k, v === null || v === undefined ? '' : String(v)])),
          })),
        )
        setPhotoPrompt(!!cfg?.photo?.prompt)
      })
      .catch((err) => {
        if (!cancelled) setLoadError(loadFormFieldsError(err))
      })
    return () => {
      cancelled = true
    }
  }, [layerId, accessToken, initial])

  const byKey = useMemo(() => new Map((fields ?? []).map((f) => [f.fieldKey, f])), [fields])
  const placed = useMemo(() => new Set(groups.flatMap((g) => g.fields)), [groups])
  const unplaced = (fields ?? []).filter((f) => !placed.has(f.fieldKey))
  const conditionSources = (fields ?? []).filter((f) => CONDITION_TYPES.has(f.dataType))

  // --- groups -------------------------------------------------------------------------------------
  const addGroup = () => setGroups((g) => [...g, { title: `Section ${g.length + 1}`, fields: [], collapsed: false }])
  const patchGroup = (i: number, p: Partial<GroupDraft>) =>
    setGroups((g) => g.map((x, j) => (j === i ? { ...x, ...p } : x)))
  const removeGroup = (i: number) => setGroups((g) => g.filter((_, j) => j !== i))
  const moveGroup = (i: number, d: -1 | 1) =>
    setGroups((g) => {
      const j = i + d
      if (j < 0 || j >= g.length) return g
      const next = [...g]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  const placeField = (key: string, gi: number) =>
    setGroups((g) => g.map((x, j) => (j === gi ? { ...x, fields: [...x.fields, key] } : { ...x, fields: x.fields.filter((k) => k !== key) })))
  const unplaceField = (key: string) => setGroups((g) => g.map((x) => ({ ...x, fields: x.fields.filter((k) => k !== key) })))
  const moveField = (gi: number, i: number, d: -1 | 1) =>
    setGroups((g) =>
      g.map((x, j) => {
        if (j !== gi) return x
        const k = i + d
        if (k < 0 || k >= x.fields.length) return x
        const f = [...x.fields]
        ;[f[i], f[k]] = [f[k], f[i]]
        return { ...x, fields: f }
      }),
    )

  // --- rules --------------------------------------------------------------------------------------
  const ruleOf = (key: string): FieldDraft => rules[key] ?? { visibleIf: null, requiredIf: null, readOnly: false }
  const patchRule = (key: string, p: Partial<FieldDraft>) => setRules((r) => ({ ...r, [key]: { ...ruleOf(key), ...p } }))

  // --- templates ----------------------------------------------------------------------------------
  const addTemplate = () => {
    const name = `New ${noun}`
    const id = slug(name) + '-' + (templates.length + 1)
    setTemplates((t) => [...t, { id, name, defaults: {} }])
    setOpenTemplate(id)
  }
  const patchTemplate = (id: string, p: Partial<TemplateDraft>) =>
    setTemplates((t) => t.map((x) => (x.id === id ? { ...x, ...p } : x)))
  const removeTemplate = (id: string) => setTemplates((t) => t.filter((x) => x.id !== id))

  // --- save ---------------------------------------------------------------------------------------
  function buildConfig(): FormConfig {
    const fs = fields ?? []
    const out: FormConfig = { version: 1 }
    const gs = groups.filter((g) => g.fields.length > 0).map((g) => ({ title: g.title.trim() || 'Section', fields: g.fields, ...(g.collapsed ? { collapsed: true } : {}) }))
    if (gs.length) out.groups = gs
    const fr: NonNullable<FormConfig['fields']> = {}
    for (const [k, d] of Object.entries(rules)) {
      if (!byKey.has(k)) continue
      const v = fromRuleDraft(d.visibleIf, fs)
      const r = fromRuleDraft(d.requiredIf, fs)
      const entry: NonNullable<FormConfig['fields']>[string] = {}
      if (v) entry.visibleIf = v
      if (r) entry.requiredIf = r
      if (d.readOnly) entry.readOnly = true
      if (Object.keys(entry).length) fr[k] = entry
    }
    if (Object.keys(fr).length) out.fields = fr
    const ts: FormTemplate[] = templates.map((t) => {
      const defaults: Record<string, unknown> = {}
      for (const [k, raw] of Object.entries(t.defaults)) {
        const f = byKey.get(k)
        if (!f || raw === '') continue
        defaults[k] = f.dataType === 'number' ? Number(raw) : f.dataType === 'boolean' ? raw === 'true' : raw
      }
      return { id: t.id, name: t.name.trim() || t.id, defaults }
    })
    if (ts.length) out.templates = ts
    if (photoPrompt) out.photo = { prompt: true }
    return out
  }

  async function save() {
    setSaving(true)
    setErrors([])
    try {
      const saved = await putLayerForm(layerId, buildConfig(), accessToken)
      onSaved(saved)
      setSavedFlash(true)
      window.setTimeout(() => setSavedFlash(false), 1500)
    } catch (err) {
      if (err instanceof ApiError) {
        // The door's 400 carries every problem as an array; show them all, not the first.
        const detail = err.message
        setErrors(detail.startsWith('[') ? (JSON.parse(detail) as string[]) : [detail])
      } else setErrors(['request failed'])
    } finally {
      setSaving(false)
    }
  }

  async function reset() {
    setSaving(true)
    setErrors([])
    try {
      await clearLayerForm(layerId, accessToken)
      setGroups([])
      setRules({})
      setTemplates([])
      setPhotoPrompt(false)
      onSaved(null)
    } catch (err) {
      setErrors([err instanceof ApiError ? err.message : 'request failed'])
    } finally {
      setSaving(false)
    }
  }

  const label = (key: string) => byKey.get(key)?.label ?? key

  function RuleEditor({ kind, fieldKey }: { kind: 'visibleIf' | 'requiredIf'; fieldKey: string }) {
    const d = ruleOf(fieldKey)
    const r = d[kind]
    const src = r ? byKey.get(r.field) : undefined
    const opts = src?.dataType === 'coded' ? (domainValues[src.domainId ?? ''] ?? []) : []
    const set = (p: Partial<RuleDraft> | null) =>
      patchRule(fieldKey, { [kind]: p === null ? null : { ...(r ?? { field: '', op: 'eq', values: [] }), ...p } })
    return (
      <div className="form-panel__rule">
        <span className="form-panel__rulelabel">{kind === 'visibleIf' ? 'Show when' : 'Required when'}</span>
        <select
          value={r?.field ?? ''}
          onChange={(e) => (e.target.value ? set({ field: e.target.value, op: 'eq', values: [] }) : set(null))}
          aria-label={`${kind === 'visibleIf' ? 'Show' : 'Required'} when field`}
        >
          <option value="">always</option>
          {conditionSources
            .filter((f) => f.fieldKey !== fieldKey)
            .map((f) => (
              <option key={f.fieldKey} value={f.fieldKey}>
                {f.label}
              </option>
            ))}
        </select>
        {r && (
          <select value={r.op} onChange={(e) => set({ op: e.target.value as Op, values: [] })} aria-label="operator">
            <option value="eq">is</option>
            {src?.dataType === 'coded' && <option value="in">is any of</option>}
            <option value="notEmpty">is filled in</option>
            <option value="isEmpty">is empty</option>
          </select>
        )}
        {r && (r.op === 'eq' || r.op === 'in') && src?.dataType === 'coded' && (
          <select
            multiple={r.op === 'in'}
            value={r.op === 'in' ? r.values : (r.values[0] ?? '')}
            onChange={(e) =>
              set({ values: r.op === 'in' ? Array.from(e.target.selectedOptions).map((o) => o.value) : [e.target.value] })
            }
            aria-label="value"
          >
            {r.op === 'eq' && <option value="">choose…</option>}
            {opts.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
        )}
        {r && r.op === 'eq' && src?.dataType === 'boolean' && (
          <select value={r.values[0] ?? 'true'} onChange={(e) => set({ values: [e.target.value] })} aria-label="value">
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        )}
        {r && r.op === 'eq' && src?.dataType === 'text' && (
          <input value={r.values[0] ?? ''} onChange={(e) => set({ values: [e.target.value] })} placeholder="exact text" aria-label="value" />
        )}
      </div>
    )
  }

  return (
    <div className="styles-panel form-panel">
      <div className="styles-panel__header">
        <h3 className="styles-panel__title">Form · {layerLabel}</h3>
        <CloseButton onClick={onClose} />
      </div>

      {loadError ? (
        <p className="sign-form__error">Could not load fields ({loadError})</p>
      ) : !fields ? (
        <p className="styles-panel__muted">Loading fields…</p>
      ) : fields.length === 0 ? (
        <p className="styles-panel__muted">This layer has no fields yet. Add fields on the layer's Fields page first.</p>
      ) : (
        <>
          {/* ---- sections ---- */}
          <div className="form-panel__bar">
            <p className="styles-panel__muted">Sections, top to bottom. Fields left out show in a trailing “Other”.</p>
            <button type="button" className="btn btn--sm" onClick={addGroup}>
              <Plus size={13} aria-hidden="true" /> Section
            </button>
          </div>
          {groups.map((g, gi) => (
            <div className="form-panel__group" key={gi}>
              <div className="form-panel__grouphead">
                <input value={g.title} onChange={(e) => patchGroup(gi, { title: e.target.value })} aria-label="Section title" />
                <label className="form-panel__mini">
                  <input type="checkbox" checked={g.collapsed} onChange={(e) => patchGroup(gi, { collapsed: e.target.checked })} />
                  folded
                </label>
                <span className="popups-panel__ud">
                  <button type="button" aria-label="Move section up" disabled={gi === 0} onClick={() => moveGroup(gi, -1)}>
                    <ChevronUp size={13} aria-hidden="true" />
                  </button>
                  <button type="button" aria-label="Move section down" disabled={gi === groups.length - 1} onClick={() => moveGroup(gi, 1)}>
                    <ChevronDown size={13} aria-hidden="true" />
                  </button>
                </span>
                <button type="button" className="form-panel__icon" aria-label="Remove section" onClick={() => removeGroup(gi)}>
                  <Trash2 size={13} aria-hidden="true" />
                </button>
              </div>
              <ul className="popups-panel__rows">
                {g.fields.map((k, i) => (
                  <li className="popups-panel__row" key={k}>
                    <button type="button" className="form-panel__fieldbtn" onClick={() => setOpenField(openField === k ? null : k)}>
                      {label(k)}
                      {(ruleOf(k).visibleIf || ruleOf(k).requiredIf || ruleOf(k).readOnly) && <span className="layer-inspector__dot" aria-hidden="true" />}
                    </button>
                    <span className="popups-panel__ud">
                      <button type="button" aria-label={`Move ${label(k)} up`} disabled={i === 0} onClick={() => moveField(gi, i, -1)}>
                        <ChevronUp size={13} aria-hidden="true" />
                      </button>
                      <button type="button" aria-label={`Move ${label(k)} down`} disabled={i === g.fields.length - 1} onClick={() => moveField(gi, i, 1)}>
                        <ChevronDown size={13} aria-hidden="true" />
                      </button>
                    </span>
                    <button type="button" className="form-panel__icon" aria-label={`Remove ${label(k)} from section`} onClick={() => unplaceField(k)}>
                      <X size={13} aria-hidden="true" />
                    </button>
                  </li>
                ))}
              </ul>
              {unplaced.length > 0 && (
                <select
                  value=""
                  onChange={(e) => e.target.value && placeField(e.target.value, gi)}
                  aria-label={`Add a field to ${g.title}`}
                  className="form-panel__addfield"
                >
                  <option value="">Add field…</option>
                  {unplaced.map((f) => (
                    <option key={f.fieldKey} value={f.fieldKey}>
                      {f.label}
                    </option>
                  ))}
                </select>
              )}
            </div>
          ))}
          {unplaced.length > 0 && (
            <div className="form-panel__group form-panel__group--other">
              <div className="form-panel__grouphead">
                <span className="styles-panel__muted">{groups.length ? 'Other (not in a section)' : 'Fields'}</span>
              </div>
              <ul className="popups-panel__rows">
                {unplaced.map((f) => (
                  <li className="popups-panel__row" key={f.fieldKey}>
                    <button type="button" className="form-panel__fieldbtn" onClick={() => setOpenField(openField === f.fieldKey ? null : f.fieldKey)}>
                      {f.label}
                      {(ruleOf(f.fieldKey).visibleIf || ruleOf(f.fieldKey).requiredIf || ruleOf(f.fieldKey).readOnly) && (
                        <span className="layer-inspector__dot" aria-hidden="true" />
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ---- the open field's rules ---- */}
          {openField && byKey.has(openField) && (
            <div className="form-panel__rules">
              <p className="form-panel__rulestitle">{label(openField)}</p>
              {byKey.get(openField)!.required ? (
                <p className="styles-panel__muted">Always required (set on the field), so it is always shown.</p>
              ) : (
                <RuleEditor kind="visibleIf" fieldKey={openField} />
              )}
              {byKey.get(openField)!.dataType !== 'boolean' && !byKey.get(openField)!.required && (
                <RuleEditor kind="requiredIf" fieldKey={openField} />
              )}
              {ruleOf(openField).visibleIf && ruleOf(openField).requiredIf && (
                <p className="styles-panel__muted">When both are set they must match — the server checks.</p>
              )}
              <label className="form-panel__mini">
                <input type="checkbox" checked={ruleOf(openField).readOnly} onChange={(e) => patchRule(openField, { readOnly: e.target.checked })} />
                Read-only in the form (shown, not editable)
              </label>
            </div>
          )}

          {/* ---- templates ---- */}
          <div className="form-panel__bar">
            <p className="styles-panel__muted">Templates — the “Add” chips a crew picks from; each pre-fills values.</p>
            <button type="button" className="btn btn--sm" onClick={addTemplate}>
              <Plus size={13} aria-hidden="true" /> Template
            </button>
          </div>
          {templates.map((t) => (
            <div className="form-panel__group" key={t.id}>
              <div className="form-panel__grouphead">
                <input value={t.name} onChange={(e) => patchTemplate(t.id, { name: e.target.value })} aria-label="Template name" />
                <button type="button" className="btn btn--sm btn--ghost" onClick={() => setOpenTemplate(openTemplate === t.id ? null : t.id)}>
                  {openTemplate === t.id ? 'Done' : 'Values'}
                </button>
                <button type="button" className="form-panel__icon" aria-label={`Remove template ${t.name}`} onClick={() => removeTemplate(t.id)}>
                  <Trash2 size={13} aria-hidden="true" />
                </button>
              </div>
              {openTemplate === t.id && (
                <div className="labels-panel__grid form-panel__defaults">
                  {fields.map((f) => (
                    <label key={f.fieldKey} className="form-panel__deflabel">
                      <span>{f.label}</span>
                      {f.dataType === 'coded' ? (
                        <select value={t.defaults[f.fieldKey] ?? ''} onChange={(e) => patchTemplate(t.id, { defaults: { ...t.defaults, [f.fieldKey]: e.target.value } })}>
                          <option value="">—</option>
                          {(domainValues[f.domainId ?? ''] ?? []).filter((o) => o.active !== false).map((o) => (
                            <option key={o.code} value={o.code}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      ) : f.dataType === 'boolean' ? (
                        <select value={t.defaults[f.fieldKey] ?? ''} onChange={(e) => patchTemplate(t.id, { defaults: { ...t.defaults, [f.fieldKey]: e.target.value } })}>
                          <option value="">—</option>
                          <option value="true">Yes</option>
                          <option value="false">No</option>
                        </select>
                      ) : (
                        <input
                          type={f.dataType === 'number' ? 'number' : f.dataType === 'date' ? 'date' : 'text'}
                          value={t.defaults[f.fieldKey] ?? ''}
                          onChange={(e) => patchTemplate(t.id, { defaults: { ...t.defaults, [f.fieldKey]: e.target.value } })}
                        />
                      )}
                    </label>
                  ))}
                </div>
              )}
            </div>
          ))}

          {attachmentsEnabled && (
            <label className="form-panel__mini form-panel__photo">
              <input type="checkbox" checked={photoPrompt} onChange={(e) => setPhotoPrompt(e.target.checked)} />
              After saving without a photo, ask the crew for one (never required)
            </label>
          )}

          {/* ---- preview ---- */}
          <button type="button" className="btn btn--sm btn--ghost form-panel__previewbtn" onClick={() => setPreview((p) => !p)}>
            {preview ? 'Hide preview' : 'Preview the saved form'}
          </button>
          {preview && (
            <div className="form-panel__preview">
              <p className="styles-panel__muted">As a crew member sees it after Save (the saved form, not unsaved edits).</p>
              <div className="form-panel__phone">
                <SlotForm
                  key={savedFlash ? 'a' : 'b'}
                  mode="create"
                  initial={null}
                  busy={false}
                  error={null}
                  onSubmit={() => undefined}
                  onCancel={() => setPreview(false)}
                  layerId={layerId}
                  accessToken={accessToken}
                  noun={noun}
                  attachmentsEnabled={attachmentsEnabled}
                />
              </div>
            </div>
          )}
        </>
      )}

      {errors.length > 0 && (
        <ul className="sign-form__error form-panel__errors">
          {errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}

      <div className="sign-form__actions">
        {(initial || groups.length || templates.length) && (
          <button type="button" className="btn btn--sm popups-panel__reset" disabled={saving} onClick={() => void reset()}>
            Reset to default
          </button>
        )}
        <button type="button" className="btn btn--sm" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className="btn btn--sm" onClick={() => void save()} disabled={!fields || saving}>
          {saving ? 'Saving…' : savedFlash ? 'Saved ✓' : 'Save'}
        </button>
      </div>
    </div>
  )
}
