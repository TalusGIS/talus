import { forwardRef, useEffect, useImperativeHandle, useState } from 'react'
import { ArrowDown, ArrowUp, X } from 'lucide-react'
import type { ChangeEvent } from 'react'

import {
  ApiError,
  countContingencyViolations,
  countFieldViolations,
  createCustomDomain,
  createCustomField,
  deleteCustomField,
  exportCustomFieldsCsv,
  exportCustomFieldsGpkg,
  getCustomDomain,
  importCustomFields,
  importEsriXmlFields,
  importGeopackageFields,
  listCustomDomains,
  listCustomFields,
  updateCustomField,
} from '@/lib/api'
import type {
  ConstraintViolationCount,
  ContingentValues,
  CustomDomain,
  CustomField,
  CustomFieldCreate,
  DomainValue,
  EsriXmlTable,
  FieldConstraints,
  FieldDataType,
  MyLayer,
  NumericSubtype,
  SchemaImportResult,
  SchemaImportStatus,
} from '@/lib/api'
import { useIsPublisher } from '@/lib/auth/roles'
import { GNSS_BUNDLE, gnssBundleDrafts } from '@/lib/gnssBundle'

/**
 * The **Fields** tab of a slot layer's Manage dialog. The layer owner (or admin) defines typed
 * attribute fields — text / number / date / true-false / **picklist (coded)** — that become editable
 * inputs on the feature form and are validated on save (app-side + the V044/V046 trigger). A `coded`
 * field binds a REUSABLE domain (slice 2): pick an existing org-wide domain or create one inline.
 *
 * Slice 3 (decision-constraint-rules.md) adds declarative **rules**: a numeric/date **range**, a text
 * **length** bound, a text **pattern** (preset or advanced regex), and a date **default-to-today** token.
 * Rules can be set when adding a field and edited later per field; tightening a rule never retro-rejects
 * existing rows (soft) — the inline editor previews how many already violate before you save.
 */

// The GIS-native field model (decision-create-wizard.md §3): TYPE = the storage data type; FORMAT = how it's
// constrained/presented. Yes/No and Picklist live under FORMAT (people don't think of boolean/coded as
// types, but as formats of a text/number field) and OVERRIDE the storage class when chosen.
type UiType = 'short_integer' | 'long_integer' | 'float' | 'double' | 'text' | 'date'
const UI_TYPES: { value: UiType; label: string }[] = [
  { value: 'short_integer', label: 'Short Integer' },
  { value: 'long_integer', label: 'Long Integer' },
  { value: 'float', label: 'Float' },
  { value: 'double', label: 'Double' },
  { value: 'text', label: 'Text' },
  { value: 'date', label: 'Date' },
]
/** (Type, Format) -> the backend field shape. Picklist -> coded, Yes/No -> boolean; else the base Type. */
function composeType(uiType: UiType, uiFormat: string): { dataType: FieldDataType; numericSubtype?: NumericSubtype } {
  if (uiFormat === 'picklist') return { dataType: 'coded' }
  if (uiFormat === 'yesno') return { dataType: 'boolean' }
  if (uiType === 'text') return { dataType: 'text' }
  if (uiType === 'date') return { dataType: 'date' }
  return { dataType: 'number', numericSubtype: uiType as NumericSubtype }
}

/** A friendly one-word type label for the field list line. */
function typeLabel(f: { dataType: FieldDataType; numericSubtype?: NumericSubtype | null }): string {
  if (f.dataType === 'coded') return 'picklist'
  if (f.dataType === 'boolean') return 'yes / no'
  if (f.dataType === 'number' && f.numericSubtype) {
    return UI_TYPES.find((t) => t.value === f.numericSubtype)?.label.toLowerCase() ?? 'number'
  }
  return f.dataType
}

const KEY_RE = /^[a-z][a-z0-9_]{0,62}$/

// Curated text-format presets (the 80%). Each is an anchored regex; the server re-anchors + guards.
// 'advanced' is the escape hatch for org-specific formats (APN / permit / asset-id) no preset can cover.
const PATTERN_PRESETS: { key: string; label: string; pattern: string }[] = [
  { key: 'phone_us', label: 'Phone — 123-456-7890', pattern: '^\\d{3}-\\d{3}-\\d{4}$' },
  { key: 'zip_us', label: 'ZIP — 12345 or 12345-6789', pattern: '^\\d{5}(-\\d{4})?$' },
  { key: 'email', label: 'Email address', pattern: '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$' },
  { key: 'url', label: 'URL — http(s)://…', pattern: '^https?://\\S+$' },
]

function coerceDefault(type: FieldDataType, raw: string): unknown | null {
  const v = raw.trim()
  if (v === '') return null
  if (type === 'number') {
    const n = Number(v)
    return Number.isFinite(n) ? n : raw
  }
  if (type === 'boolean') return v === 'true'
  return v
}

interface DraftValue {
  code: string
  label: string
}

/** UI-side draft of a field's rules (all strings, easy to bind to inputs). */
interface RuleDraft {
  min: string
  max: string
  minLength: string
  maxLength: string
  patternMode: string // 'none' | <preset key> | 'advanced'
  patternCustom: string
  today: boolean // date: default new features to today's date
}

const EMPTY_RULES: RuleDraft = {
  min: '',
  max: '',
  minLength: '',
  maxLength: '',
  patternMode: 'none',
  patternCustom: '',
  today: true, // date fields default to "fill with today" (the common created-date pattern); uncheck to opt out
}

function resolvePattern(d: RuleDraft): string | undefined {
  if (d.patternMode === 'none') return undefined
  if (d.patternMode === 'advanced') return d.patternCustom.trim() || undefined
  return PATTERN_PRESETS.find((p) => p.key === d.patternMode)?.pattern
}

/** Build the API `constraints` object for a type from the draft (undefined = no rules). */
function draftToConstraints(type: FieldDataType, d: RuleDraft): FieldConstraints | undefined {
  const c: FieldConstraints = {}
  if (type === 'number') {
    if (d.min.trim() !== '') c.min = Number(d.min)
    if (d.max.trim() !== '') c.max = Number(d.max)
  } else if (type === 'date') {
    if (d.min.trim() !== '') c.min = d.min
    if (d.max.trim() !== '') c.max = d.max
  } else if (type === 'text') {
    if (d.minLength.trim() !== '') c.minLength = Number(d.minLength)
    if (d.maxLength.trim() !== '') c.maxLength = Number(d.maxLength)
    const pat = resolvePattern(d)
    if (pat) c.pattern = pat
  }
  return Object.keys(c).length > 0 ? c : undefined
}

/** Seed a draft from an existing field's stored constraints + default kind (for the inline editor). */
function fieldToDraft(f: CustomField): RuleDraft {
  const c = f.constraints ?? {}
  // The server stores patterns whole-string-anchored as `^(?:RAW)$`, so match a preset by its wrapped
  // form (fall back to the raw form for any legacy/unwrapped value). Anything else is 'advanced'.
  const presetKey = PATTERN_PRESETS.find(
    (p) => c.pattern === `^(?:${p.pattern})$` || c.pattern === p.pattern,
  )?.key
  return {
    min: c.min !== undefined ? String(c.min) : '',
    max: c.max !== undefined ? String(c.max) : '',
    minLength: c.minLength !== undefined ? String(c.minLength) : '',
    maxLength: c.maxLength !== undefined ? String(c.maxLength) : '',
    patternMode: c.pattern ? (presetKey ?? 'advanced') : 'none',
    patternCustom: c.pattern && !presetKey ? c.pattern : '',
    today: f.defaultKind === 'today',
  }
}

/** Compact human summary of a field's rules for the list line. */
function ruleSummary(f: CustomField): string {
  const c = f.constraints
  const parts: string[] = []
  if (c) {
    if (f.dataType === 'number' || f.dataType === 'date') {
      if (c.min !== undefined && c.max !== undefined) parts.push(`${c.min}–${c.max}`)
      else if (c.min !== undefined) parts.push(`≥ ${c.min}`)
      else if (c.max !== undefined) parts.push(`≤ ${c.max}`)
    }
    if (f.dataType === 'text') {
      if (c.minLength !== undefined && c.maxLength !== undefined) parts.push(`${c.minLength}–${c.maxLength} chars`)
      else if (c.minLength !== undefined) parts.push(`≥ ${c.minLength} chars`)
      else if (c.maxLength !== undefined) parts.push(`≤ ${c.maxLength} chars`)
      if (c.pattern) parts.push('format')
    }
  }
  return parts.length ? ` · ${parts.join(' · ')}` : ''
}

/** Per-type rule inputs, shared by the add form and the inline editor. */
function RuleInputs({
  type,
  draft,
  set,
  hidePattern = false,
}: {
  type: FieldDataType
  draft: RuleDraft
  set: (patch: Partial<RuleDraft>) => void
  // The add-field form owns text patterns via the top-level Format dropdown, so it hides the in-grid one.
  hidePattern?: boolean
}) {
  if (type === 'number' || type === 'date') {
    const inputType = type === 'number' ? 'number' : 'date'
    return (
      <div className="cf-rules__grid">
        <label className="sign-form__field">
          <span>Minimum</span>
          <input type={inputType} value={draft.min} onChange={(e) => set({ min: e.target.value })} />
        </label>
        <label className="sign-form__field">
          <span>Maximum</span>
          <input type={inputType} value={draft.max} onChange={(e) => set({ max: e.target.value })} />
        </label>
        {type === 'date' && (
          <label className="sign-form__field cf-rules__full">
            <span style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
              <input
                type="checkbox"
                checked={draft.today}
                onChange={(e) => set({ today: e.target.checked })}
                style={{ width: 'auto', margin: 0 }}
              />
              Default new features to today’s date
            </span>
          </label>
        )}
      </div>
    )
  }
  if (type === 'text') {
    return (
      <div className="cf-rules__grid">
        <label className="sign-form__field">
          <span>Min length</span>
          <input type="number" min={0} value={draft.minLength} onChange={(e) => set({ minLength: e.target.value })} />
        </label>
        <label className="sign-form__field">
          <span>Max length</span>
          <input type="number" min={0} value={draft.maxLength} onChange={(e) => set({ maxLength: e.target.value })} />
        </label>
        {!hidePattern && (
          <label className="sign-form__field cf-rules__full">
            <span>Format</span>
            <select value={draft.patternMode} onChange={(e) => set({ patternMode: e.target.value })}>
              <option value="none">— any text —</option>
              {PATTERN_PRESETS.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label}
                </option>
              ))}
              <option value="advanced">Custom pattern (advanced)…</option>
            </select>
          </label>
        )}
        {!hidePattern && draft.patternMode === 'advanced' && (
          <label className="sign-form__field cf-rules__full">
            <span>
              Regular expression <span className="modal__muted">(anchored automatically)</span>
            </span>
            <input
              type="text"
              value={draft.patternCustom}
              placeholder="^[A-Z]{2}-\d{4}$"
              onChange={(e) => set({ patternCustom: e.target.value })}
            />
          </label>
        )}
      </div>
    )
  }
  return null // boolean / coded carry no extra rules
}

/** Inline editor to set/tighten rules on an EXISTING field, with a fork-7 impact preview before save. */
function FieldRulesEditor({
  layerId,
  field,
  accessToken,
  onClose,
  onSaved,
}: {
  layerId: string
  field: CustomField
  accessToken: string
  onClose: () => void
  onSaved: () => void
}) {
  const [draft, setDraft] = useState<RuleDraft>(() => fieldToDraft(field))
  const [impact, setImpact] = useState<ConstraintViolationCount | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  function set(patch: Partial<RuleDraft>): void {
    setDraft((d) => ({ ...d, ...patch }))
    setImpact(null) // any change invalidates the last preview
  }

  async function checkImpact(): Promise<void> {
    setBusy(true)
    setErr(null)
    try {
      const c = draftToConstraints(field.dataType, draft)
      setImpact(await countFieldViolations(layerId, field.id, c ?? {}, accessToken))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Couldn’t check impact')
    } finally {
      setBusy(false)
    }
  }

  async function save(): Promise<void> {
    setBusy(true)
    setErr(null)
    try {
      const c = draftToConstraints(field.dataType, draft)
      const today = field.dataType === 'date' && draft.today
      await updateCustomField(
        layerId,
        field.id,
        {
          constraints: c ?? null,
          defaultKind: today ? 'today' : 'constant',
          // 'today' can't coexist with a constant default — clear it as we switch on.
          ...(today ? { defaultValue: null } : {}),
        },
        accessToken,
      )
      onSaved()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="cf-rules">
      <RuleInputs type={field.dataType} draft={draft} set={set} />
      {err !== null && <p className="sign-form__error">{err}</p>}
      {impact !== null && (
        <p className="modal__muted cf-rules__impact">
          {impact.violating === 0
            ? `No existing rows violate this rule (${impact.total} total).`
            : `${impact.violating} of ${impact.total} existing rows already violate this — they keep their values until next edited.`}
        </p>
      )}
      <div className="sign-form__actions">
        <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void checkImpact()}>
          Check impact
        </button>
        <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void save()}>
          {busy ? 'Saving…' : 'Save rules'}
        </button>
        <button type="button" className="btn btn--xs" disabled={busy} onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>
  )
}

/**
 * Inline editor for a coded field's CONTINGENCY (V077, decision-contingent-values.md): pick the coded
 * sibling it depends on, then per parent value check which of this field's codes are allowed. A parent
 * value with nothing checked gets NO entry = unrestricted (fork 3's permissive rule); the pair itself is
 * trigger-enforced server-side. Impact preview before save, like rules (fork-7 pattern).
 */
function FieldContingencyEditor({
  layerId,
  field,
  siblings,
  accessToken,
  onClose,
  onSaved,
}: {
  layerId: string
  field: CustomField
  /** The layer's OTHER active coded fields — the parent candidates. */
  siblings: CustomField[]
  accessToken: string
  onClose: () => void
  onSaved: () => void
}) {
  const [parentKey, setParentKey] = useState<string>(field.parentFieldKey ?? '')
  const [mapping, setMapping] = useState<ContingentValues>(field.contingentValues ?? {})
  const [parentValues, setParentValues] = useState<DomainValue[] | null>(null)
  const [childValues, setChildValues] = useState<DomainValue[] | null>(null)
  const [impact, setImpact] = useState<ConstraintViolationCount | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const parent = siblings.find((s) => s.fieldKey === parentKey)

  useEffect(() => {
    if (!field.domainId) return
    getCustomDomain(field.domainId, accessToken)
      .then((d) => setChildValues(d.values.filter((v) => v.active)))
      .catch(() => setChildValues([]))
  }, [field.domainId, accessToken])

  useEffect(() => {
    setImpact(null)
    if (!parent?.domainId) {
      setParentValues(null)
      return
    }
    getCustomDomain(parent.domainId, accessToken)
      .then((d) => setParentValues(d.values.filter((v) => v.active)))
      .catch(() => setParentValues([]))
  }, [parent?.domainId, accessToken])

  function pickParent(key: string): void {
    setParentKey(key)
    // A mapping is keyed by the parent's codes — switching parents invalidates it (restore the stored
    // one when switching back).
    setMapping(key === (field.parentFieldKey ?? '') ? (field.contingentValues ?? {}) : {})
    setImpact(null)
  }

  function toggle(pcode: string, ccode: string, on: boolean): void {
    setMapping((m) => {
      const cur = m[pcode] ?? []
      const next = on ? [...cur, ccode] : cur.filter((c) => c !== ccode)
      const out = { ...m, [pcode]: next }
      return out
    })
    setImpact(null)
  }

  /** Entries with nothing checked are dropped — no entry = unrestricted for that parent value. */
  function cleaned(): ContingentValues {
    return Object.fromEntries(Object.entries(mapping).filter(([, codes]) => codes.length > 0))
  }

  const clearing = parentKey === '' || Object.keys(cleaned()).length === 0

  async function checkImpact(): Promise<void> {
    if (clearing) return
    setBusy(true)
    setErr(null)
    try {
      setImpact(await countContingencyViolations(layerId, field.id, parentKey, cleaned(), accessToken))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Couldn’t check impact')
    } finally {
      setBusy(false)
    }
  }

  async function save(): Promise<void> {
    setBusy(true)
    setErr(null)
    try {
      await updateCustomField(
        layerId,
        field.id,
        clearing
          ? { parentFieldKey: null, contingentValues: null }
          : { parentFieldKey: parentKey, contingentValues: cleaned() },
        accessToken,
      )
      onSaved()
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="cf-rules">
      <label className="sign-form__field">
        <span>
          Depends on <span className="modal__muted">(this field’s picklist narrows by that field’s value)</span>
        </span>
        <select value={parentKey} disabled={busy} onChange={(e) => pickParent(e.target.value)}>
          <option value="">— no dependency —</option>
          {siblings.map((s) => (
            <option key={s.fieldKey} value={s.fieldKey}>
              {s.label} ({s.fieldKey})
            </option>
          ))}
        </select>
      </label>

      {parent && parentValues !== null && childValues !== null && (
        <>
          <p className="modal__muted">
            For each <strong>{parent.label}</strong> value, check the <strong>{field.label}</strong> values
            allowed under it. Leaving a row unchecked keeps it unrestricted.
          </p>
          {parentValues.length === 0 && (
            <p className="modal__muted">That field’s domain has no active values.</p>
          )}
          <div className="cf-contingency__rows">
            {parentValues.map((pv) => (
              <div key={pv.code} className="sign-form__field">
                <span>
                  {pv.label} <code className="modal__muted">{pv.code}</code>
                  {(mapping[pv.code] ?? []).length === 0 && <span className="modal__muted"> — unrestricted</span>}
                </span>
                <div className="cf-contingency__codes">
                  {childValues.map((cv) => (
                    <label key={cv.code} style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3rem', marginRight: '0.9rem' }}>
                      <input
                        type="checkbox"
                        checked={(mapping[pv.code] ?? []).includes(cv.code)}
                        disabled={busy}
                        onChange={(e) => toggle(pv.code, cv.code, e.target.checked)}
                        style={{ width: 'auto', margin: 0 }}
                      />
                      {cv.label}
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {err !== null && <p className="sign-form__error">{err}</p>}
      {impact !== null && (
        <p className="modal__muted cf-rules__impact">
          {impact.violating === 0
            ? `No existing rows hold a pair this would forbid (${impact.total} total).`
            : `${impact.violating} of ${impact.total} existing rows already hold a forbidden pair — they keep their values until either field is next edited.`}
        </p>
      )}
      <div className="sign-form__actions">
        {!clearing && (
          <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void checkImpact()}>
            Check impact
          </button>
        )}
        <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void save()}>
          {busy ? 'Saving…' : clearing ? 'Clear dependency' : 'Save dependency'}
        </button>
        <button type="button" className="btn btn--xs" disabled={busy} onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>
  )
}

// A downloadable CSV template matching the server's columns (decision-schema-import.md §3).
const IMPORT_TEMPLATE = [
  'field_key,label,data_type,required,default,default_today,min,max,min_length,max_length,pattern',
  'pipe_diameter,Pipe Diameter,number,false,,,0,48,,,',
  'inspected_on,Inspected On,date,true,,yes,,,,,',
  'asset_tag,Asset Tag,text,false,,,,,3,20,"^[A-Z]{2}-\\d{4}$"',
  '',
].join('\n')

function downloadText(filename: string, content: string): void {
  downloadBlob(filename, new Blob([content], { type: 'text/csv' }))
}

function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function importBadge(s: SchemaImportStatus): string {
  if (s === 'ok' || s === 'created') return 'sa-badge sa-badge--on'
  if (s === 'renamed') return 'sa-badge sa-badge--rw' // imported, but the name changed — say so in amber
  if (s === 'coded' || s === 'duplicate') return 'sa-badge sa-badge--ro'
  return 'sa-badge sa-badge--off' // error
}

/** Base64-encode binary (a .gpkg or .xml) for the JSON transport. Chunked so a multi-MB XML stays fast. */
function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  const CHUNK = 0x8000
  let bin = ''
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK))
  }
  return btoa(bin)
}

/** Bulk-import fields from a CSV or GeoPackage (decision-schema-import.md): pick a file → server dry-run
 * preview → commit. The file is read in the browser and sent (CSV as text, .gpkg as base64); the server
 * parses + validates and never persists it. Both formats share the same preview/commit UI. */
function ImportPanel({
  layerId,
  accessToken,
  onClose,
  onImported,
}: {
  layerId: string
  accessToken: string
  onClose: () => void
  onImported: () => void
}) {
  // The parsed source is held so Commit (and the XML feature-class picker) can replay it.
  const [csvText, setCsvText] = useState('')
  const [binB64, setBinB64] = useState('') // base64 of a .gpkg or .xml
  const [kind, setKind] = useState<'csv' | 'gpkg' | 'xml' | null>(null)
  const [fileName, setFileName] = useState('')
  const [xmlTables, setXmlTables] = useState<EsriXmlTable[] | null>(null) // the FC picker list (XML, multi-table)
  const [xmlTable, setXmlTable] = useState('') // the chosen feature class
  const [preview, setPreview] = useState<SchemaImportResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  function reset(): void {
    setPreview(null)
    setXmlTables(null)
    setXmlTable('')
    setErr(null)
  }

  async function onPickFile(e: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = e.target.files?.[0]
    if (!file) return
    setBusy(true)
    reset()
    try {
      setFileName(file.name)
      const name = file.name.toLowerCase()
      if (name.endsWith('.gpkg')) {
        const b64 = arrayBufferToBase64(await file.arrayBuffer())
        setBinB64(b64)
        setKind('gpkg')
        setPreview(await importGeopackageFields(layerId, b64, false, accessToken)) // dry-run
      } else if (name.endsWith('.xml')) {
        const b64 = arrayBufferToBase64(await file.arrayBuffer())
        setBinB64(b64)
        setKind('xml')
        const r = await importEsriXmlFields(layerId, b64, null, false, accessToken)
        if ('needsTable' in r) setXmlTables(r.tables) // GDB is multi-table → show the picker
        else setPreview(r) // single feature class → straight to preview
      } else {
        const text = await file.text()
        setCsvText(text)
        setKind('csv')
        setPreview(await importCustomFields(layerId, text, false, accessToken)) // dry-run
      }
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : 'Couldn’t read the file')
    } finally {
      setBusy(false)
    }
  }

  // XML only: the user picked a feature class -> dry-run that one.
  async function pickTable(table: string): Promise<void> {
    setXmlTable(table)
    if (!table) {
      setPreview(null)
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const r = await importEsriXmlFields(layerId, binB64, table, false, accessToken)
      setPreview('needsTable' in r ? null : r)
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : 'Couldn’t read that feature class')
    } finally {
      setBusy(false)
    }
  }

  async function commit(): Promise<void> {
    setBusy(true)
    setErr(null)
    try {
      let r: SchemaImportResult
      if (kind === 'gpkg') r = await importGeopackageFields(layerId, binB64, true, accessToken)
      else if (kind === 'xml') r = (await importEsriXmlFields(layerId, binB64, xmlTable, true, accessToken)) as SchemaImportResult
      else r = await importCustomFields(layerId, csvText, true, accessToken)
      if ((r.summary.created ?? 0) > 0) onImported()
      setPreview(r)
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : 'Import failed')
    } finally {
      setBusy(false)
    }
  }

  // 'renamed' rows import exactly like 'ok' ones — they must count toward the button's total, or a
  // file whose only importable column was a reserved name would render an Import button that says 0.
  const okCount = preview ? (preview.summary.ok ?? 0) + (preview.summary.renamed ?? 0) : 0
  const s = preview?.summary ?? {}

  return (
    <div className="cf-rules">
      <p className="modal__muted">
        Upload a <strong>CSV</strong>, <strong>GeoPackage</strong> (.gpkg), or <strong>Esri XML Workspace</strong>{' '}
        (.xml) schema to scaffold many fields at once.{' '}
        <button type="button" className="cf-linkbtn" onClick={() => downloadText('talus-field-import-template.csv', IMPORT_TEMPLATE)}>
          Download CSV template
        </button>
        . Picklist (coded) fields are flagged — add those after and bind a domain.
      </p>
      <input type="file" accept=".csv,text/csv,.gpkg,.xml" disabled={busy} onChange={(e) => void onPickFile(e)} />
      {err !== null && <p className="sign-form__error">{err}</p>}

      {xmlTables !== null && (
        <label className="sign-form__field">
          <span>Feature class <span className="modal__muted">({xmlTables.length} in this geodatabase)</span></span>
          <select value={xmlTable} disabled={busy} onChange={(e) => void pickTable(e.target.value)}>
            <option value="">— choose a feature class —</option>
            {xmlTables.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name} ({t.fieldCount} fields)
              </option>
            ))}
          </select>
        </label>
      )}

      {preview !== null && (
        <>
          <p className="modal__muted cf-rules__impact">
            {fileName}: {okCount} ready · {s.created ? `${s.created} created · ` : ''}
            {s.renamed ? `${s.renamed} renamed · ` : ''}
            {s.coded ?? 0} picklist · {s.duplicate ?? 0} duplicate · {s.error ?? 0} error
          </p>
          <table className="data-table cf-import__table">
            <thead>
              <tr>
                <th>Field</th>
                <th>Status</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {preview.rows.map((row, i) => (
                <tr key={`${row.fieldKey}-${i}`}>
                  <td><code>{row.fieldKey || '—'}</code></td>
                  <td><span className={importBadge(row.status)}>{row.status}</span></td>
                  <td className="modal__muted">{row.reason ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div className="sign-form__actions">
        {preview && !preview.committed && okCount > 0 && (
          <button type="button" className="btn btn--sm" disabled={busy} onClick={() => void commit()}>
            {busy ? 'Importing…' : `Import ${okCount} field${okCount === 1 ? '' : 's'}`}
          </button>
        )}
        <button type="button" className="btn btn--xs" disabled={busy} onClick={onClose}>
          {preview?.committed ? 'Done' : 'Cancel'}
        </button>
      </div>
    </div>
  )
}

/**
 * The reusable "Add a field" form (decision-create-wizard.md §3) — Type + Format + rules, building one
 * CustomFieldCreate payload. Used live by CustomFieldsSection (POSTs to the layer) AND by the create wizard
 * in draft mode (pushes to an in-memory list). Inline-created Picklist domains commit live (org-reusable, not
 * orphans), so a draft field can reference them by id either way.
 */
export interface AddFieldFormHandle {
  /** Commit the in-progress field. Resolves the committed payload, or null (invalid form / failed —
   *  the form shows its own inline error). Lets the wizard auto-commit a filled form on Next/Finish
   *  instead of silently discarding it (B5b 2026-08-11: fields typed but never "+ Field"-ed were dropped). */
  submit: () => Promise<CustomFieldCreate | null>
  /** True when the form holds user-typed work (a name or alias) — lets the wizard refuse to
   *  Finish past an incomplete field instead of silently dropping it. */
  isDirty: () => boolean
}

export const AddFieldForm = forwardRef<
  AddFieldFormHandle,
  {
    domains: CustomDomain[]
    accessToken: string
    busy: boolean
    submitLabel: string
    nextSortOrder: number
    onSubmit: (payload: CustomFieldCreate) => Promise<void> | void
    onDomainsChanged: () => void
    /** Hide the internal "+ Field" button; the parent renders one in its own bar (wizard). */
    hideSubmit?: boolean
    /** Reports whether the draft is addable, so a parent-rendered button can enable/disable. */
    onCanAddChange?: (canAdd: boolean) => void
  }
>(function AddFieldForm(
  { domains, accessToken, busy, submitLabel, nextSortOrder, onSubmit, onDomainsChanged, hideSubmit, onCanAddChange },
  ref,
) {
  const [fieldKey, setFieldKey] = useState('')
  const [label, setLabel] = useState('')
  const [uiType, setUiType] = useState<UiType>('text')
  const [uiFormat, setUiFormat] = useState<string>('any')
  const { dataType, numericSubtype } = composeType(uiType, uiFormat)
  const [required, setRequired] = useState(false)
  const [defaultRaw, setDefaultRaw] = useState('')
  const [rules, setRules] = useState<RuleDraft>(EMPTY_RULES)
  const [domainMode, setDomainMode] = useState<'existing' | 'new'>('existing')
  // Creating a picklist needs the publisher or admin role (the API enforces). A layer owner without
  // it picks an existing one — so "Create new" is not offered, rather than offered and refused.
  const canCreateDomain = useIsPublisher()
  // The mode in force: one value for the render, the ready check and the submit, so the three cannot
  // disagree if the role is lost while the form is open.
  const mode = canCreateDomain ? domainMode : 'existing'
  const [selDomainId, setSelDomainId] = useState('')
  const [newDomainName, setNewDomainName] = useState('')
  const [newValues, setNewValues] = useState<DraftValue[]>([{ code: '', label: '' }])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const working = busy || submitting
  const keyValid = KEY_RE.test(fieldKey)
  const codedReady =
    dataType !== 'coded' ||
    (mode === 'existing'
      ? selDomainId !== ''
      : newDomainName.trim() !== '' && newValues.some((v) => v.code.trim() && v.label.trim()))
  const advancedPatternMissing = uiType === 'text' && uiFormat === 'advanced' && rules.patternCustom.trim() === ''
  const canAdd = keyValid && label.trim() !== '' && codedReady && !advancedPatternMissing && !working
  const todayDefault = dataType === 'date' && rules.today
  const typeHasRules = dataType !== 'boolean' && dataType !== 'coded'

  function setVal(i: number, key: keyof DraftValue, v: string): void {
    setNewValues((prev) => prev.map((row, idx) => (idx === i ? { ...row, [key]: v } : row)))
  }
  function resetDraft(): void {
    setFieldKey('')
    setLabel('')
    setUiType('text')
    setUiFormat('any')
    setRequired(false)
    setDefaultRaw('')
    setRules(EMPTY_RULES)
    setDomainMode('existing')
    setSelDomainId('')
    setNewDomainName('')
    setNewValues([{ code: '', label: '' }])
  }

  async function submit(): Promise<CustomFieldCreate | null> {
    if (!canAdd) return null
    setSubmitting(true)
    setError(null)
    try {
      let domainId: string | undefined
      if (dataType === 'coded') {
        if (mode === 'existing') {
          domainId = selDomainId
        } else {
          const created = await createCustomDomain(
            {
              name: newDomainName.trim(),
              values: newValues
                .filter((v) => v.code.trim() && v.label.trim())
                .map((v, i) => ({ code: v.code.trim(), label: v.label.trim(), sortOrder: i * 10 })),
            },
            accessToken,
          )
          domainId = created.id
          // Persisted org-wide now; flip to "use existing" + select it so a failed field-create retry reuses
          // THIS domain instead of orphaning a new one each time.
          setDomainMode('existing')
          setSelDomainId(created.id)
          setNewDomainName('')
          setNewValues([{ code: '', label: '' }])
          onDomainsChanged()
        }
      }
      let constraints = dataType === 'coded' || dataType === 'boolean' ? undefined : draftToConstraints(dataType, rules)
      if (dataType === 'text' && uiFormat !== 'any') {
        const pat = uiFormat === 'advanced'
          ? rules.patternCustom.trim() || undefined
          : PATTERN_PRESETS.find((p) => p.key === uiFormat)?.pattern
        if (pat) constraints = { ...(constraints ?? {}), pattern: pat }
      }
      const payload: CustomFieldCreate = {
        fieldKey,
        label: label.trim(),
        dataType,
        required,
        defaultValue: dataType === 'coded' || todayDefault ? undefined : coerceDefault(dataType, defaultRaw),
        sortOrder: nextSortOrder,
        defaultKind: todayDefault ? 'today' : 'constant',
        ...(constraints ? { constraints } : {}),
        ...(domainId ? { domainId } : {}),
        ...(numericSubtype ? { numericSubtype } : {}),
      }
      await onSubmit(payload)
      resetDraft()
      return payload
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Add failed')
      return null
    } finally {
      setSubmitting(false)
    }
  }

  // Expose submit to a parent (the wizard renders its own "+ Field" button in the bottom bar).
  useImperativeHandle(ref, () => ({
    submit,
    isDirty: () => fieldKey.trim() !== '' || label.trim() !== '',
  }))
  // Keep a parent-rendered button's enabled state in sync with this form's validity.
  useEffect(() => {
    onCanAddChange?.(canAdd)
  }, [canAdd, onCanAddChange])

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Add a field</h3>

      {/* ① Field Name (the immutable key) */}
      <label className="sign-form__field">
        <span>
          Field Name <span className="modal__muted">(snake_case, can’t change later)</span>
        </span>
        <input type="text" value={fieldKey} onChange={(e) => setFieldKey(e.target.value)} placeholder="ie. pipe_material" />
        {fieldKey !== '' && !keyValid && (
          <span className="modal__field-error">a lowercase letter, then letters / digits / underscore</span>
        )}
      </label>

      {/* ② Field Alias (the human label) */}
      <label className="sign-form__field">
        <span>Field Alias *</span>
        <input type="text" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="ie. Pipe Material" />
      </label>

      {/* ③ Type */}
      <label className="sign-form__field">
        <span>Type</span>
        <select
          value={uiType}
          onChange={(e) => {
            setUiType(e.target.value as UiType)
            setUiFormat('any')
          }}
        >
          {UI_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </label>

      {/* ④ Format (optional) */}
      <label className="sign-form__field">
        <span>
          Format <span className="modal__muted">(optional)</span>
        </span>
        <select value={uiFormat} onChange={(e) => setUiFormat(e.target.value)}>
          <option value="any">
            {uiType === 'text' ? '— plain text —' : uiType === 'date' ? '— plain date —' : '— plain number —'}
          </option>
          {uiType === 'text' &&
            PATTERN_PRESETS.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          {uiType === 'text' && <option value="advanced">Custom pattern (advanced)…</option>}
          {uiType !== 'date' && <option value="picklist">Picklist (coded values)</option>}
          {uiType !== 'date' && <option value="yesno">Yes / No</option>}
        </select>
      </label>

      {uiType === 'text' && uiFormat === 'advanced' && (
        <label className="sign-form__field">
          <span>
            Regular expression <span className="modal__muted">(anchored automatically)</span>
          </span>
          <input
            type="text"
            value={rules.patternCustom}
            placeholder="^[A-Z]{2}-\d{4}$"
            onChange={(e) => setRules((d) => ({ ...d, patternCustom: e.target.value }))}
          />
        </label>
      )}

      {dataType === 'coded' && (
        <div className="sign-form__field">
          <span>Picklist values *</span>
          {canCreateDomain ? (
            <div className="cf-domain__modes">
              <label>
                <input type="radio" name="domainmode" checked={domainMode === 'existing'} onChange={() => setDomainMode('existing')} /> Use existing
              </label>
              <label>
                <input type="radio" name="domainmode" checked={domainMode === 'new'} onChange={() => setDomainMode('new')} /> Create new
              </label>
            </div>
          ) : (
            <p className="modal__muted">
              Choose an existing picklist. A publisher or an administrator can add a new one on the Domains page.
            </p>
          )}
          {mode === 'existing' ? (
            <select value={selDomainId} onChange={(e) => setSelDomainId(e.target.value)}>
              <option value="">— choose a domain —</option>
              {domains.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.valueCount}){d.ownerName ? ` — ${d.ownerName}` : ''}
                </option>
              ))}
            </select>
          ) : (
            <>
              <input type="text" placeholder="Domain name (e.g. Condition)" value={newDomainName} onChange={(e) => setNewDomainName(e.target.value)} />
              {newValues.map((v, i) => (
                <div key={i} className="cf-domain__valrow">
                  <input type="text" placeholder="code" value={v.code} onChange={(e) => setVal(i, 'code', e.target.value)} />
                  <input type="text" placeholder="label" value={v.label} onChange={(e) => setVal(i, 'label', e.target.value)} />
                  <button
                    type="button"
                    className="btn btn--xs"
                    aria-label="Remove value"
                    disabled={newValues.length === 1}
                    onClick={() => setNewValues((prev) => prev.filter((_, idx) => idx !== i))}
                  >
                    <X size={13} aria-hidden="true" />
                  </button>
                </div>
              ))}
              <button type="button" className="btn btn--xs" onClick={() => setNewValues((prev) => [...prev, { code: '', label: '' }])}>
                + value
              </button>
            </>
          )}
        </div>
      )}

      {/* ⑤ Required — checkbox on its own line, normal size, inline with label */}
      <label className="sign-form__field">
        <span style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
          <input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} style={{ width: 'auto', margin: 0 }} />
          Required
        </span>
      </label>

      {/* ⑥ Default Value (optional) */}
      {dataType !== 'coded' && !todayDefault && (
        <label className="sign-form__field">
          <span>
            Default Value <span className="modal__muted">(optional)</span>
          </span>
          {dataType === 'boolean' ? (
            <select value={defaultRaw} onChange={(e) => setDefaultRaw(e.target.value)}>
              <option value="">— none —</option>
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          ) : (
            <input
              type={dataType === 'number' ? 'number' : dataType === 'date' ? 'date' : 'text'}
              value={defaultRaw}
              onChange={(e) => setDefaultRaw(e.target.value)}
            />
          )}
        </label>
      )}

      {/* ⑦ Min / Max (+ date "default to today"); text patterns live in Format above */}
      {typeHasRules && (
        <div className="sign-form__field">
          <span>Rules <span className="modal__muted">(optional)</span></span>
          <RuleInputs type={dataType} draft={rules} set={(patch) => setRules((d) => ({ ...d, ...patch }))} hidePattern />
        </div>
      )}

      {error !== null && <p className="sign-form__error">{error}</p>}
      {!hideSubmit && (
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" disabled={!canAdd} onClick={() => void submit()}>
            {working ? 'Saving…' : submitLabel}
          </button>
        </div>
      )}
    </section>
  )
})

export default function CustomFieldsSection({
  layer,
  accessToken,
  openImport = false,
}: {
  layer: MyLayer
  accessToken: string
  /** Auto-expand the schema import panel on mount (the create→import path from CreateLayerDialog). */
  openImport?: boolean
}) {
  const [fields, setFields] = useState<CustomField[] | null>(null)
  const [domains, setDomains] = useState<CustomDomain[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editingRules, setEditingRules] = useState<string | null>(null) // field id with its rules editor open
  const [editingContingency, setEditingContingency] = useState<string | null>(null) // field id with its dependency editor open
  const [importing, setImporting] = useState(openImport) // the import panel is open (seeded by the create→import path)
  const [gnssNote, setGnssNote] = useState<string | null>(null) // GNSS bundle result line (added/skipped)

  function refresh(): void {
    listCustomFields(layer.id, accessToken)
      .then((r) => setFields(r.fields))
      .catch((e) =>
        setLoadError(
          e instanceof ApiError ? `Couldn’t load fields (HTTP ${e.status})` : 'Couldn’t load fields',
        ),
      )
  }
  function refreshDomains(): Promise<void> {
    return listCustomDomains(accessToken)
      .then((d) => setDomains(d))
      .catch(() => {
        /* the picker just stays empty; field CRUD still works */
      })
  }
  useEffect(() => {
    refresh()
    void refreshDomains()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layer.id, accessToken])

  const domainName = (id: string | null): string =>
    domains.find((d) => d.id === id)?.name ?? '…'

  const nextSortOrder = (fields ?? []).reduce((m, f) => Math.max(m, f.sortOrder), 0) + 10

  async function toggleActive(f: CustomField): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await updateCustomField(layer.id, f.id, { active: !f.active }, accessToken)
      refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Update failed')
    } finally {
      setBusy(false)
    }
  }

  async function move(i: number, dir: -1 | 1): Promise<void> {
    if (!fields) return
    const j = i + dir
    if (j < 0 || j >= fields.length) return
    const a = fields[i]
    const b = fields[j]
    setBusy(true)
    setError(null)
    try {
      // Two non-atomic PATCHes: refresh in `finally` so a half-applied swap (first ok, second fails)
      // still reflects the real server order rather than a stale optimistic view.
      await updateCustomField(layer.id, a.id, { sortOrder: b.sortOrder }, accessToken)
      await updateCustomField(layer.id, b.id, { sortOrder: a.sortOrder }, accessToken)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Reorder failed')
    } finally {
      refresh()
      setBusy(false)
    }
  }

  async function remove(f: CustomField): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await deleteCustomField(layer.id, f.id, accessToken)
      refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Delete failed')
    } finally {
      setBusy(false)
    }
  }

  // One-click GNSS metadata bundle (decision-gnss-metadata-fields.md): create the MISSING bundle
  // fields only (skip-existing by key, retired included — a re-click is a clean no-op). Each create
  // is its own POST, so a mid-loop failure leaves a partial bundle the next click self-heals.
  async function addGnssBundle(): Promise<void> {
    if (!fields) return
    setBusy(true)
    setError(null)
    setGnssNote(null)
    const have = new Set(fields.map((f) => f.fieldKey))
    const missing = gnssBundleDrafts(nextSortOrder).filter((f) => !have.has(f.fieldKey))
    const skipped = GNSS_BUNDLE.length - missing.length
    let added = 0
    try {
      for (const f of missing) {
        await createCustomField(layer.id, f, accessToken)
        added += 1
      }
      setGnssNote(
        missing.length === 0
          ? 'All GNSS metadata fields already exist — nothing to add.'
          : `Added ${added} GNSS metadata field(s)${skipped > 0 ? `; ${skipped} already existed` : ''}.`,
      )
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'Create failed'
      setError(`GNSS bundle: added ${added} of ${missing.length} — ${msg}. Click again to add the rest.`)
    } finally {
      refresh()
      setBusy(false)
    }
  }

  async function downloadSchema(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      downloadText(`${layer.id}-schema.csv`, await exportCustomFieldsCsv(layer.id, accessToken))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Export failed')
    } finally {
      setBusy(false)
    }
  }

  async function downloadSchemaGpkg(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      downloadBlob(`${layer.id}-schema.gpkg`, await exportCustomFieldsGpkg(layer.id, accessToken))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Export failed')
    } finally {
      setBusy(false)
    }
  }

  if (loadError !== null) return <p className="sign-form__error">{loadError}</p>
  if (fields === null) return <p className="modal__muted">Loading fields…</p>

  return (
    <>
      <section className="modal__section">
        <details className="cf-moreinfo">
          <summary>More info</summary>
          <p className="modal__muted">
            Define custom attribute fields for this layer’s features. Editors fill them in when adding or
            editing a feature; values are validated on save. A <strong>Picklist</strong> field binds a
            reusable domain — pick an existing one or create a new one below. Add <strong>rules</strong>{' '}
            (a range, a length, a format) to reject bad values. Retiring a field hides it from the form but
            keeps existing values.
          </p>
        </details>
      </section>

      <section className="modal__section">
        <div className="cf-fields__head">
          <h3 className="modal__section-title">Fields</h3>
          <div className="cf-fields__head-actions">
            {fields.length > 0 && (
              <>
                <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void downloadSchema()}>
                  Export CSV
                </button>
                <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void downloadSchemaGpkg()}>
                  Export GeoPackage
                </button>
              </>
            )}
            <button type="button" className="btn btn--xs" disabled={busy} onClick={() => setImporting((v) => !v)}>
              {importing ? 'Close import' : 'Import from CSV'}
            </button>
            {layer.geomType !== 'none' && (
              <button
                type="button"
                className="btn btn--xs"
                disabled={busy}
                title="Add the GNSS provenance/QA set: accuracy, fix type, satellites, DOPs, receiver. Fields that already exist are skipped."
                onClick={() => void addGnssBundle()}
              >
                Add GNSS fields
              </button>
            )}
          </div>
        </div>
        {importing && (
          <ImportPanel
            layerId={layer.id}
            accessToken={accessToken}
            onClose={() => setImporting(false)}
            onImported={refresh}
          />
        )}
        {gnssNote !== null && <p className="modal__muted">{gnssNote}</p>}
        {error !== null && <p className="sign-form__error">{error}</p>}
        {fields.length === 0 ? (
          <p className="modal__muted">No custom fields yet.</p>
        ) : (
          <ul className="cf-fieldlist">
            {fields.map((f, i) => (
              <li key={f.id} className="cf-field" style={{ opacity: f.active ? 1 : 0.55 }}>
                <div className="cf-field__row">
                  <div className="cf-field__info">
                    <strong>{f.label}</strong>
                    {!f.active && <span className="modal__muted"> (retired)</span>}
                    <div className="modal__muted">
                      <code>{f.fieldKey}</code> ·{' '}
                      {f.dataType === 'coded' ? `picklist: ${domainName(f.domainId)}` : typeLabel(f)}
                      {f.required ? ' · required' : ''}
                      {f.defaultKind === 'today'
                        ? ' · default today'
                        : f.defaultValue !== null && f.defaultValue !== undefined
                          ? ` · default ${String(f.defaultValue)}`
                          : ''}
                      {ruleSummary(f)}
                      {f.parentFieldKey ? ` · depends on ${f.parentFieldKey}` : ''}
                    </div>
                  </div>
                  <div className="cf-field__actions">
                    <button type="button" className="btn btn--xs" disabled={busy || i === 0} onClick={() => void move(i, -1)} aria-label="Move up">
                      <ArrowUp size={12} aria-hidden="true" />
                    </button>
                    <button type="button" className="btn btn--xs" disabled={busy || i === fields.length - 1} onClick={() => void move(i, 1)} aria-label="Move down">
                      <ArrowDown size={12} aria-hidden="true" />
                    </button>
                    {f.dataType !== 'boolean' && f.dataType !== 'coded' && (
                      <button
                        type="button"
                        className="btn btn--xs"
                        disabled={busy}
                        onClick={() => setEditingRules((cur) => (cur === f.id ? null : f.id))}
                      >
                        Rules
                      </button>
                    )}
                    {f.dataType === 'coded' &&
                      fields.some((s) => s.id !== f.id && s.active && s.dataType === 'coded') && (
                        <button
                          type="button"
                          className="btn btn--xs"
                          disabled={busy}
                          onClick={() => setEditingContingency((cur) => (cur === f.id ? null : f.id))}
                        >
                          Depends on
                        </button>
                      )}
                    <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void toggleActive(f)}>
                      {f.active ? 'Retire' : 'Restore'}
                    </button>
                    <button type="button" className="btn btn--xs" disabled={busy} onClick={() => void remove(f)}>
                      Delete
                    </button>
                  </div>
                </div>
                {editingRules === f.id && (
                  <FieldRulesEditor
                    layerId={layer.id}
                    field={f}
                    accessToken={accessToken}
                    onClose={() => setEditingRules(null)}
                    onSaved={() => {
                      setEditingRules(null)
                      refresh()
                    }}
                  />
                )}
                {editingContingency === f.id && (
                  <FieldContingencyEditor
                    layerId={layer.id}
                    field={f}
                    siblings={fields.filter((s) => s.id !== f.id && s.active && s.dataType === 'coded')}
                    accessToken={accessToken}
                    onClose={() => setEditingContingency(null)}
                    onSaved={() => {
                      setEditingContingency(null)
                      refresh()
                    }}
                  />
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <AddFieldForm
        domains={domains}
        accessToken={accessToken}
        busy={busy}
        submitLabel="+ Field"
        nextSortOrder={nextSortOrder}
        onDomainsChanged={() => void refreshDomains()}
        onSubmit={async (payload) => {
          await createCustomField(layer.id, payload, accessToken)
          refresh()
        }}
      />
    </>
  )
}
