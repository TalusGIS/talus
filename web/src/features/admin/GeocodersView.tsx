import { useCallback, useEffect, useState } from 'react'
import { ArrowDown, ArrowUp, X } from 'lucide-react'
import { useAuth } from 'react-oidc-context'

import ConfirmDialog from '@/components/ConfirmDialog'
import {
  ApiError,
  createGeocoder,
  deleteGeocoder,
  fetchEligibleLocatorLayers,
  fetchGeocoders,
  fetchMappableFields,
  reorderGeocoders,
  testGeocoder,
  updateGeocoder,
  type EligibleLocatorLayer,
  type Geocoder,
  type GeocoderTestResult,
  type GeocoderType,
  type GeocoderWrite,
  type MappableField,
} from '@/lib/api'

/**
 * Admin → Geocoders (Talus 37). The in-portal replacement for editing the GEOCODERS env var +
 * restarting the api: list / add / edit / reorder / enable / remove the org's locators, plus a
 * non-blocking "Test" probe so a bad URL is caught before it silently breaks everyone's map search.
 * Locators are org-wide / shared-by-default; this list feeds the map's "Search in" picker via the
 * /geocode proxy. Every endpoint is admin-gated server-side; this screen sits behind RequireAdmin.
 *
 * Token locators (decision-geocoder-token-locators.md): an ArcGIS locator may carry a token,
 * WRITE-ONLY — the API never echoes it, so an edit shows "Token set (…last4)" with Replace/Clear
 * instead of the value. `tokenUnavailable` (server key loss) badges the row: re-enter the token.
 * URLs must be http/https (F4); the server re-validates.
 *
 * Mode B (decision-own-data-geocoder.md): type "Your address layer" geocodes against a hosted
 * point layer IN the portal's own database — no external service, works air-gapped. Its form swaps
 * the URL for a layer picker + an ordered address-field picker, and carries the deliberate-
 * disclosure note (§3 F3): everyone signed in can search those addresses.
 */
const TYPES: ReadonlyArray<{ value: GeocoderType; label: string }> = [
  { value: 'arcgis', label: 'ArcGIS GeocodeServer' },
  { value: 'nominatim', label: 'Nominatim (OSS)' },
  { value: 'internal', label: 'Your address layer (this portal)' },
]

interface FormState {
  name: string
  type: GeocoderType
  url: string
  outSR: string
  extent: string
  enabled: boolean
  layerId: string
  addressFields: string[]
  localityFields: string[]
  // Preserved through edit (PUT is a full replace — omitting it would reset sort_order to 0 and
  // silently reorder the org's fallback walk). Not shown in the form; reorder is the ↑↓ buttons.
  order: number
  // Write-only credential (arcgis only). `token` is the NEW value being typed ('' = untouched —
  // the server keeps what's stored); `clearToken` removes the stored one; `replacingToken` just
  // reveals the input when a stored token exists (the value itself never comes back from the API).
  token: string
  clearToken: boolean
  replacingToken: boolean
}

const EMPTY_FORM: FormState = {
  name: '',
  type: 'arcgis',
  url: '',
  outSR: '4326',
  extent: '',
  enabled: true,
  layerId: '',
  addressFields: [],
  localityFields: [],
  order: 0,
  token: '',
  clearToken: false,
  replacingToken: false,
}

function toForm(g: Geocoder): FormState {
  return {
    name: g.name,
    type: g.type,
    url: g.url ?? '',
    outSR: String(g.outSR),
    extent: g.extent ? g.extent.join(', ') : '',
    enabled: g.enabled,
    layerId: g.layerId ?? '',
    addressFields: g.addressFields ?? [],
    localityFields: g.localityFields ?? [],
    order: g.order,
    token: '',
    clearToken: false,
    replacingToken: false,
  }
}

/** Full-replace body from an existing row (enable toggle) — per-type shape, matching the server.
 * Carries EVERY row field the server stores (order, extent, …): a full replace that drops one
 * silently erases it. */
function writeFrom(g: Geocoder): GeocoderWrite {
  if (g.type === 'internal') {
    return {
      name: g.name, type: g.type, outSR: g.outSR, extent: g.extent, order: g.order,
      enabled: g.enabled, layerId: g.layerId, addressFields: g.addressFields,
      localityFields: g.localityFields,
    }
  }
  return {
    name: g.name, type: g.type, url: g.url, outSR: g.outSR, extent: g.extent,
    order: g.order, enabled: g.enabled,
  }
}

/** Parse the "xmin, ymin, xmax, ymax" field. '' → null; anything not exactly 4 numbers → 'invalid'. */
function parseExtent(s: string): number[] | null | 'invalid' {
  const t = s.trim()
  if (!t) return null
  const parts = t.split(',').map((x) => Number(x.trim()))
  if (parts.length !== 4 || parts.some((n) => Number.isNaN(n))) return 'invalid'
  return parts
}

export default function GeocodersView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [list, setList] = useState<Geocoder[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // The add/edit form: null = closed; { id: null } = adding; { id } = editing that row.
  const [form, setForm] = useState<FormState | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  // The row being edited — the token never round-trips, so its presence/hint/health render from here.
  const editingRow = editingId && list ? (list.find((g) => g.id === editingId) ?? null) : null
  const [formError, setFormError] = useState<string | null>(null)
  // Per-row + form test results (keyed by row id, or '__form__' for the unsaved form probe).
  const [tests, setTests] = useState<Record<string, GeocoderTestResult | 'running'>>({})
  // Internal-locator pickers (Mode B): eligible point layers, and the picked layer's fields.
  const [eligible, setEligible] = useState<EligibleLocatorLayer[] | null>(null)
  const [fields, setFields] = useState<MappableField[] | null>(null)

  const reload = useCallback(async () => {
    if (!token) return
    try {
      setList(await fetchGeocoders(token))
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load geocoders')
    }
  }, [token])

  useEffect(() => {
    void reload()
  }, [reload])

  // Internal-locator pickers (Mode B). The layer list loads lazily the first time the form shows
  // the internal type; the field list follows the picked layer.
  const formType = form?.type
  const formLayerId = form?.layerId
  useEffect(() => {
    if (!token || formType !== 'internal' || eligible !== null) return
    fetchEligibleLocatorLayers(token)
      .then(setEligible)
      .catch(() => setEligible([]))
  }, [token, formType, eligible])
  useEffect(() => {
    if (!token || formType !== 'internal' || !formLayerId) {
      setFields(null)
      return
    }
    let cancelled = false
    fetchMappableFields(formLayerId, token)
      .then((f) => {
        if (!cancelled) setFields(f)
      })
      .catch(() => {
        if (!cancelled) setFields([])
      })
    return () => {
      cancelled = true
    }
  }, [token, formType, formLayerId])

  const openAdd = () => {
    setForm(EMPTY_FORM)
    setEditingId(null)
    setFormError(null)
    setEligible(null) // refetch on open — a failed/early fetch must not cache an empty list
  }
  const openEdit = (g: Geocoder) => {
    setForm(toForm(g))
    setEditingId(g.id)
    setFormError(null)
    setEligible(null)
  }
  const closeForm = () => {
    setForm(null)
    setEditingId(null)
    setFormError(null)
  }

  /** Build the API body from the form, or set a form error and return null. */
  const formBody = (f: FormState): GeocoderWrite | null => {
    const name = f.name.trim()
    if (!name) {
      setFormError('Name is required.')
      return null
    }
    if (f.type === 'internal') {
      if (!f.layerId) {
        setFormError('Pick the address layer to search.')
        return null
      }
      if (f.addressFields.length === 0) {
        setFormError('Pick at least one address field.')
        return null
      }
      return {
        name, type: f.type, enabled: f.enabled, order: f.order,
        layerId: f.layerId, addressFields: f.addressFields, localityFields: f.localityFields,
      }
    }
    const url = f.url.trim()
    if (!/^https?:\/\//.test(url)) {
      setFormError('URL must start with http:// or https://')
      return null
    }
    const extent = parseExtent(f.extent)
    if (extent === 'invalid') {
      setFormError('Extent must be 4 comma-separated numbers: xmin, ymin, xmax, ymax')
      return null
    }
    const outSR = Number(f.outSR)
    if (!Number.isInteger(outSR)) {
      setFormError('Out SR must be a whole number (default 4326).')
      return null
    }
    const body: GeocoderWrite = { name, type: f.type, url, outSR, extent, enabled: f.enabled, order: f.order }
    // Write-only token (arcgis only): typed value → set/replace; clear → remove; neither → keep.
    if (f.type === 'arcgis') {
      const t = f.token.trim()
      if (t) body.token = t
      else if (f.clearToken) body.clearToken = true
    }
    return body
  }

  const submitForm = async () => {
    if (!form) return
    const body = formBody(form)
    if (!body) return
    setBusy(true)
    try {
      if (editingId) await updateGeocoder(editingId, body, token)
      else await createGeocoder(body, token)
      closeForm()
      await reload()
    } catch (e) {
      setFormError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  const toggleEnabled = async (g: Geocoder) => {
    setBusy(true)
    try {
      await updateGeocoder(g.id, { ...writeFrom(g), enabled: !g.enabled }, token)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Update failed')
    } finally {
      setBusy(false)
    }
  }

  // The geocoder awaiting its remove ConfirmDialog (§5).
  const [confirmRemove, setConfirmRemove] = useState<Geocoder | null>(null)

  const remove = async (g: Geocoder) => {
    setBusy(true)
    try {
      await deleteGeocoder(g.id, token)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Delete failed')
    } finally {
      setBusy(false)
      setConfirmRemove(null)
    }
  }

  /** Move a row up/down by swapping with its neighbor, then persist the new order. */
  const move = async (index: number, delta: number) => {
    if (!list) return
    const next = [...list]
    const j = index + delta
    if (j < 0 || j >= next.length) return
    ;[next[index], next[j]] = [next[j], next[index]]
    setList(next) // optimistic
    setBusy(true)
    try {
      await reorderGeocoders(next.map((g) => g.id), token)
      await reload()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Reorder failed')
      await reload()
    } finally {
      setBusy(false)
    }
  }

  const runTest = async (key: string, body: Parameters<typeof testGeocoder>[0]) => {
    setTests((t) => ({ ...t, [key]: 'running' }))
    try {
      const result = await testGeocoder(body, token)
      setTests((t) => ({ ...t, [key]: result }))
    } catch (e) {
      setTests((t) => ({
        ...t,
        [key]: { ok: false, sampleCount: 0, error: e instanceof ApiError ? e.message : 'Test failed' },
      }))
    }
  }

  const renderTest = (r: GeocoderTestResult | 'running' | undefined) => {
    if (!r) return null
    if (r === 'running') return <span className="sa__muted"> testing…</span>
    return r.ok ? (
      <span className="sa-badge sa-badge--on"> ✓ {r.sampleCount} result{r.sampleCount === 1 ? '' : 's'}</span>
    ) : (
      <span className="sa-badge sa-badge--off" title={r.error ?? ''}> ✕ no response</span>
    )
  }

  return (
    <div className="admin">
      <h1 className="admin__title">Geocoders</h1>
      <p className="admin__note">
        The address-search locators your organization uses on the map. These are shared by everyone —
        adding one here replaces editing the <code>GEOCODERS</code> setting and restarting the server.
        Order sets the search fallback (top locator tried first). Disabled locators are hidden from the
        map’s “Search in” picker.
      </p>

      {error && <p className="sa__error" role="alert">{error}</p>}

      {!form && (
        <div className="sign-form__actions">
          <button type="button" className="btn btn--sm" onClick={openAdd} disabled={busy}>
            + Add geocoder
          </button>
        </div>
      )}

      {form && (
        <div className="org-settings__card">
          <h3 className="org-settings__heading">{editingId ? 'Edit geocoder' : 'Add geocoder'}</h3>
          <label className="sign-form__field">
            <span>Name</span>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="e.g. county-streets"
            />
          </label>
          <label className="sign-form__field">
            <span>Type</span>
            <select
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value as GeocoderType })}
            >
              {TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </label>
          {form.type !== 'internal' && (
            <>
              <label className="sign-form__field">
                <span>Service URL</span>
                <input
                  value={form.url}
                  onChange={(e) => setForm({ ...form, url: e.target.value })}
                  placeholder="https://…/GeocodeServer"
                />
              </label>
              <label className="sign-form__field">
                <span>Out SR (EPSG)</span>
                <input
                  value={form.outSR}
                  onChange={(e) => setForm({ ...form, outSR: e.target.value })}
                  inputMode="numeric"
                />
              </label>
              <label className="sign-form__field">
                <span>Limit to extent (optional)</span>
                <input
                  value={form.extent}
                  onChange={(e) => setForm({ ...form, extent: e.target.value })}
                  placeholder="xmin, ymin, xmax, ymax (WGS84)"
                />
              </label>
              {form.type === 'arcgis' && (
                <div className="sign-form__field">
                  <span>Token (optional — for a secured service)</span>
                  {editingRow?.hasToken && !form.clearToken && !form.replacingToken ? (
                    <p>
                      <span className="sa-badge sa-badge--on">
                        Token set{editingRow.tokenHint ? ` (…${editingRow.tokenHint})` : ''}
                      </span>{' '}
                      {editingRow.tokenUnavailable && (
                        <span
                          className="sa-badge sa-badge--off"
                          title="The server's encryption key can't open the stored token (key changed?). Re-enter it."
                        >
                          credential unavailable — re-enter it
                        </span>
                      )}{' '}
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => setForm({ ...form, replacingToken: true })}
                      >
                        Replace
                      </button>{' '}
                      <button
                        type="button"
                        className="btn btn--sm btn--danger"
                        onClick={() => setForm({ ...form, clearToken: true, token: '', replacingToken: false })}
                      >
                        Clear
                      </button>
                    </p>
                  ) : form.clearToken ? (
                    <p>
                      <span className="sa-badge sa-badge--off">Token will be removed when you save</span>{' '}
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => setForm({ ...form, clearToken: false })}
                      >
                        Undo
                      </button>
                    </p>
                  ) : (
                    <input
                      type="password"
                      autoComplete="off"
                      value={form.token}
                      onChange={(e) => setForm({ ...form, token: e.target.value })}
                      placeholder="paste the service token"
                    />
                  )}
                  <p className="sa__muted">
                    Stored encrypted on the server and never shown again — only that a token is set
                    (and its last 4 characters).
                  </p>
                </div>
              )}
            </>
          )}
          {form.type === 'internal' && (
            <>
              <label className="sign-form__field">
                <span>Address layer</span>
                <select
                  value={form.layerId}
                  onChange={(e) => setForm({ ...form, layerId: e.target.value, addressFields: [], localityFields: [] })}
                >
                  <option value="">— pick a point layer —</option>
                  {(eligible ?? []).map((l) => (
                    <option key={l.layerId} value={l.layerId}>{l.displayName}</option>
                  ))}
                </select>
              </label>
              {eligible !== null && eligible.length === 0 && (
                <p className="sa__muted">No eligible point layers yet — load your address points first.</p>
              )}
              {form.layerId && (
                <div className="sign-form__field">
                  <span>Address fields (in display order)</span>
                  {form.addressFields.length > 0 && (
                    <p>
                      {form.addressFields.map((k, i) => (
                        <span key={k} className="sa-badge" style={{ marginRight: '0.35rem' }}>
                          {i + 1}. {fields?.find((f) => f.key === k)?.label ?? k}{' '}
                          <button
                            type="button"
                            className="btn btn--sm"
                            aria-label={`Remove ${k}`}
                            onClick={() =>
                              setForm({ ...form, addressFields: form.addressFields.filter((x) => x !== k) })
                            }
                          >
                            <X size={13} aria-hidden="true" />
                          </button>
                        </span>
                      ))}
                    </p>
                  )}
                  <select
                    value=""
                    onChange={(e) => {
                      const key = e.target.value
                      if (key) setForm({ ...form, addressFields: [...form.addressFields, key] })
                    }}
                  >
                    <option value="">+ Add field…</option>
                    {(fields ?? [])
                      .filter((f) => !form.addressFields.includes(f.key) && !form.localityFields.includes(f.key))
                      .map((f) => (
                        <option key={f.key} value={f.key}>{f.label}</option>
                      ))}
                  </select>
                </div>
              )}
              {form.layerId && (
                <div className="sign-form__field">
                  <span>Locality fields (city, ZIP — optional)</span>
                  {form.localityFields.length > 0 && (
                    <p>
                      {form.localityFields.map((k, i) => (
                        <span key={k} className="sa-badge" style={{ marginRight: '0.35rem' }}>
                          {i + 1}. {fields?.find((f) => f.key === k)?.label ?? k}{' '}
                          <button
                            type="button"
                            className="btn btn--sm"
                            aria-label={`Remove ${k}`}
                            onClick={() =>
                              setForm({ ...form, localityFields: form.localityFields.filter((x) => x !== k) })
                            }
                          >
                            <X size={13} aria-hidden="true" />
                          </button>
                        </span>
                      ))}
                    </p>
                  )}
                  <select
                    value=""
                    onChange={(e) => {
                      const key = e.target.value
                      if (key) setForm({ ...form, localityFields: [...form.localityFields, key] })
                    }}
                  >
                    <option value="">+ Add locality field…</option>
                    {(fields ?? [])
                      .filter((f) => !form.addressFields.includes(f.key) && !form.localityFields.includes(f.key))
                      .map((f) => (
                        <option key={f.key} value={f.key}>{f.label}</option>
                      ))}
                  </select>
                </div>
              )}
              <p className="admin__note">
                <strong>Address fields</strong> are the street address — house number and street, in
                order, with the house number first (e.g. <em>address</em>, or <em>number</em> +
                <em>street</em>). They are what a search is matched and scored against.{' '}
                <strong>Locality fields</strong> (city, ZIP) are shown in the result and break ties
                between the same street address in two towns, but never lower a score — leave them out of
                the address fields, or a county-wide locator scores every correct match too low.{' '}
                <strong>Heads-up:</strong> locators are org-wide —
                every signed-in member will be able to search and see these addresses, regardless of
                the layer’s sharing settings.
              </p>
            </>
          )}
          <label className="sa-check">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            />
            <span>Enabled</span>
          </label>

          {formError && <p className="sign-form__error" role="alert">{formError}</p>}

          <div className="sign-form__actions">
            <button type="button" className="btn btn--sm" onClick={submitForm} disabled={busy}>
              {editingId ? 'Save changes' : 'Add'}
            </button>
            <button
              type="button"
              className="btn btn--sm"
              disabled={busy}
              onClick={() => {
                const body = formBody(form)
                if (!body) return
                if (body.type === 'internal') {
                  void runTest('__form__', {
                    type: 'internal',
                    layerId: body.layerId ?? '',
                    addressFields: body.addressFields ?? [],
                    localityFields: body.localityFields ?? [],
                  })
                } else if (!body.token && editingRow?.hasToken && !form.clearToken) {
                  // The stored token never reaches the browser, so an ad-hoc probe can't carry it —
                  // probe the SAVED definition by id instead (unsaved URL edits aren't exercised).
                  void runTest('__form__', { id: editingRow.id })
                } else {
                  void runTest('__form__', {
                    type: body.type, url: body.url ?? '', outSR: body.outSR, extent: body.extent,
                    // Transient probe-before-save credential — used once, never stored.
                    ...(body.token ? { token: body.token } : {}),
                  })
                }
              }}
            >
              Test
            </button>
            <button type="button" className="btn btn--sm" onClick={closeForm} disabled={busy}>
              Cancel
            </button>
            {renderTest(tests['__form__'])}
          </div>
        </div>
      )}

      {list && list.length === 0 && (
        <p className="sa__muted">No geocoders configured yet — address search is off until you add one.</p>
      )}

      {list && list.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Order</th>
              <th>Name</th>
              <th>Type</th>
              <th>Source</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {list.map((g, i) => (
              <tr key={g.id}>
                <td>
                  <button type="button" className="btn btn--sm" disabled={busy || i === 0} onClick={() => move(i, -1)} aria-label="Move up"><ArrowUp size={13} aria-hidden="true" /></button>
                  <button type="button" className="btn btn--sm" disabled={busy || i === list.length - 1} onClick={() => move(i, 1)} aria-label="Move down"><ArrowDown size={13} aria-hidden="true" /></button>
                </td>
                <td>{g.name}</td>
                <td>{g.type}</td>
                <td
                  className="sa__muted"
                  style={{ maxWidth: '22rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  title={g.type === 'internal' ? `${g.layerId ?? ''}: ${(g.addressFields ?? []).join(' + ')}${g.localityFields?.length ? ` · ${g.localityFields.join(' + ')}` : ''}` : g.url ?? ''}
                >
                  {g.type === 'internal'
                    ? `${g.layerId ?? ''} · ${(g.addressFields ?? []).join(' + ')}${g.localityFields?.length ? ` · ${g.localityFields.join(' + ')}` : ''}`
                    : g.url}
                </td>
                <td>
                  <button
                    type="button"
                    className={`sa-badge ${g.enabled ? 'sa-badge--on' : 'sa-badge--off'}`}
                    onClick={() => toggleEnabled(g)}
                    disabled={busy}
                    title="Toggle enabled"
                  >
                    {g.enabled ? 'Enabled' : 'Disabled'}
                  </button>
                  {g.hasToken && (
                    g.tokenUnavailable ? (
                      <span
                        className="sa-badge sa-badge--off"
                        style={{ marginLeft: '0.35rem' }}
                        title="The server's encryption key can't open the stored token (key changed?). Edit and re-enter it."
                      >
                        credential unavailable
                      </span>
                    ) : (
                      <span
                        className="sa-badge"
                        style={{ marginLeft: '0.35rem' }}
                        title="A service token is stored (encrypted) for this locator"
                      >
                        token{g.tokenHint ? ` …${g.tokenHint}` : ''}
                      </span>
                    )
                  )}
                </td>
                <td className="sa-actions">
                  <button type="button" className="btn btn--sm" disabled={busy} onClick={() => void runTest(g.id, { id: g.id })}>Test</button>
                  <button type="button" className="btn btn--sm" disabled={busy} onClick={() => openEdit(g)}>Edit</button>
                  <button type="button" className="btn btn--sm btn--danger" disabled={busy} onClick={() => setConfirmRemove(g)}>Remove</button>
                  {renderTest(tests[g.id])}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {confirmRemove !== null && (
        <ConfirmDialog
          title={`Remove the "${confirmRemove.name}" geocoder?`}
          confirmLabel="Remove"
          tone="danger"
          busy={busy}
          onConfirm={() => void remove(confirmRemove)}
          onCancel={() => setConfirmRemove(null)}
          consequence="This affects everyone's map search."
        />
      )}
    </div>
  )
}
