import { useEffect, useState } from 'react'

import { apiOrigin, fetchOrgSettings } from '@/lib/api'
import {
  buildProOgcFile,
  buildProWmtsFile,
  buildQgisConnectionsXml,
  buildQgisWcsConnectionsXml,
  buildQgisWmsConnectionsXml,
  downloadTextFile,
} from '@/lib/connectionFiles'

/**
 * Profile → Step 2 "Download a connection file" (Interop I-4 + I-2): the deployment-level
 * connection files — credential-free, whole-catalog, machine-scoped (hence it lives here on
 * Profile, not on any layer). Download → import in the desktop app → enter your own access token
 * once when prompted → every layer/raster you can view is available, including ones shared to you
 * later. Vector rides the OGC door (QGIS OAPIF XML + Pro `.ogc`); imagery rides the WMTS door
 * (QGIS WMS/WMTS XML + Pro `.wmts`); live raster values ride the WCS door (QGIS WCS XML; Pro has
 * no byte-captured `.wcs` yet — its button copies the server URL for New WCS Server instead,
 * decision-interop-i6-coverages.md fork 6).
 *
 * The step heading + lede live in ProfileView's spine; this renders the two-card matrix (Vector /
 * Imagery, each offering QGIS + ArcGIS Pro) plus the one shared sign-in note beneath it.
 */

// Small inline glyphs (stroke = currentColor so they inherit the token color of their box).
function IconLayers() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m12 2 9 5-9 5-9-5 9-5Z" />
      <path d="m3 12 9 5 9-5" />
      <path d="m3 17 9 5 9-5" />
    </svg>
  )
}

function IconImage() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="9" cy="9" r="2" />
      <path d="m21 15-3.6-3.6a2 2 0 0 0-2.8 0L6 20" />
    </svg>
  )
}

function IconDownload() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="m7 10 5 5 5-5" />
      <path d="M12 15V3" />
    </svg>
  )
}

function IconInfo() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
  )
}

function IconCrosshair() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 2v4M12 18v4M2 12h4M18 12h4" />
    </svg>
  )
}

export default function ConnectComputerCard({ accessToken }: { accessToken: string }) {
  const [advertised, setAdvertised] = useState<string | null>(null)
  const [wcsUrlCopied, setWcsUrlCopied] = useState(false)

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
  const host = base.replace(/^https?:\/\//, '')
  // Windows-filename-safe host (Pro's own convention for its .ogc names: ':' → '_').
  const safeHost = host.replace(/[:/\\]/g, '_')

  function downloadQgis(): void {
    downloadTextFile(
      'talus-qgis-connection.xml',
      buildQgisConnectionsXml(`Talus GIS (${host})`, `${base}/api/v1/ogc-auth`),
    )
  }

  function downloadPro(): void {
    downloadTextFile(
      `Talus GIS (${safeHost}).ogc`,
      buildProOgcFile(`Talus GIS (${safeHost})`, `${base}/api/v1/ogc-auth`),
      'application/octet-stream',
    )
  }

  function downloadQgisImagery(): void {
    downloadTextFile(
      'talus-qgis-imagery-connection.xml',
      buildQgisWmsConnectionsXml(
        `Talus GIS imagery (${host})`,
        `${base}/api/v1/wmts-auth/1.0.0/WMTSCapabilities.xml`,
      ),
    )
  }

  function downloadProImagery(): void {
    downloadTextFile(
      `Talus GIS — WMTS (${safeHost}).wmts`,
      buildProWmtsFile(
        `Talus GIS — WMTS (${safeHost})`,
        `${base}/api/v1/wmts-auth/1.0.0/WMTSCapabilities.xml`,
      ),
      'application/octet-stream',
    )
  }

  function downloadQgisValues(): void {
    downloadTextFile(
      'talus-qgis-values-connection.xml',
      buildQgisWcsConnectionsXml(`Talus GIS values (${host})`, `${base}/api/v1/wcs-auth`),
    )
  }

  function copyProValuesUrl(): void {
    void navigator.clipboard.writeText(`${base}/api/v1/wcs-auth`).then(() => {
      setWcsUrlCopied(true)
      window.setTimeout(() => setWcsUrlCopied(false), 2500)
    })
  }

  return (
    <>
      <div className="connect-cards">
        <div className="connect-card">
          <div className="connect-card__head">
            <span className="connect-card__icon">
              <IconLayers />
            </span>
            <span className="connect-card__title">Vector layers</span>
          </div>
          <div className="connect-card__sub">Features over OGC API — WFS</div>
          <div className="connect-card__actions">
            <button type="button" className="connect-dlbtn" onClick={downloadQgis}>
              <IconDownload /> QGIS
            </button>
            <button type="button" className="connect-dlbtn" onClick={downloadPro}>
              <IconDownload /> ArcGIS Pro
            </button>
          </div>
        </div>

        <div className="connect-card">
          <div className="connect-card__head">
            <span className="connect-card__icon">
              <IconImage />
            </span>
            <span className="connect-card__title">Imagery</span>
          </div>
          <div className="connect-card__sub">Rasters over WMTS</div>
          <div className="connect-card__actions">
            <button type="button" className="connect-dlbtn" onClick={downloadQgisImagery}>
              <IconDownload /> QGIS
            </button>
            <button type="button" className="connect-dlbtn" onClick={downloadProImagery}>
              <IconDownload /> ArcGIS Pro
            </button>
          </div>
        </div>

        <div className="connect-card">
          <div className="connect-card__head">
            <span className="connect-card__icon">
              <IconCrosshair />
            </span>
            <span className="connect-card__title">Raster values</span>
          </div>
          <div className="connect-card__sub">Live pixel values over WCS</div>
          <div className="connect-card__actions">
            <button type="button" className="connect-dlbtn" onClick={downloadQgisValues}>
              <IconDownload /> QGIS
            </button>
            {/* No byte-captured Pro .wcs yet (I-6 fork 6) — Pro users paste this URL into
                Insert ▸ Connections ▸ New WCS Server instead. */}
            <button type="button" className="connect-dlbtn" onClick={copyProValuesUrl}>
              {wcsUrlCopied ? 'Copied!' : 'Copy URL for Pro'}
            </button>
          </div>
          {/* ArcGIS Pro's WCS 1.0.0 client returns values on identify but does not render the
              coverage (rig-verified 2026-07-22: valid tile + valid stretch, still paints nothing —
              a Pro-side limitation, not a serving gap). QGIS renders WCS fine, so this caveat is
              Pro-only. Steer Pro users who want a visible layer to the WMTS/Imagery door. */}
          <div className="connect-card__hint">
            In ArcGIS Pro this returns values on identify, not a drawn layer — add{' '}
            <strong>Imagery</strong> to display.
          </div>
        </div>
      </div>

      <div className="connect-note">
        <span className="connect-note__icon">
          <IconInfo />
        </span>
        <span>
          After importing, Connect and sign in with username{' '}
          <span className="connect-chip">talus</span> and your <strong>step 1</strong> token as the
          password. The app remembers it — you won’t be asked again on this computer.
        </span>
      </div>

      {/* Pro's "New ArcGIS Server Connection" dialog validates by Esri's proprietary Admin API and
          fails against Talus even with a valid token (decision-pro-server-connection.md). Steer
          here, at the deployment-wide hand-off, before a Pro user can reach for it. */}
      <div className="connect-note">
        <span className="connect-note__icon">
          <IconInfo />
        </span>
        <span>
          <strong>ArcGIS Pro:</strong> the files above are the connection — skip Pro’s{' '}
          <em>New ArcGIS Server Connection</em> dialog, which requires Esri’s proprietary
          server-admin API and fails against Talus (“unable to connect”, even with a valid token).
          To <strong>edit</strong> a layer in Pro, paste its FeatureServer link from the layer’s{' '}
          <strong>Use in…</strong> wizard via Add Data From Path.
        </span>
      </div>

      {advertised === null && (
        <p className="connect-note" style={{ color: 'var(--text-subtle)' }}>
          <span className="connect-note__icon">
            <IconInfo />
          </span>
          <span>
            Connection URLs use this portal’s API address. An administrator can set the
            organization’s advertised base URL under Admin → Connections so they always show an address
            other machines can reach.
          </span>
        </p>
      )}
    </>
  )
}
