import { useEffect, useState } from 'react'
import { ArrowDown, ArrowUp } from 'lucide-react'
import { useAuth } from 'react-oidc-context'

import { Highlight } from '@/lib/highlight'

import {
  ApiError,
  addDomainValue,
  createCustomDomain,
  deleteCustomDomain,
  deleteDomainValue,
  getCustomDomain,
  getDomainUsage,
  listCustomDomains,
  updateDomainValue,
} from '@/lib/api'
import type { CustomDomain, DomainBinding, DomainValue } from '@/lib/api'
import Caret from '@/components/Caret'

/**
 * The org-wide **Domains** management page (data-owner-independence arc, slice 2b). Reusable coded-value
 * picklists ("domains") are define-once / bind-everywhere; this is where a data owner manages them at
 * scale. Anyone may browse (org-wide reuse); only a domain's owner or an admin may edit it
 * (`canEdit`, server-authoritative — the API re-enforces). Each domain shows its value count + how many
 * fields bind it ("where used"); values support add / relabel / retire-restore / reorder / delete, with
 * the server's guards surfaced (a value in use, or a bound domain, can't be hard-deleted — retire/unbind).
 *
 * Refreshed (06-main-pages.md): the shared page-header + a NEW search that matches on domain NAME or
 * any VALUE (label/code) and auto-expands every match. Value lists load lazily per card, so value
 * search uses a LAZY-EAGER strategy: the first time you type in the box, every domain's
 * values are batch-fetched once and cached — a normal visit stays a single list request.
 */
function SearchGlyph() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  )
}

export default function DomainsView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [domains, setDomains] = useState<CustomDomain[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [showNew, setShowNew] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [query, setQuery] = useState('')

  // Lazy-eager value cache for value-search: populated once on first search interaction.
  const [valuesById, setValuesById] = useState<Record<string, DomainValue[]>>({})
  const [valuesLoaded, setValuesLoaded] = useState(false)
  const [valuesLoading, setValuesLoading] = useState(false)

  function refresh(): void {
    listCustomDomains(token)
      .then((d) => {
        setDomains(d)
        // A create/delete may have changed the set; drop the value cache so a later search re-batches.
        setValuesById({})
        setValuesLoaded(false)
      })
      .catch((e) =>
        setLoadError(e instanceof ApiError ? `Couldn’t load domains (HTTP ${e.status})` : 'Couldn’t load domains'),
      )
  }
  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  async function loadAllValues(list: CustomDomain[]): Promise<void> {
    setValuesLoading(true)
    const entries = await Promise.all(
      list.map(async (d) => {
        try {
          const full = await getCustomDomain(d.id, token)
          return [d.id, full.values] as const
        } catch {
          return [d.id, [] as DomainValue[]] as const
        }
      }),
    )
    setValuesById(Object.fromEntries(entries))
    setValuesLoaded(true)
    setValuesLoading(false)
  }

  function onQuery(next: string): void {
    setQuery(next)
    // First real search keystroke → batch-fetch every domain's values, once.
    if (next.trim() !== '' && !valuesLoaded && !valuesLoading && domains) void loadAllValues(domains)
  }

  async function createDomain(): Promise<void> {
    if (newName.trim() === '') return
    setBusy(true)
    setError(null)
    try {
      await createCustomDomain({ name: newName.trim(), description: newDesc.trim() || null }, token)
      setNewName('')
      setNewDesc('')
      setShowNew(false)
      refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  if (loadError !== null) return <div className="mp-page"><p className="sign-form__error">{loadError}</p></div>

  const q = query.trim().toLowerCase()
  const matched =
    domains === null
      ? null
      : !q
        ? domains
        : domains.filter(
            (d) =>
              d.name.toLowerCase().includes(q) ||
              (valuesById[d.id]?.some(
                (v) => v.label.toLowerCase().includes(q) || v.code.toLowerCase().includes(q),
              ) ??
                false),
          )

  return (
    <div className="mp-page">
      <div className="mp-head">
        <div>
          <p className="eyebrow">Data model</p>
          <h1 className="mp-title">Domains</h1>
        </div>
        <button type="button" className="btn btn--sm" onClick={() => setShowNew((s) => !s)} disabled={busy}>
          {showNew ? 'Cancel' : '+ New domain'}
        </button>
      </div>
      <p className="mp-intro">
        Reusable picklists. Define a domain once and bind it to fields on any layer — editing it updates
        everywhere it’s used. You can edit the domains you own; anyone can reuse them.
      </p>

      {showNew && (
        <div className="grp-newform">
          <h3 className="grp-newform__title">New domain</h3>
          <label className="grp-newform__field">
            <span>Name *</span>
            <input className="field" type="text" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Condition" />
          </label>
          <label className="grp-newform__field">
            <span>Description <span className="grp-newform__opt">(optional)</span></span>
            <input className="field" type="text" value={newDesc} onChange={(e) => setNewDesc(e.target.value)} />
          </label>
          {error !== null && <p className="sign-form__error">{error}</p>}
          <div>
            <button type="button" className="btn btn--sm" disabled={busy || newName.trim() === ''} onClick={() => void createDomain()}>
              {busy ? 'Saving…' : 'Create domain'}
            </button>
          </div>
        </div>
      )}

      <div className="mp-search dm-search">
        <span className="mp-search__icon">
          <SearchGlyph />
        </span>
        <input
          className="field"
          type="search"
          placeholder="Search by domain name or value…"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          aria-label="Search domains by name or value"
        />
        {valuesLoading && <span className="dm-search__hint">Loading values…</span>}
      </div>

      {matched === null ? (
        <p className="mp-intro" style={{ margin: 0 }}>Loading…</p>
      ) : matched.length === 0 ? (
        <p className="dm-empty">{q ? `No domains matching “${query.trim()}”.` : 'No domains yet.'}</p>
      ) : (
        <div className="dm-list">
          {matched.map((d) => (
            <DomainCard
              key={d.id}
              domain={d}
              token={token}
              onChanged={refresh}
              forceOpen={q !== ''}
              preloadedValues={valuesById[d.id]}
              query={query}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function DomainCard({
  domain,
  token,
  onChanged,
  forceOpen = false,
  preloadedValues,
  query = '',
}: {
  domain: CustomDomain
  token: string
  onChanged: () => void
  /** Search auto-expands every match. */
  forceOpen?: boolean
  /** Values from the parent's lazy-eager batch — avoids a redundant per-card fetch on auto-expand. */
  preloadedValues?: DomainValue[]
  /** Active search text — used to highlight the matching name / value substrings. */
  query?: string
}) {
  const [openState, setOpenState] = useState(false)
  const [values, setValues] = useState<DomainValue[] | null>(null)
  const [usage, setUsage] = useState<DomainBinding[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [newCode, setNewCode] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const canEdit = domain.canEdit === true
  const open = forceOpen || openState
  const shownValues = values ?? preloadedValues ?? null

  function load(): void {
    getCustomDomain(domain.id, token).then((d) => setValues(d.values)).catch(() => setValues([]))
    getDomainUsage(domain.id, token).then(setUsage).catch(() => setUsage([]))
  }

  // Fetch on open. Values come free from the parent batch when searching; otherwise fetch them.
  useEffect(() => {
    if (!open) return
    if (values === null && !preloadedValues) {
      getCustomDomain(domain.id, token).then((d) => setValues(d.values)).catch(() => setValues([]))
    }
    if (usage === null) getDomainUsage(domain.id, token).then(setUsage).catch(() => setUsage([]))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, preloadedValues])

  async function run(fn: () => Promise<unknown>): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await fn()
      load()
      onChanged() // refresh the list so value/field counts stay accurate
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Action failed')
    } finally {
      setBusy(false)
    }
  }

  function addValue(): void {
    if (newCode.trim() === '' || newLabel.trim() === '') return
    void run(async () => {
      await addDomainValue(domain.id, { code: newCode.trim(), label: newLabel.trim() }, token)
      setNewCode('')
      setNewLabel('')
    })
  }
  function moveValue(i: number, dir: -1 | 1): void {
    if (!shownValues) return
    const j = i + dir
    if (j < 0 || j >= shownValues.length) return
    const a = shownValues[i]
    const b = shownValues[j]
    void run(async () => {
      await updateDomainValue(domain.id, a.id, { sortOrder: b.sortOrder }, token)
      await updateDomainValue(domain.id, b.id, { sortOrder: a.sortOrder }, token)
    })
  }

  return (
    <div className="dm-card">
      <button type="button" className="dm-card__head" onClick={() => setOpenState((o) => !o)} aria-expanded={open}>
        <Caret className="dm-card__caret" open={open} />
        <span className="dm-card__name"><Highlight text={domain.name} query={query} /></span>
        <span className="dm-card__spacer" />
        <span className="dm-card__summary">
          {domain.ownerName ?? '—'} · {domain.valueCount} value{domain.valueCount === 1 ? '' : 's'}
          {' · '}{domain.boundFieldCount ?? 0} field{(domain.boundFieldCount ?? 0) === 1 ? '' : 's'}
        </span>
        {!canEdit && <span className="chip">read-only</span>}
      </button>

      {open && (
        <div className="dm-card__body">
          {domain.description && <p className="dm-card__desc">{domain.description}</p>}

          <p className="eyebrow dm-card__section">Values</p>
          {shownValues === null ? (
            <p className="dm-card__muted">Loading…</p>
          ) : shownValues.length === 0 ? (
            <p className="dm-card__muted">No values yet.</p>
          ) : (
            <div className="dm-values">
              {shownValues.map((v, i) => (
                <div key={v.id} className="dm-value" style={{ opacity: v.active ? 1 : 0.55 }}>
                  <span className="dm-value__label">
                    <Highlight text={v.label} query={query} />
                    {!v.active && <span className="dm-value__retired"> (retired)</span>}
                  </span>
                  <span className="chip"><Highlight text={v.code} query={query} /></span>
                  <span className="dm-value__spacer" />
                  {canEdit && (
                    <div className="dm-value__actions">
                      <button type="button" className="iact" disabled={busy || i === 0} onClick={() => moveValue(i, -1)} aria-label="Move up"><ArrowUp size={13} aria-hidden="true" /></button>
                      <button type="button" className="iact" disabled={busy || i === shownValues.length - 1} onClick={() => moveValue(i, 1)} aria-label="Move down"><ArrowDown size={13} aria-hidden="true" /></button>
                      <button type="button" className="dm-value__retire" disabled={busy} onClick={() => void run(() => updateDomainValue(domain.id, v.id, { active: !v.active }, token))}>
                        {v.active ? 'Retire' : 'Restore'}
                      </button>
                      <button type="button" className="iact danger" disabled={busy} onClick={() => void run(() => deleteDomainValue(domain.id, v.id, token))} aria-label="Delete value">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /></svg>
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {canEdit && (
            <div className="dm-addrow">
              <input className="field" type="text" placeholder="code" value={newCode} onChange={(e) => setNewCode(e.target.value)} />
              <input className="field" type="text" placeholder="label" value={newLabel} onChange={(e) => setNewLabel(e.target.value)} />
              <button type="button" className="dm-addrow__btn" disabled={busy || !newCode.trim() || !newLabel.trim()} onClick={addValue}>+ value</button>
            </div>
          )}

          <p className="eyebrow dm-card__section">Used by</p>
          {usage === null ? (
            <p className="dm-card__muted">Loading…</p>
          ) : usage.length === 0 ? (
            <p className="dm-card__muted">Not bound to any field — safe to delete.</p>
          ) : (
            <div className="dm-usage">
              {usage.map((b) => (
                <span key={`${b.layerId}.${b.fieldKey}`} className="chip">{b.layerName} · {b.fieldLabel}</span>
              ))}
            </div>
          )}

          {error !== null && <p className="sign-form__error">{error}</p>}

          {canEdit && (
            <div className="dm-card__footer">
              <button type="button" className="btn btn--xs btn--ghost" disabled={busy || (domain.boundFieldCount ?? 0) > 0} onClick={() => void run(() => deleteCustomDomain(domain.id, token).then(onChanged))}>
                Delete domain
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
