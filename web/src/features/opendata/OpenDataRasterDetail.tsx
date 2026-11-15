import { useEffect, useState } from 'react'
import { useParams } from 'react-router'
import { useAuth } from 'react-oidc-context'
import { Download } from 'lucide-react'

import PanelSection from '@/components/PanelSection'

import {
  ApiError,
  exportRaster,
  fetchOpenRasterDataset,
  type OpenRasterDataset,
} from '@/lib/api'
import {
  ApiSection,
  DetailHero,
  downloadErrorMessage,
  FactStrip,
  formatLabel,
  MetadataDownload,
  NotFoundPage,
} from './parts'

/**
 * A single curated public RASTER's page (raster-public slice) — the imagery/elevation twin of
 * OpenDataDetail: published metadata, a thumbnail preview off the anon read path, GeoTIFF/COG
 * downloads (only while the raster is downloadable — view-only rasters still stream), and the
 * standard connect endpoints (WMTS always; WCS live values when downloadable). Anon-capable; a
 * raster that isn't curated-and-visible 404s server-side and renders as "not found" here.
 */
export default function OpenDataRasterDetail() {
  const { rasterId = '' } = useParams<{ rasterId: string }>()
  const auth = useAuth()
  // Token ONLY when truly signed in — the public page must degrade to anon, not error (the
  // OpenDataDetail lesson: a lingering expired token would 401 the fetch).
  const token = auth.isAuthenticated ? auth.user?.access_token : undefined

  const [ds, setDs] = useState<OpenRasterDataset | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [thumbOk, setThumbOk] = useState(true)

  useEffect(() => {
    let active = true
    setDs(null)
    setNotFound(false)
    fetchOpenRasterDataset(rasterId, token)
      .then((d) => active && setDs(d))
      .catch((e) => active && setNotFound(e instanceof ApiError && e.status === 404))
    return () => {
      active = false
    }
  }, [rasterId, token])

  if (notFound) return <NotFoundPage />
  if (!ds) {
    return (
      <div className="opendata opendata--detail">
        <div className="opendata__body">
          <p className="opendata__muted">Loading…</p>
        </div>
      </div>
    )
  }

  return (
    <div className="opendata opendata--detail">
      <DetailHero title={ds.title} lede={ds.abstract} />

      <FactStrip
        cells={[
          { label: 'Type', value: 'Imagery / raster' },
          { label: 'Bands', value: ds.bandCount ? String(ds.bandCount) : '', mono: true },
          {
            label: 'Updated',
            value: ds.updatedAt ? new Date(ds.updatedAt).toISOString().slice(0, 10) : '',
            mono: true,
          },
          { label: 'License', value: ds.license ?? '' },
        ]}
      />

      <div className="opendata__body opendata__split">
        <div className="opendata__main">
          {/* A non-downloadable raster still shows its thumbnail — view-only is not empty. */}
          {thumbOk && (
            <img
              className="opendata__raster-thumb"
              src={ds.thumbnailUrl}
              alt={`Preview of ${ds.title}`}
              onError={() => setThumbOk(false)}
            />
          )}

          <section className="opendata__tools">
            <h2 className="opendata__section-title">Use it from your own tools</h2>
            <ApiSection
              title="WMTS (stream the imagery)"
              note="Standard tiled-imagery endpoint — add it to QGIS, ArcGIS Pro, or any WMTS client."
              url={ds.wmtsUrl}
              standard
            />
            {ds.wcsUrl && (
              <ApiSection
                title="WCS (live raster values)"
                note="Standard coverage endpoint — read the actual pixel values (elevations, measurements) in QGIS, ArcGIS Pro, or GDAL."
                url={ds.wcsUrl}
                standard
              />
            )}
            <PanelSection title="Metadata">
              <MetadataDownload kind="raster" id={ds.rasterId} token={token} />
            </PanelSection>
          </section>
        </div>

        <aside className="opendata__rail">
          {ds.downloadable ? (
            <Downloads ds={ds} token={token} />
          ) : (
            <div className="opendata__dlcard">
              <h2 className="opendata__dlcard-title">Download</h2>
              <p className="opendata__muted opendata__muted--sm">
                This raster is view/stream-only — downloads aren’t offered, but you can view it here
                and stream it into a desktop GIS via WMTS.
              </p>
            </div>
          )}
          <About ds={ds} />
        </aside>
      </div>
    </div>
  )
}

function About({ ds }: { ds: OpenRasterDataset }) {
  if (!ds.ownerName && !ds.topicCategory && ds.keywords.length === 0) return null
  return (
    <div className="opendata__about">
      {ds.ownerName && (
        <div className="opendata__about-row">
          <span className="opendata__about-label">Published by</span>
          <span className="opendata__about-value">{ds.ownerName}</span>
        </div>
      )}
      {ds.topicCategory && (
        <div className="opendata__about-row">
          <span className="opendata__about-label">Topic</span>
          <span className="opendata__about-value">{ds.topicCategory}</span>
        </div>
      )}
      {ds.keywords.length > 0 && (
        <div className="opendata__about-row">
          <span className="opendata__about-label">Keywords</span>
          <span className="opendata__formats">
            {ds.keywords.map((k) => (
              <span key={k} className="chip">
                {k}
              </span>
            ))}
          </span>
        </div>
      )}
    </div>
  )
}

function Downloads({ ds, token }: { ds: OpenRasterDataset; token?: string }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  async function run(format: string) {
    setErr(null)
    setBusy(format)
    try {
      const result = await exportRaster(ds.rasterId, { format }, token)
      const url = URL.createObjectURL(result.blob)
      const a = document.createElement('a')
      a.href = url
      a.download = result.filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setErr(downloadErrorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const [primary, ...rest] = ds.formats
  if (!primary) return null
  return (
    <div className="opendata__dlcard">
      <h2 className="opendata__dlcard-title">Download</h2>
      <button
        type="button"
        className="opendata__dl opendata__dl--primary"
        disabled={busy !== null}
        onClick={() => void run(primary)}
      >
        <Download size={15} aria-hidden="true" />
        <span>{busy === primary ? 'Preparing…' : formatLabel(primary)}</span>
      </button>
      {rest.map((f) => (
        <button
          key={f}
          type="button"
          className="opendata__dl"
          disabled={busy !== null}
          onClick={() => void run(f)}
        >
          <Download size={14} aria-hidden="true" />
          <span>{busy === f ? 'Preparing…' : formatLabel(f)}</span>
        </button>
      ))}
      {err && <p className="opendata__error">{err}</p>}
    </div>
  )
}
