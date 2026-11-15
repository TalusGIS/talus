import { useEffect, useState } from 'react'

import {
  ApiError,
  fetchMyLayers,
  fetchWebMap,
  generateFieldProjectFromMap,
} from '@/lib/api'
import type { MyLayer, WebMapDetail, WebMapSummary } from '@/lib/api'

/**
 * "Make field project" (decision-generate-field-project.md F5) — the publisher-gated section
 * in a web map's Manage ▸ Settings tab. The server compiles the map into a ready-to-collect
 * QField .qgz, mints a dedicated access token, and files the result under Catalog ▸ Field
 * projects (NOT pre-shared — the artifact embeds that token; the publisher shares it
 * deliberately, like an upload).
 *
 * The preview below is a client-side APPROXIMATION of the server's layer walk (visible hosted
 * point/line/polygon layers ride in; view layers, rasters, external references, tables and
 * invisible layers are skipped loudly). The server is the authority — its warnings land on
 * the generated item either way.
 */

type PreviewRow = {
  key: string
  name: string
  included: boolean
  detail: string   // badges when included; the skip reason otherwise
}

function buildPreview(detail: WebMapDetail, layers: MyLayer[]): PreviewRow[] {
  const byId = new Map(layers.map((l) => [l.id, l]))
  const rows: PreviewRow[] = []
  detail.definition.operationalLayers.forEach((op, i) => {
    const kind = op.kind ?? 'layer'
    const key = `${op.layerId ?? op.rasterId ?? op.externalId ?? 'op'}-${i}`
    if (kind !== 'layer') {
      rows.push({
        key,
        name: op.title || op.url || (kind === 'raster' ? 'Raster layer' : 'External reference'),
        included: false,
        detail: kind === 'raster' ? 'raster — not field-collectable' : 'external reference — skipped',
      })
      return
    }
    const lid = op.layerId
    if (!lid) return
    const layer = byId.get(lid)
    const name = op.title || layer?.displayName || lid
    if (op.visible === false) {
      rows.push({ key, name, included: false, detail: 'not visible in this map' })
      return
    }
    if (lid.startsWith('vw_')) {
      rows.push({ key, name, included: false, detail: 'view layers can’t be served to QField yet' })
      return
    }
    if (!layer || !['point', 'line', 'polygon'].includes(layer.geomType ?? '')) {
      rows.push({ key, name, included: false, detail: 'not a hosted vector layer' })
      return
    }
    // Admin-override-only visibility (no genuine share): the project's token resolves
    // WITHOUT admin, so the server walk will skip this layer — say so before the click.
    if (!layer.owner && !layer.sharedWithMe) {
      rows.push({
        key,
        name,
        included: false,
        detail: 'admin-only visibility — the project token can’t read it; share the layer first',
      })
      return
    }
    const badges = [
      layer.geomType,
      layer.editable ? 'editable' : 'read-only',
      layer.attachmentsEnabled ? 'photos' : null,
      op.filters && op.filters.length > 0 ? 'filter won’t apply' : null,
    ].filter(Boolean)
    rows.push({ key, name, included: true, detail: badges.join(' · ') })
  })
  return rows
}

export default function MakeFieldProjectSection({
  map,
  accessToken,
  onGenerated,
}: {
  map: WebMapSummary
  accessToken: string
  onGenerated?: () => void
}) {
  const [preview, setPreview] = useState<PreviewRow[] | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [title, setTitle] = useState(map.title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<{ title: string; layerCount: number | null; scope: string } | null>(null)

  useEffect(() => {
    let active = true
    Promise.all([fetchWebMap(map.id, accessToken), fetchMyLayers(accessToken)])
      .then(([detail, mine]) => {
        if (active) setPreview(buildPreview(detail, mine.layers))
      })
      .catch((e) => active && setPreviewError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [map.id, accessToken])

  const included = preview?.filter((r) => r.included) ?? []

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await generateFieldProjectFromMap(map.id, { title: title.trim() || undefined }, accessToken)
      setDone({ title: res.title, layerCount: res.layerCount ?? null, scope: res.pat.scope })
      onGenerated?.()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="modal__section">
      <p className="modal__section-title">Field project (QField)</p>
      {done ? (
        <>
          <p className="modal__muted">
            “{done.title}” is ready under Catalog ▸ Field projects
            {done.layerCount != null ? ` (${done.layerCount} layers)` : ''}. A dedicated access
            token ({done.scope === 'read_write' ? 'read & edit' : 'read-only'}) was created in
            your token list — the project embeds it; revoke it there to retire the project.
          </p>
          <p className="modal__muted">
            Crews import it by URL from the Field projects tab. It is not shared yet — share it
            from its own Manage dialog when ready.
          </p>
        </>
      ) : (
        <>
          <p className="modal__muted">Compile this map into a ready-to-collect QField project:</p>
          <details className="modal__muted">
            <summary>More about generated projects</summary>
            <p>
              Live layers over the network, GNSS capture pre-wired, photos where attachments
              are on. The map stays the source of truth — when it changes, Regenerate the
              project from its Manage dialog (Talus never edits the .qgz; it recompiles it).
              A dedicated access token is created in your token list and embedded in the
              project; deleting the item revokes it. The item is not shared until you share it.
            </p>
          </details>
          {previewError && <p className="modal__field-error">{previewError}</p>}
          {preview && (
            <ul className="modal__muted" style={{ margin: '0 0 8px', paddingLeft: 18 }}>
              {preview.map((r) => (
                <li key={r.key}>
                  {r.included ? '✓' : '—'} {r.name}
                  {r.detail ? ` (${r.detail})` : ''}
                </li>
              ))}
            </ul>
          )}
          <label className="modal__label" htmlFor="fp-gen-title">
            Project title
          </label>
          <input
            id="fp-gen-title"
            className="modal__input"
            value={title}
            maxLength={80}
            onChange={(e) => setTitle(e.target.value)}
            disabled={busy}
          />
          {error && <p className="modal__field-error">{error}</p>}
          <div className="sign-form__actions">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void generate()}
              disabled={busy || (preview !== null && included.length === 0)}
            >
              {busy ? 'Generating…' : 'Make field project'}
            </button>
          </div>
          {preview !== null && included.length === 0 && (
            <p className="modal__field-error">
              No field-collectable layers — the project needs at least one visible hosted
              point, line or polygon layer.
            </p>
          )}
        </>
      )}
    </section>
  )
}
