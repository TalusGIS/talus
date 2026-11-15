import { useEffect, useState, type ReactNode } from 'react'

import {
  apiOrigin,
  ApiError,
  exportRaster,
  fetchOrgSettings,
  fetchRasterExportFormats,
  type RasterExportFormat,
  type RasterSummary,
} from '@/lib/api'
import { CopyRow, MintTokenModal } from '@/features/profile/AccessTokens'
import { ProServerDialogNote } from '@/features/catalog/ProServerDialogNote'
import {
  buildProWmtsFile,
  buildQgisWcsConnectionsXml,
  buildQgisWmsConnectionsXml,
  downloadTextFile,
} from '@/lib/connectionFiles'
import type { PatMinted } from '@/lib/api'
import CloseButton from '@/components/CloseButton'
import Caret from '@/components/Caret'

/**
 * The raster "Use in…" wizard (Interop I-2 + I-6, decision-interop-i2-wmts.md /
 * decision-interop-i6-coverages.md) — the imagery twin of the vector UseInWizard, same paged
 * chrome (intro → goal → recipe, Cancel · Back · Next · Finish). A hosted raster is a WMTS layer
 * (stream a picture) AND a WCS coverage (query the live pixel values): Pro/QGIS reach both with a
 * PAT through the sign-in doors. Goals: Stream in Pro / Stream in QGIS (WMTS) / Query live values
 * in Pro / in QGIS (WCS 1.0.0) / Download a copy (the existing raster export). No server URL as an
 * input — it's a deployment fact (Expandable).
 */

type Goal = 'pro' | 'qgis' | 'pro-values' | 'qgis-values' | 'download'
type Page = 'intro' | 'goals' | 'recipe'

const GOALS: Array<{ id: Goal; label: string; hint: string }> = [
  { id: 'pro', label: 'Stream in ArcGIS Pro', hint: 'live imagery over the network (WMTS)' },
  { id: 'qgis', label: 'Stream in QGIS', hint: 'live imagery over the network (WMTS)' },
  { id: 'pro-values', label: 'Query live values in ArcGIS Pro', hint: 'elevation / pixel values from the source (WCS)' },
  { id: 'qgis-values', label: 'Query live values in QGIS', hint: 'elevation / pixel values from the source (WCS)' },
  { id: 'download', label: 'Download a copy', hint: 'GeoTIFF / COG and other formats' },
]

function Expandable({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="use-in__expand">
      <button type="button" className="use-in__expand-toggle" onClick={() => setOpen((o) => !o)}>
        <Caret open={open} /> {label}
      </button>
      {open && <div className="use-in__expand-body">{children}</div>}
    </div>
  )
}

export default function RasterUseInWizard({
  raster,
  accessToken,
  onClose,
}: {
  raster: RasterSummary
  accessToken: string
  onClose: () => void
}) {
  const [page, setPage] = useState<Page>('intro')
  const [goal, setGoal] = useState<Goal | null>(null)
  const [advertised, setAdvertised] = useState<string | null>(null)

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
  const capsUrl = `${base}/api/v1/wmts-auth/1.0.0/WMTSCapabilities.xml`
  const wcsUrl = `${base}/api/v1/wcs-auth`

  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Use this raster in desktop apps"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Use “{raster.title}” in…</h2>
          <CloseButton onClick={onClose} />
        </div>

        {page === 'intro' && (
          <section className="modal__section">
            <p>
              Streaming imagery to a desktop GIS (ArcGIS Pro, QGIS) takes two steps, once per
              computer: <strong>1</strong> — create an access token (your app password),{' '}
              <strong>2</strong> — add the WMTS connection and sign in with the token when the app
              asks.
            </p>
            <p className="modal__muted">
              Every raster you can view is served as a WMTS layer — no publish step. Click Next to
              pick what you want to do with “{raster.title}”.
            </p>
            {advertised === null && (
              <p className="modal__muted">
                Connection URLs currently use this portal’s API address. An administrator can set
                the advertised base URL under Admin → Connections so they show an address other
                machines can reach.
              </p>
            )}
          </section>
        )}

        {page === 'goals' && (
          <section className="modal__section">
            <h3 className="modal__section-title">I want to…</h3>
            <div className="use-in__goals">
              {GOALS.map((g) => (
                <button
                  key={g.id}
                  type="button"
                  className="use-in__goal"
                  onClick={() => {
                    setGoal(g.id)
                    setPage('recipe')
                  }}
                >
                  <span className="use-in__goal-label">{g.label}</span>
                  <span className="modal__muted">{g.hint}</span>
                </button>
              ))}
            </div>
          </section>
        )}

        {page === 'recipe' &&
          (goal === 'download' ? (
            <DownloadRecipe raster={raster} accessToken={accessToken} />
          ) : goal === 'pro-values' || goal === 'qgis-values' ? (
            <ValuesRecipe
              client={goal === 'pro-values' ? 'pro' : 'qgis'}
              raster={raster}
              wcsUrl={wcsUrl}
              accessToken={accessToken}
            />
          ) : goal ? (
            <StreamRecipe client={goal} raster={raster} capsUrl={capsUrl} accessToken={accessToken} />
          ) : null)}

        <div className="sign-form__actions wizard__actions">
          <button type="button" className="btn btn--sm btn--danger" onClick={onClose}>
            Cancel
          </button>
          <span className="wizard__actions-spacer" />
          {page !== 'intro' && (
            <button
              type="button"
              className="btn btn--sm"
              style={{ background: 'var(--brand-subtle)', color: 'var(--text-brand)', borderColor: 'var(--border-default)' }}
              onClick={() => setPage(page === 'recipe' ? 'goals' : 'intro')}
            >
              Back
            </button>
          )}
          {page === 'intro' && (
            <button type="button" className="btn btn--sm" onClick={() => setPage('goals')}>
              Next
            </button>
          )}
          {page === 'recipe' && (
            <button
              type="button"
              className="btn btn--sm"
              style={{ background: 'var(--success-600)', color: 'var(--text-inverse)', borderColor: 'var(--success-600)' }}
              onClick={onClose}
            >
              Finish
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function StreamRecipe({
  client,
  raster,
  capsUrl,
  accessToken,
}: {
  client: 'pro' | 'qgis'
  raster: RasterSummary
  capsUrl: string
  accessToken: string
}) {
  const [minted, setMinted] = useState<PatMinted | null>(null)
  const [pasted, setPasted] = useState('')
  const [showMint, setShowMint] = useState(false)
  const testValue = minted?.token ?? pasted.trim()
  const host = capsUrl
    .replace(/^https?:\/\//, '')
    .replace(/\/api\/v1\/wmts-auth.*$/, '')
    .replace(/[:/\\]/g, '_')

  function downloadFile(): void {
    if (client === 'qgis') {
      downloadTextFile(
        'talus-qgis-imagery-connection.xml',
        buildQgisWmsConnectionsXml(`Talus GIS imagery (${host})`, capsUrl),
      )
    } else {
      downloadTextFile(
        `Talus GIS — WMTS (${host}).wmts`,
        buildProWmtsFile(`Talus GIS — WMTS (${host})`, capsUrl),
        'application/octet-stream',
      )
    }
  }

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">
        {client === 'pro' ? 'Stream in ArcGIS Pro' : 'Stream in QGIS'}
      </h3>
      <p className="modal__muted">
        This raster is served as a WMTS layer. <strong>Already connected this computer?</strong>{' '}
        Then it’s already in your Talus WMTS connection — just add it. Setting up fresh:
      </p>

      <h4 className="use-in__step">1 · Access token</h4>
      {minted !== null ? (
        <>
          <CopyRow value={minted.token} mono />
          <p className="modal__muted">
            “{minted.label}” — copy it now; it’s the password when the app asks (username{' '}
            <code className="sa-code">talus</code>).
          </p>
        </>
      ) : (
        <div className="use-in__credrow">
          <button type="button" className="btn btn--sm" onClick={() => setShowMint(true)}>
            + Token
          </button>
          <span className="modal__muted">you’ll paste it as the password when the app asks.</span>
        </div>
      )}

      <h4 className="use-in__step">2 · WMTS connection</h4>
      {client === 'qgis' ? (
        <>
          <div className="use-in__credrow">
            <button type="button" className="btn btn--sm" onClick={downloadFile}>
              Download connection file
            </button>
          </div>
          <p className="modal__muted">
            In QGIS: Layer ▸ Data Source Manager ▸ <strong>WMS/WMTS</strong> ▸ click{' '}
            <strong>Load</strong> (next to Save, under the connections list) ▸ choose the file ▸{' '}
            <strong>Import</strong> ▸ Connect ▸ add “{raster.title}”. Sign in with{' '}
            <code className="sa-code">talus</code> + your token.
          </p>
          <Expandable label="Set it up manually instead">
            <p className="modal__muted">Server / capabilities URL (set by your administrator):</p>
            <CopyRow value={capsUrl} mono />
            <p className="modal__muted">
              Data Source Manager ▸ WMS/WMTS ▸ New ▸ paste the URL ▸ Authentication: Basic,
              username <code className="sa-code">talus</code> + your token ▸ Connect ▸ add the
              raster.
            </p>
          </Expandable>
        </>
      ) : (
        <>
          <div className="use-in__credrow">
            <button type="button" className="btn btn--sm" onClick={downloadFile}>
              Download connection file
            </button>
          </div>
          <p className="modal__muted">
            In Pro: save the <code className="sa-code">.wmts</code> file into your project’s home
            folder (or any folder connection) — it appears in the Catalog pane under Servers; open
            it, sign in with <code className="sa-code">talus</code> + your token, and add “
            {raster.title}”. <strong>Changed the URL or credential later?</strong> Fully restart Pro
            (it caches per session and keys stored credentials to the server URL).
          </p>
          <Expandable label="Set it up manually instead">
            <p className="modal__muted">Server / capabilities URL (set by your administrator):</p>
            <CopyRow value={capsUrl} mono />
            <p className="modal__muted">
              Insert ▸ Connections ▸ Server ▸ New WMTS Server ▸ paste the URL ▸ Server
              Authentication, username <code className="sa-code">talus</code> + your token.
            </p>
          </Expandable>
          <ProServerDialogNote
            route={
              <>
                <strong>New WMTS Server</strong> (or the file above) is the dialog that works for
                imagery.
              </>
            }
          />
        </>
      )}

      <Expandable label="Test the connection">
        {minted === null && (
          <>
            <p className="modal__muted">Paste a token you saved earlier to test with it.</p>
            <input
              type="password"
              className="use-in__paste"
              placeholder="tpat_…"
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              autoComplete="off"
            />
          </>
        )}
        <TestConnection url={capsUrl} tokenValue={testValue} />
      </Expandable>

      {showMint && (
        <MintTokenModal
          accessToken={accessToken}
          onClose={() => setShowMint(false)}
          onMinted={(m) => {
            setShowMint(false)
            setMinted(m)
          }}
        />
      )}
    </section>
  )
}

function ValuesRecipe({
  client,
  raster,
  wcsUrl,
  accessToken,
}: {
  client: 'pro' | 'qgis'
  raster: RasterSummary
  wcsUrl: string
  accessToken: string
}) {
  const [minted, setMinted] = useState<PatMinted | null>(null)
  const [pasted, setPasted] = useState('')
  const [showMint, setShowMint] = useState(false)
  const testValue = minted?.token ?? pasted.trim()
  const host = wcsUrl
    .replace(/^https?:\/\//, '')
    .replace(/\/api\/v1\/wcs-auth.*$/, '')
    .replace(/[:/\\]/g, '_')

  // Live values ARE the raster's data — the coverage door rides the same downloadable knob as
  // export (decision-interop-i6-coverages.md fork 1), so a view-only raster gets the honest gate.
  if (raster.downloadable === false) {
    return (
      <section className="modal__section">
        <h3 className="modal__section-title">
          {client === 'pro' ? 'Query live values in ArcGIS Pro' : 'Query live values in QGIS'}
        </h3>
        <p className="modal__muted">
          This raster is marked view/stream-only, so its pixel values aren’t served — streaming
          (WMTS) still works. Ask the owner to allow downloads to enable live values.
        </p>
      </section>
    )
  }

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">
        {client === 'pro' ? 'Query live values in ArcGIS Pro' : 'Query live values in QGIS'}
      </h3>
      <p className="modal__muted">
        This raster is served as a <strong>WCS 1.0.0 coverage</strong> — your desktop reads the
        actual pixel values (elevations, measurements) live from the authoritative copy, in the
        raster’s native coordinate system. Analysis happens in your GIS; Talus serves the values.
      </p>

      <h4 className="use-in__step">1 · Access token</h4>
      {minted !== null ? (
        <>
          <CopyRow value={minted.token} mono />
          <p className="modal__muted">
            “{minted.label}” — copy it now; it’s the password when the app asks (username{' '}
            <code className="sa-code">talus</code>).
          </p>
        </>
      ) : (
        <div className="use-in__credrow">
          <button type="button" className="btn btn--sm" onClick={() => setShowMint(true)}>
            + Token
          </button>
          <span className="modal__muted">you’ll paste it as the password when the app asks.</span>
        </div>
      )}

      <h4 className="use-in__step">2 · WCS connection</h4>
      {client === 'qgis' ? (
        <>
          <div className="use-in__credrow">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() =>
                downloadTextFile(
                  'talus-qgis-values-connection.xml',
                  buildQgisWcsConnectionsXml(`Talus GIS values (${host})`, wcsUrl),
                )
              }
            >
              Download connection file
            </button>
          </div>
          <p className="modal__muted">
            In QGIS: Layer ▸ Data Source Manager ▸ <strong>WCS</strong> ▸ click <strong>Load</strong>{' '}
            (under the connections list) ▸ choose the file ▸ <strong>Import</strong> ▸ Connect ▸ add
            “{raster.title}”. Sign in with <code className="sa-code">talus</code> + your token, then
            use Identify to read values.
          </p>
          <Expandable label="Set it up manually instead">
            <p className="modal__muted">Server URL (set by your administrator):</p>
            <CopyRow value={wcsUrl} mono />
            <p className="modal__muted">
              Data Source Manager ▸ WCS ▸ New ▸ paste the URL ▸ Authentication: Basic, username{' '}
              <code className="sa-code">talus</code> + your token ▸ Connect ▸ add the coverage.
            </p>
          </Expandable>
        </>
      ) : (
        <>
          <p className="modal__muted">Server URL (set by your administrator):</p>
          <CopyRow value={wcsUrl} mono />
          <p className="modal__muted">
            In Pro: Insert ▸ Connections ▸ Server ▸ <strong>New WCS Server</strong> ▸ paste the URL
            (the version defaults to 1.0.0 — the one this server speaks) ▸ Server Authentication,
            username <code className="sa-code">talus</code> + your token. Add “{raster.title}” from
            the connection, then use Explore/Identify to read values.
          </p>
          <ProServerDialogNote
            route={
              <>
                <strong>New WCS Server</strong> (above) is the dialog that works for live values.
              </>
            }
          />
        </>
      )}

      <Expandable label="Test the connection">
        {minted === null && (
          <>
            <p className="modal__muted">Paste a token you saved earlier to test with it.</p>
            <input
              type="password"
              className="use-in__paste"
              placeholder="tpat_…"
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              autoComplete="off"
            />
          </>
        )}
        <TestConnection
          url={`${wcsUrl}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCapabilities`}
          tokenValue={testValue}
          listedMarker="<CoverageOfferingBrief>"
          listedMsg="Connected — live values are available with these credentials."
        />
      </Expandable>

      {showMint && (
        <MintTokenModal
          accessToken={accessToken}
          onClose={() => setShowMint(false)}
          onMinted={(m) => {
            setShowMint(false)
            setMinted(m)
          }}
        />
      )}
    </section>
  )
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function DownloadRecipe({ raster, accessToken }: { raster: RasterSummary; accessToken: string }) {
  const [formats, setFormats] = useState<RasterExportFormat[] | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    fetchRasterExportFormats(accessToken)
      .then((f) => active && setFormats(f))
      .catch(() => active && setFormats([]))
    return () => {
      active = false
    }
  }, [accessToken])

  async function grab(fmt: string): Promise<void> {
    setBusy(fmt)
    setError(null)
    try {
      // Whole raster (no bbox) — the wizard is a quick "get a copy", not the map's clip tool.
      const result = await exportRaster(raster.id, { format: fmt }, accessToken)
      saveBlob(result.blob, result.filename)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Export failed')
    } finally {
      setBusy(null)
    }
  }

  if (raster.downloadable === false) {
    return (
      <section className="modal__section">
        <h3 className="modal__section-title">Download a copy</h3>
        <p className="modal__muted">
          Downloads are turned off for this raster — it’s view/stream-only. Use the streaming
          options to add it live in your desktop GIS.
        </p>
      </section>
    )
  }

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Download a copy</h3>
      <p className="modal__muted">A snapshot of the raster you can open in any GIS.</p>
      {formats === null ? (
        <p className="modal__muted">Loading formats…</p>
      ) : (
        formats.map((f) => (
          <div key={f.key} className="use-in__credrow">
            <button
              type="button"
              className="btn btn--sm"
              disabled={busy !== null}
              onClick={() => void grab(f.key)}
            >
              {busy === f.key ? 'Preparing…' : f.label}
            </button>
            <span className="modal__muted">.{f.ext}</span>
          </div>
        ))
      )}
      {error && <p className="modal__field-error">{error}</p>}
    </section>
  )
}

function TestConnection({
  url,
  tokenValue,
  listedMarker = '<Layer>',
  listedMsg = 'Connected — imagery is available with these credentials.',
}: {
  url: string
  tokenValue: string
  listedMarker?: string
  listedMsg?: string
}) {
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null)

  async function run(): Promise<void> {
    setTesting(true)
    setResult(null)
    try {
      const headers: Record<string, string> = { Accept: 'application/xml' }
      if (tokenValue) headers.Authorization = `Basic ${btoa(`talus:${tokenValue}`)}`
      const res = await fetch(url, { headers, cache: 'no-store' })
      if (res.ok) {
        const text = await res.text()
        const listed = text.includes(listedMarker)
        setResult({
          ok: true,
          msg: listed
            ? listedMsg
            : 'Connected, but no rasters are visible to this token yet.',
        })
      } else if (res.status === 401) {
        setResult({ ok: false, msg: 'Reached the server, but the token was rejected (revoked, expired, or mistyped).' })
      } else {
        setResult({ ok: false, msg: `Server answered ${res.status} — check the URL.` })
      }
    } catch {
      setResult({
        ok: false,
        msg: 'Couldn’t reach the server from this browser. If the URL is meant for another machine, test from there.',
      })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="use-in__test">
      <button
        type="button"
        className="btn btn--sm"
        disabled={testing || !tokenValue}
        title={!tokenValue ? 'Create a token (or paste an existing one) first' : undefined}
        onClick={() => void run()}
      >
        {testing ? 'Testing…' : 'Test connection'}
      </button>
      {result && (
        <span className={result.ok ? 'use-in__test-ok' : 'use-in__test-fail'}>{result.msg}</span>
      )}
      <p className="modal__muted">
        Tests from this browser — a desktop on another machine must be able to reach the same
        address.
      </p>
    </div>
  )
}
