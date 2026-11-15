import { useEffect, useState } from 'react'

import { ApiError, apiOrigin, downloadFieldProject, fetchOrgSettings } from '@/lib/api'
import type { FieldProjectSummary, PatMinted } from '@/lib/api'
import { MintTokenModal } from '@/features/profile/AccessTokens'

import ImportQr from './ImportQr'
import CloseButton from '@/components/CloseButton'

/**
 * The Field-project OPEN affordance (decision-field-projects.md F7): clicking a field project
 * never opens a viewer — it offers the QField Import URL + the crew workflow, right where the
 * crews' delete-old-first rule has to live (B5c field evidence), plus a portal download.
 *
 * The Import URL is credentialed: QField sends the access token in the URL userinfo
 * (https://talus:<token>@host/…) — the same bench-proven shape the desktop connection recipes
 * use. The dialog shows a placeholder, never a real token.
 */

/** Human text for the upload-lip warning codes (F5) + the generation notes
 * (decision-generate-field-project.md F2–F4, stored in the same warnings jsonb). */
const WARNING_TEXT: Record<string, string> = {
  multiple_qgs:
    'The archive holds more than one project file (.qgs/.qgs~). Phones can open the wrong one — ' +
    're-export a clean .qgz with a single .qgs inside.',
  os_junk:
    'The archive contains macOS junk entries (__MACOSX, ._ files). It will work, but a clean ' +
    'export avoids mis-opens.',
  foreign_datasources:
    'No layer in this project points at this portal — double-check it is the right artifact ' +
    'for these crews.',
  unreadable_metadata:
    'The project metadata could not be read. The file is stored and served as-is; the catalog ' +
    'card just shows no title or layer count.',
  skipped_layers:
    'Some of the web map’s layers were left out — rasters, external references, tables and ' +
    'layers hidden in the map are not field-collectable.',
  view_layer_not_served:
    'A view layer in the web map was left out — view layers can’t be served to QField yet.',
  filters_not_applied:
    'A layer in the web map has a filter, but the field project shows ALL features — filters ' +
    'don’t travel yet. Crews will see the unfiltered layer.',
  renderer_flattened:
    'A layer uses per-category colors on the web map; the field project shows it in a single ' +
    'color (the category default).',
  point_icon_not_carried:
    'A point layer uses a map symbol on the web map; the field project draws it as a plain ' +
    'marker in the same color and size. The symbol itself does not travel yet.',
  photo_naming_wall_clock:
    'A photo layer has no gnss_fix_time field, so photo filenames fall back to a wall-clock ' +
    'stamp — the post-field-day photo ingest is not deterministic. Add the GNSS bundle.',
  token_scope_read_only:
    'The map has grown editable layers, but this project’s embedded token is read-only (it was ' +
    'minted before). To collect on the new layers, delete this item and generate a fresh one.',
  layer_token_denied:
    'A layer was left out because the project’s access token can’t read it — admin-only ' +
    'visibility doesn’t travel into tokens. Share the layer (organization or group) with the ' +
    'generating account, then Regenerate.',
}

function IconCopy() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  )
}

function IconCheck() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  )
}

/** Mono value + copy icon-button (flashes to a check) — the UseInWizard affordance, reused. */
function CopyField({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="ck__field">
      <code>{value}</code>
      <button
        type="button"
        className="ck__iconbtn"
        aria-label={copied ? 'Copied' : 'Copy'}
        title={copied ? 'Copied' : 'Copy'}
        onClick={() => {
          void navigator.clipboard?.writeText(value)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? <IconCheck /> : <IconCopy />}
      </button>
    </div>
  )
}

export default function FieldProjectImportDialog({
  project,
  accessToken,
  onClose,
}: {
  project: FieldProjectSummary
  accessToken: string
  onClose: () => void
}) {
  const [advertised, setAdvertised] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Crew-token flow: the composed credentialed URL exists ONLY in
  // this dialog's memory, and ONLY at the one moment a token's plaintext is available — a
  // fresh mint (the shared modal, fork-6-A one-flow) or a paste of a token the user saved.
  // Existing tokens can never be listed-and-chosen: the server keeps sha256 only (revoke-first
  // by design), so "pick a token -> get the URL" is impossible after mint. Nothing here is
  // persisted; closing the dialog forgets the token.
  const [showMint, setShowMint] = useState(false)
  const [minted, setMinted] = useState<PatMinted | null>(null)
  const [pastedToken, setPastedToken] = useState('')

  useEffect(() => {
    let active = true
    fetchOrgSettings(accessToken)
      .then((s) => active && setAdvertised(s.advertisedBaseUrl))
      .catch(() => active && setAdvertised(null))
    return () => {
      active = false
    }
  }, [accessToken])

  const base = (advertised ?? apiOrigin()).replace(/\/+$/, '')
  const archiveUrl = `${base}/api/v1/field-projects/${project.id}/archive`
  // The server's own PAT shape (ratelimit.PAT_SHAPE_RE) — a paste that doesn't match is
  // ignored, so a crafted string can never steer the composed URL's authority.
  const pasted = pastedToken.trim()
  const crewToken = minted?.token ?? (/^tpat_[A-Za-z0-9_-]{20,64}$/.test(pasted) ? pasted : null)
  // The credentialed form QField actually consumes — token in the URL userinfo. With a crew
  // token in hand the URL is COMPLETE (paste-and-go, no manual substitution — the 2026-08-18
  // A50 bench showed hand-editing this string on a phone reliably breaks it).
  const importUrl = archiveUrl.replace(
    /^(https?:\/\/)/,
    () => `http${archiveUrl.startsWith('https') ? 's' : ''}://talus:${crewToken ?? '<ACCESS-TOKEN>'}@`,
  )

  async function download(): Promise<void> {
    setDownloading(true)
    setError(null)
    try {
      const blob = await downloadFieldProject(project.id, accessToken)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${project.title.replace(/[\\/:"]/g, '')}.qgz`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e))
    } finally {
      setDownloading(false)
    }
  }

  const warnings = (project.warnings ?? []).filter((w) => WARNING_TEXT[w])

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Field project"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">“{project.title}” on a phone</h2>
          <CloseButton onClick={onClose} />
        </div>

        <section className="modal__section">
          <p className="modal__muted">
            {project.qgsTitle ? `Project “${project.qgsTitle}” · ` : ''}
            {typeof project.layerCount === 'number' ? `${project.layerCount} layer${project.layerCount === 1 ? '' : 's'} · ` : ''}
            {(project.sizeBytes / (1024 * 1024)).toFixed(1)} MB · updated{' '}
            {new Date(project.updated_at).toLocaleDateString()}
          </p>
          {project.sourceWebMapId && (
            <p className="modal__muted">
              Generated from a web map — kept current with Regenerate (Manage), never by editing
              the file.
            </p>
          )}
          {warnings.length > 0 && (
            <ul className="modal__muted">
              {warnings.map((w) => (
                <li key={w} className="modal__field-error">{WARNING_TEXT[w]}</li>
              ))}
            </ul>
          )}
        </section>

        {/* Crew token — composes the COMPLETE Import URL while the plaintext exists. */}
        <section className="modal__section">
          <h3 className="modal__section-title">Crew access token</h3>
          {crewToken ? (
            <p className="modal__muted">
              {minted ? (
                <>Token <strong>{minted.label}</strong> created — the Import URL below is complete.</>
              ) : (
                <>Token applied — the Import URL below is complete.</>
              )}{' '}
              It now <strong>contains the token</strong>: treat the URL like a password. Nothing
              is saved — closing this dialog forgets it (revoke anytime under Account ▸ Access
              tokens).
            </p>
          ) : (
            <>
              <p className="modal__muted">
                The Import URL needs a crew access token. Create one here and the complete URL
                is composed on the spot — a token’s secret exists only at creation, so it can’t
                be picked from a list later. Or paste one you saved.
              </p>
              <div className="sign-form__actions" style={{ justifyContent: 'flex-start' }}>
                <button type="button" className="btn btn--sm" onClick={() => setShowMint(true)}>
                  Create crew token
                </button>
              </div>
              <input
                type="text"
                value={pastedToken}
                placeholder="…or paste a saved token (tpat_…)"
                onChange={(e) => setPastedToken(e.target.value)}
                aria-label="Existing access token"
              />
            </>
          )}
        </section>

        {/* The Import URL — the whole point of the item type. */}
        <section className="modal__section">
          <h3 className="modal__section-title">Import into QField</h3>
          <p>
            In QField: <strong>⋮ next to “Imported projects” ▸ Import URL</strong>, then paste
            this address{crewToken ? (
              <> — it’s ready to use as-is (one line, no edits)</>
            ) : (
              <> with <code>&lt;ACCESS-TOKEN&gt;</code> replaced by the crew’s access token
              (or use the section above to compose it)</>
            )}:
          </p>
          <CopyField value={importUrl} />
          {crewToken && (
            <>
              <ImportQr value={importUrl} title={project.title} tokenLabel={minted?.label} />
              <p className="modal__muted">
                Crews can scan this straight off the screen (QField’s Import URL has a
                reader), or <strong>Download PNG</strong> to print. A saved or printed QR
                <strong> is a key</strong> — it contains the token; revoking the token
                (Account ▸ Access tokens) disables every copy at once.
              </p>
            </>
          )}
          <p className="modal__muted">
            <strong>Updating later: re-importing creates a duplicate</strong> (“{project.title}_1”) —
            crews must <strong>delete the old copy first</strong>, then import again. On Android,
            always use Import — never open a .qgz straight from Downloads (that makes Downloads
            the project folder, and “Compress and send” will ship everything beside it). If the
            legend shows <code>log_*</code> tables and no camera button, the project was opened
            the wrong way — delete it and import again.
          </p>
          {advertised === null && (
            <p className="modal__muted">
              This URL currently uses the portal’s API address. An administrator can set the
              organization’s advertised base URL under Admin → Connections so it always shows an
              address phones can reach.
            </p>
          )}
        </section>

        <section className="modal__section">
          <h3 className="modal__section-title">Download</h3>
          <p className="modal__muted">
            The stored .qgz, exactly as uploaded. Talus never opens or edits projects — to change
            one, download it, edit in QGIS, and upload the replacement in Manage.
          </p>
          <div className="sign-form__actions">
            <button type="button" className="btn btn--sm" onClick={() => void download()} disabled={downloading}>
              {downloading ? 'Downloading…' : 'Download .qgz'}
            </button>
          </div>
          {error && <p className="modal__field-error">{error}</p>}
        </section>

        {showMint && (
          <MintTokenModal
            accessToken={accessToken}
            defaultScope="read_write"
            namePlaceholder={`e.g. crew: ${project.title}`}
            onClose={() => setShowMint(false)}
            onMinted={(m) => {
              setShowMint(false)
              setMinted(m)
            }}
          />
        )}
      </div>
    </div>
  )
}
