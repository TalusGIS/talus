/**
 * The slot feature form's MODEL — everything about a layer's owner-defined fields that is not
 * chrome (decision-talus-field-f1.md §4.3.4, fork 2): seeding from a feature or the defaults,
 * coercing inputs back to JSON, the client-side constraint mirror, the V077 contingency filter,
 * and value assembly. Extracted VERBATIM from SlotForm.tsx so the portal's desktop form and the
 * Talus+ field app's phone form run one set of rules — the contingency rules in particular are
 * exactly the kind of logic that drifts between two copies. The server stays authoritative
 * (layer_props.py + the V044/V046/V048/V077 triggers); this is feedback, not enforcement.
 */
import { ApiError, fetchLayerForm, getCustomDomain, listCustomFields } from '@/lib/api'
import type { CustomField, DomainValue, FormCondition, FormConfig } from '@/lib/api'

export type Val = string | boolean

/** Local YYYY-MM-DD (the 'today' dynamic default; mirrors the trigger's CURRENT_DATE). */
export function todayISO(): string {
  const d = new Date()
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** Seed a field's input from the feature's existing value, else the field's default. A 'today'-kind date
 * field with no existing value pre-fills today — the form sends every field explicitly, so this is what
 * actually realises the dynamic default (the trigger only fills genuinely-absent keys). */
export function seed(field: CustomField, existing: unknown): Val {
  if (field.dataType === 'boolean') {
    const raw = existing !== undefined ? existing : field.defaultValue
    return raw === true || raw === 'true'
  }
  if (existing !== undefined && existing !== null) return String(existing)
  if (field.dataType === 'date' && field.defaultKind === 'today') return todayISO()
  const raw = field.defaultValue
  return raw === null || raw === undefined ? '' : String(raw)
}

/** Coerce a field's input back into the JSON value the API expects ('' → null; checkbox → boolean). */
export function coerce(field: CustomField, v: Val): unknown {
  if (field.dataType === 'boolean') return Boolean(v)
  const s = v as string
  if (s === '') return null
  if (field.dataType === 'number') return Number(s)
  return s // text + date ride as strings
}

/** Client-side constraint feedback (range/length/pattern). Mirrors layer_props.py + the V048 trigger;
 * the server stays authoritative. Returns a friendly message or null. Empty values defer to `required`. */
export function fieldError(field: CustomField, v: Val): string | null {
  if (field.dataType === 'boolean' || field.dataType === 'coded') return null
  const s = (v as string) ?? ''
  if (s === '') return null
  const c = field.constraints
  if (!c) return null
  if (field.dataType === 'number') {
    const n = Number(s)
    if (Number.isNaN(n)) return null // a non-number is the server/type concern, not a range message
    if (c.min !== undefined && n < Number(c.min)) return `Must be at least ${c.min}`
    if (c.max !== undefined && n > Number(c.max)) return `Must be at most ${c.max}`
  } else if (field.dataType === 'date') {
    // ISO YYYY-MM-DD compares correctly as strings (zero-padded).
    if (c.min !== undefined && s < String(c.min)) return `Must be on or after ${c.min}`
    if (c.max !== undefined && s > String(c.max)) return `Must be on or before ${c.max}`
  } else if (field.dataType === 'text') {
    if (c.minLength !== undefined && s.length < c.minLength) return `Must be at least ${c.minLength} characters`
    if (c.maxLength !== undefined && s.length > c.maxLength) return `Must be at most ${c.maxLength} characters`
    if (c.pattern) {
      try {
        if (!new RegExp(c.pattern).test(s)) return 'Does not match the required format'
      } catch {
        /* a non-JS-compilable pattern still fails closed at the trigger */
      }
    }
  }
  return null
}

/** The layer's ACTIVE fields + each coded field's active domain values (deduped by domain), in one
 *  call — what every form needs before it can render. A domain that fails to load yields []. */
export async function loadFormFields(
  layerId: string,
  accessToken: string,
): Promise<{ fields: CustomField[]; domainValues: Record<string, DomainValue[]>; formConfig: FormConfig | null }> {
  // The form document rides along (f2): one extra GET, and a missing/failed one is the default form.
  const [r, formConfig] = await Promise.all([
    listCustomFields(layerId, accessToken),
    fetchLayerForm(layerId, accessToken).catch(() => null),
  ])
  const fields = r.fields.filter((f) => f.active)
  const domainIds = [
    ...new Set(fields.filter((f) => f.dataType === 'coded' && f.domainId).map((f) => f.domainId as string)),
  ]
  const pairs = await Promise.all(
    domainIds.map((id) =>
      getCustomDomain(id, accessToken)
        .then((d) => [id, d.values.filter((v) => v.active)] as const)
        .catch(() => [id, [] as DomainValue[]] as const),
    ),
  )
  return { fields, domainValues: Object.fromEntries(pairs), formConfig }
}

/** A friendly message for a failed loadFormFields. */
export function loadFormFieldsError(e: unknown): string {
  return e instanceof ApiError ? `Couldn’t load custom fields (HTTP ${e.status})` : 'Couldn’t load custom fields'
}

/** Seed every active field from the feature's props (edit) or the defaults (create). */
export function seedValues(fields: CustomField[], props: Record<string, unknown> | undefined): Record<string, Val> {
  const seeded: Record<string, Val> = {}
  for (const f of fields) seeded[f.fieldKey] = seed(f, props?.[f.fieldKey])
  return seeded
}

/** What a coded field may offer right now, and how its current value stands against the rules. */
export interface CodedState {
  /** The options to offer: the domain's active values, narrowed by the parent's value when mapped. */
  opts: DomainValue[]
  /** The current value is one the contingency forbids under the parent's current value. Keep it
   *  selectable + flagged, never silently clear it: the pair only binds server-side when either side
   *  actually CHANGES, so e.g. a notes-only edit of a legacy row still saves. */
  pairInvalid: boolean
  /** The current value is a now-RETIRED code: keep it selectable so it shows + survives. */
  showRetired: boolean
  parentField: CustomField | undefined
  parentVal: string
}

/** Contingency (V077): with a parent set AND mapped, offer only the allowed codes. An empty or
 *  unmapped parent value (or a retired/absent parent field) filters nothing — fork 3 permissive. */
export function codedState(
  f: CustomField,
  values: Record<string, Val>,
  domainValues: Record<string, DomainValue[]>,
  fields: CustomField[],
): CodedState {
  const cur = (values[f.fieldKey] as string) ?? ''
  const allOpts = f.dataType === 'coded' ? (domainValues[f.domainId ?? ''] ?? []) : []
  const parentVal =
    f.parentFieldKey !== null && f.parentFieldKey !== undefined ? String(values[f.parentFieldKey] ?? '') : ''
  const allowed = f.contingentValues && parentVal !== '' ? f.contingentValues[parentVal] : undefined
  const opts = allowed ? allOpts.filter((v) => allowed.includes(v.code)) : allOpts
  const showRetired = f.dataType === 'coded' && cur !== '' && !allOpts.some((v) => v.code === cur)
  const pairInvalid = allowed !== undefined && cur !== '' && !allowed.includes(cur)
  const parentField = f.parentFieldKey ? fields.find((x) => x.fieldKey === f.parentFieldKey) : undefined
  return { opts, pairInvalid, showRetired, parentField, parentVal }
}

// --- the form document (decision-talus-field-f2.md §4.1) — the closed vocabulary, evaluated here ONCE ---
// The trigger (V104) evaluates `requiredIf` in SQL with the same four operators; this is the client's
// mirror so the crew gets inline feedback before a round trip. `visibleIf` and `readOnly` and groups are
// presentation and live only here.

/** Evaluate ONE condition against the form's current values. Values are the form's own (strings for
 *  text/coded/number/date, booleans for boolean); a boolean `eq` compares booleans, else strings. */
export function evalCondition(cond: FormCondition, values: Record<string, Val>): boolean {
  const v = values[cond.field]
  if ('eq' in cond) {
    if (typeof cond.eq === 'boolean') return v === cond.eq
    return v !== undefined && v !== true && v !== false && String(v) === cond.eq
  }
  if ('in' in cond) return typeof v === 'string' && v !== '' && cond.in.includes(v)
  if ('isEmpty' in cond) return v === undefined || v === '' || v === false
  return !(v === undefined || v === '' || v === false)
}

export interface FormLayout {
  /** Ordered groups; a trailing "Other" holds every active field the config did not place (§4.1: unlisted
   *  fields are shown, never hidden). A config with no groups yields one untitled group. */
  groups: { title: string | null; fields: CustomField[]; collapsed: boolean }[]
  hidden: Set<string>
  conditionallyRequired: Set<string>
  readOnly: Set<string>
}

/** Apply the document to the active fields + current values → what to draw and what is required NOW. */
export function applyFormRules(
  config: FormConfig | null | undefined,
  fields: CustomField[],
  values: Record<string, Val>,
): FormLayout {
  const byKey = new Map(fields.map((f) => [f.fieldKey, f]))
  const hidden = new Set<string>()
  const conditionallyRequired = new Set<string>()
  const readOnly = new Set<string>()
  for (const [key, rules] of Object.entries(config?.fields ?? {})) {
    if (!byKey.has(key)) continue
    if (rules.visibleIf && !evalCondition(rules.visibleIf, values)) hidden.add(key)
    if (rules.requiredIf && evalCondition(rules.requiredIf, values)) conditionallyRequired.add(key)
    if (rules.readOnly) readOnly.add(key)
  }
  const placed = new Set<string>()
  const groups: FormLayout['groups'] = []
  for (const g of config?.groups ?? []) {
    const members = g.fields.map((k) => byKey.get(k)).filter((f): f is CustomField => !!f && !placed.has(f.fieldKey))
    for (const f of members) placed.add(f.fieldKey)
    if (members.length) groups.push({ title: g.title, fields: members, collapsed: !!g.collapsed })
  }
  const rest = fields.filter((f) => !placed.has(f.fieldKey))
  if (rest.length) groups.push({ title: groups.length ? 'Other' : null, fields: rest, collapsed: false })
  return { groups, hidden, conditionallyRequired, readOnly }
}

/** Block save when a required (static, or conditional-and-firing), VISIBLE, non-boolean field is empty, or
 *  any visible value violates its constraints (the server also enforces all of it). Hidden fields are not
 *  judged: they are not sent (assembleProps), and the server's static `required` can never hide (the form
 *  door refuses that document). */
export function blocksSave(
  fields: CustomField[],
  values: Record<string, Val>,
  layout?: Pick<FormLayout, 'hidden' | 'conditionallyRequired'>,
): boolean {
  const hidden = layout?.hidden ?? new Set<string>()
  const cond = layout?.conditionallyRequired ?? new Set<string>()
  const missingRequired = fields.some(
    (f) =>
      !hidden.has(f.fieldKey) &&
      (f.required || cond.has(f.fieldKey)) &&
      f.dataType !== 'boolean' &&
      (values[f.fieldKey] ?? '') === '',
  )
  const hasConstraintError = fields.some(
    (f) => !hidden.has(f.fieldKey) && fieldError(f, values[f.fieldKey] ?? '') !== null,
  )
  return missingRequired || hasConstraintError
}

/** The `props` object a save sends: every VISIBLE active field, coerced. A hidden field is omitted —
 *  on edit the row keeps its stored value (PATCH props is a partial merge), on create the trigger's
 *  defaults apply. */
export function assembleProps(
  fields: CustomField[],
  values: Record<string, Val>,
  hidden: Set<string> = new Set(),
): Record<string, unknown> {
  const props: Record<string, unknown> = {}
  for (const f of fields) if (!hidden.has(f.fieldKey)) props[f.fieldKey] = coerce(f, values[f.fieldKey] ?? '')
  return props
}
