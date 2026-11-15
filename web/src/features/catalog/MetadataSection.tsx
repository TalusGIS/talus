import { useEffect, useState } from 'react'
import { ArrowRight } from 'lucide-react'

import {
  ApiError,
  downloadMetadata,
  fetchCuration,
  fetchItemMetadata,
  fetchRasterCuration,
  saveItemMetadata,
} from '@/lib/api'
import type { FavoriteItemType, MetadataContact, MetadataDoc, MetadataFormat } from '@/lib/api'

/**
 * Talus 31 — the "Metadata" section shared by the map and layer Manage dialogs.
 *
 * Loads the item's ISO-19115-cored metadata record and edits the REQUIRED-CORE profile (title,
 * abstract, keywords, ISO topic category, license, point-of-contact, language). The spatial extent is
 * SERVER-derived from the layer's geometry (read-only here). A completeness banner mirrors the server's
 * `metadata_complete()` — the same check the Talus 33 Open Data publish gate enforces — so a publisher
 * sees up-front what's still missing. The server re-enforces owner-or-admin on every write; this section
 * is only mounted inside a Manage dialog the caller could open. A documented record exports to QGIS
 * `.qmd` / ISO 19139 XML (Talus 33 c7) — the "your metadata isn't locked in" open-standards download.
 *
 * The completeness banner has always ENDED with "ready to publish to Open Data" and then offered no
 * way to get there: finishing the form meant closing the dialog and reopening it on the Open Data
 * tab. `onPublishToOpenData` closes that loop — the reverse of the jump CurateSection already makes
 * in the other direction when metadata is incomplete.
 */

// ISO 19115 MD_TopicCategoryCode — the fixed 19-value enumeration (the server validates the same set).
const TOPIC_CATEGORIES = [
  'farming', 'biota', 'boundaries', 'climatologyMeteorologyAtmosphere', 'economy', 'elevation',
  'environment', 'geoscientificInformation', 'health', 'imageryBaseMapsEarthCover', 'intelligenceMilitary',
  'inlandWaters', 'location', 'oceans', 'planningCadastre', 'society', 'structure', 'transportation',
  'utilitiesCommunication',
] as const

// Friendly labels for the completeness 'missing' keys.
const FIELD_LABELS: Record<string, string> = {
  title: 'Title', abstract: 'Abstract', keywords: 'Keywords', topicCategory: 'Topic category',
  license: 'License', pointOfContact: 'Point of contact', language: 'Language', spatialExtent: 'Spatial extent',
}

const EMPTY_DOC: MetadataDoc = { keywords: [], language: 'en' }

export default function MetadataSection({
  itemType,
  itemId,
  accessToken,
  onPublishToOpenData,
}: {
  itemType: FavoriteItemType
  itemId: string
  accessToken: string
  /**
   * Jump the dialog to the Open Data tab — the exact reverse of CurateSection's `onGoToMetadata`.
   * Rendered only once `complete` is true, so it appears the moment the gate is satisfied (the save
   * response refreshes `complete`, so no reopen is needed).
   *
   * NAVIGATION ONLY. It deliberately does NOT call curate: publishing exposes data to the public
   * internet and CurateSection is confirm-heavy on purpose, so this walks the publisher into that
   * control rather than forking a second path to the same governed, audited action.
   *
   * Omitted for maps (not curated in v1, F1) and when the deployment has no public face (F6). When
   * it IS supplied we read the SERVER's curation status, because "metadata is complete" is only one
   * of the gates: an already-published layer must not be offered publishing again, and `canCurate` /
   * `publicSharePermitted` decide whether this caller may publish at all (org policy can forbid a
   * member from making anything public even with perfect metadata). Guessing any of those from the
   * metadata record alone would offer a button that 403s.
   */
  onPublishToOpenData?: () => void
}) {
  const [doc, setDoc] = useState<MetadataDoc | null>(null)
  const [complete, setComplete] = useState(false)
  const [missing, setMissing] = useState<string[]>([])
  // Keywords edit as one comma-separated string; split to an array on save.
  const [keywordsText, setKeywordsText] = useState('')
  const [saving, setSaving] = useState(false)
  // Only fetched when a publish hand-off is on offer — maps never ask for it.
  const [curation, setCuration] = useState<{
    curated: boolean
    canCurate: boolean
    publicSharePermitted: boolean
  } | null>(null)
  const [savedFlash, setSavedFlash] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchItemMetadata(itemType, itemId, accessToken)
      .then((m) => {
        if (!active) return
        setDoc(m.doc)
        setComplete(m.complete)
        setMissing(m.missing)
        setKeywordsText((m.doc.keywords ?? []).join(', '))
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [itemType, itemId, accessToken])

  function set<K extends keyof MetadataDoc>(key: K, value: MetadataDoc[K]): void {
    setDoc((prev) => ({ ...(prev ?? EMPTY_DOC), [key]: value }))
  }

  function setContact(field: keyof MetadataContact, value: string): void {
    setDoc((prev) => {
      const base = prev ?? EMPTY_DOC
      return { ...base, pointOfContact: { ...(base.pointOfContact ?? {}), [field]: value } }
    })
  }

  // Depend on the BOOLEAN, not the callback: the parent passes an inline arrow, so its identity
  // changes on every parent render and depending on it refetches curation each time the dialog
  // re-renders (a tab switch, a sibling's state change). Whether the hand-off is on offer is what
  // this effect actually cares about, and that is stable.
  const canOfferPublish = !!onPublishToOpenData && (itemType === 'layer' || itemType === 'raster')

  useEffect(() => {
    if (!canOfferPublish) return
    let active = true
    const get = itemType === 'raster' ? fetchRasterCuration : fetchCuration
    get(itemId, accessToken)
      .then((c) => active && setCuration(c))
      .catch(() => active && setCuration(null)) // best-effort: no hand-off rather than a wrong one
    return () => {
      active = false
    }
  }, [canOfferPublish, itemType, itemId, accessToken])

  async function save(): Promise<void> {
    if (!doc) return
    setSaving(true)
    setError(null)
    const keywords = keywordsText.split(',').map((k) => k.trim()).filter(Boolean)
    try {
      const saved = await saveItemMetadata(itemType, itemId, { ...doc, keywords }, accessToken)
      setDoc(saved.doc)
      setComplete(saved.complete)
      setMissing(saved.missing)
      setKeywordsText((saved.doc.keywords ?? []).join(', '))
      setSavedFlash(true)
      window.setTimeout(() => setSavedFlash(false), 1500)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const ext = doc?.spatialExtent

  return (
    <section className="modal__section">
      <p className="modal__section-title">Metadata</p>
      {doc === null && error === null ? (
        <p className="modal__muted">Loading…</p>
      ) : (
        <>
          <p className={`metadata__status metadata__status--${complete ? 'ok' : 'warn'}`}>
            {complete
              ? '✓ Complete — ready to publish to Open Data'
              : `Incomplete — fill in: ${missing.map((m) => FIELD_LABELS[m] ?? m).join(', ')}`}
          </p>

          <label className="sign-form__field">
            <span>Title</span>
            <input type="text" value={doc?.title ?? ''} onChange={(e) => set('title', e.target.value)} />
          </label>

          <label className="sign-form__field">
            <span>Abstract</span>
            <textarea
              rows={3}
              value={doc?.abstract ?? ''}
              placeholder="What this dataset is, what it covers, and how it should be used."
              onChange={(e) => set('abstract', e.target.value)}
            />
          </label>

          <label className="sign-form__field">
            <span>Keywords (comma-separated)</span>
            <input
              type="text"
              value={keywordsText}
              placeholder="e.g. signs, traffic, public works"
              onChange={(e) => setKeywordsText(e.target.value)}
            />
          </label>

          <label className="sign-form__field">
            <span>Topic category (ISO)</span>
            <select
              value={doc?.topicCategory ?? ''}
              onChange={(e) => set('topicCategory', e.target.value || null)}
            >
              <option value="">— select —</option>
              {TOPIC_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>

          <label className="sign-form__field">
            <span>License / use constraints</span>
            <input
              type="text"
              value={doc?.license ?? ''}
              placeholder="e.g. Public Domain (PDDL)"
              onChange={(e) => set('license', e.target.value)}
            />
          </label>

          <label className="sign-form__field">
            <span>Point of contact — name</span>
            <input
              type="text"
              value={doc?.pointOfContact?.name ?? ''}
              onChange={(e) => setContact('name', e.target.value)}
            />
          </label>

          <label className="sign-form__field">
            <span>Point of contact — email</span>
            <input
              type="email"
              value={doc?.pointOfContact?.email ?? ''}
              onChange={(e) => setContact('email', e.target.value)}
            />
          </label>

          <label className="sign-form__field">
            <span>Language</span>
            <input type="text" value={doc?.language ?? 'en'} onChange={(e) => set('language', e.target.value)} />
          </label>

          {ext ? (
            <p className="metadata__extent">
              Spatial extent (auto-derived): W {ext.west.toFixed(4)}, S {ext.south.toFixed(4)}, E{' '}
              {ext.east.toFixed(4)}, N {ext.north.toFixed(4)}
            </p>
          ) : itemType === 'layer' ? (
            <p className="metadata__extent modal__muted">
              Spatial extent auto-derives from the layer’s geometry when you save.
            </p>
          ) : null}

          {error && <p className="modal__field-error">{error}</p>}

          <div className="sign-form__actions">
            <button type="button" className="btn btn--sm" disabled={saving} onClick={() => void save()}>
              {saving ? 'Saving…' : savedFlash ? 'Saved' : 'Save metadata'}
            </button>
          </div>

          <ExportRow itemType={itemType} itemId={itemId} accessToken={accessToken} />

          {/* Every gate, not just the metadata one: complete AND not already published AND this
              caller may actually publish. `metadataComplete` on the status is deliberately NOT used
              — the local `complete` is refreshed by the save response, so the hand-off appears the
              moment the form is finished rather than one reopen later. */}
          {complete &&
            onPublishToOpenData &&
            curation?.curated === false &&
            curation.canCurate &&
            curation.publicSharePermitted && (
            <div className="metadata__next">
              <p className="metadata__next-text">
                Metadata is complete, so this {itemType === 'raster' ? 'raster' : 'layer'} can go in
                the public Open Data catalog.
              </p>
              <button type="button" className="btn btn--sm" onClick={onPublishToOpenData}>
                Publish to Open Data
                <ArrowRight size={14} aria-hidden="true" />
              </button>
              <p className="metadata__next-note">
                You’ll see what becomes public, and confirm, before anything goes live.
              </p>
            </div>
            )}
        </>
      )}
    </section>
  )
}

const _META_FORMATS: { format: MetadataFormat; label: string }[] = [
  { format: 'qmd', label: 'QGIS .qmd' },
  { format: 'iso19139', label: 'ISO 19139' },
]

function ExportRow({
  itemType,
  itemId,
  accessToken,
}: {
  itemType: FavoriteItemType
  itemId: string
  accessToken: string
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function run(format: MetadataFormat): Promise<void> {
    setErr(null)
    setBusy(format)
    try {
      await downloadMetadata(itemType, itemId, format, accessToken)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Download failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="metadata__export">
      <span className="modal__muted">Download metadata:</span>
      {_META_FORMATS.map(({ format, label }) => (
        <button
          key={format}
          type="button"
          className="btn btn--sm"
          disabled={busy !== null}
          onClick={() => void run(format)}
        >
          {busy === format ? '…' : label}
        </button>
      ))}
      {err && <span className="modal__field-error">{err}</span>}
    </div>
  )
}
