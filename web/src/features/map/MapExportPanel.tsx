import { useState } from 'react'
import CloseButton from '@/components/CloseButton'

// Map export (decision-map-export.md) — one left-rail tool, two tabs. Screenshot (PNG) is the quick
// paste-anywhere grab: current viewport + a drawn legend of the visible layers + the mandatory
// attribution footer, at native resolution. Print (PDF) is the templated one-page US-Letter document
// (title block + map + legend + scale bar + north arrow + attribution) at a chosen DPI. The panel is
// presentational: capture/composite/download live in mapExport.ts / mapExportPdf.ts, invoked through
// the callbacks so AssetMap keeps ownership of the map instance + layer state.

export type ExportOrientation = 'landscape' | 'portrait'

export interface MapExportPanelProps {
  /** Default PNG filename stem, e.g. "street-signs-20260712" (no extension). */
  defaultName: string
  /** Default PDF document title (the saved map title, else "Untitled map"). */
  defaultTitle: string
  /** Capture the current view → composite → download the PNG. Resolves to an error message on
   *  failure (e.g. a tainted canvas), or null on success. */
  onDownloadPng: (filename: string) => Promise<string | null>
  /** Render the one-page PDF at the chosen title/orientation/DPI and download it. Error or null. */
  onDownloadPdf: (opts: { title: string; orientation: ExportOrientation; dpi: number }) => Promise<string | null>
  /** The map is tilted (pitch ≠ 0): the PDF is exported as seen but cannot be georeferenced, and the
   *  Print tab says so (decision-geospatial-pdf-export.md §4.2). The panel is a modal, so the value
   *  read when it opens holds while it is open. */
  pitched: boolean
  onClose: () => void
}

type Tab = 'screenshot' | 'print'

const DPI_OPTIONS: { value: number; label: string }[] = [
  { value: 96, label: 'Screen' },
  { value: 150, label: '150 dpi' },
  { value: 300, label: '300 dpi' },
  { value: 600, label: '600 dpi' },
]

export default function MapExportPanel(props: MapExportPanelProps) {
  const [tab, setTab] = useState<Tab>('screenshot')
  const [name, setName] = useState(props.defaultName)
  const [title, setTitle] = useState(props.defaultTitle)
  const [orientation, setOrientation] = useState<ExportOrientation>('landscape')
  const [dpi, setDpi] = useState(300)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const switchTab = (t: Tab): void => {
    setTab(t)
    setError(null)
  }

  const run = async (fn: () => Promise<string | null>): Promise<void> => {
    setBusy(true)
    setError(null)
    try {
      const err = await fn()
      if (err) setError(err)
    } catch {
      setError('Export failed. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" role="presentation" onClick={props.onClose}>
    <div
      className="modal map-export"
      role="dialog"
      aria-modal="true"
      aria-label="Export map"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="add-layers__header">
        <span className="add-layers__title">Export map</span>
        <CloseButton onClick={props.onClose} className="add-layers__close" />
      </div>

      <div className="map-export__tabs" role="tablist" aria-label="Export format">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'screenshot'}
          className={`map-export__tab${tab === 'screenshot' ? ' is-active' : ''}`}
          onClick={() => switchTab('screenshot')}
        >
          Screenshot
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'print'}
          className={`map-export__tab${tab === 'print' ? ' is-active' : ''}`}
          onClick={() => switchTab('print')}
        >
          Print
        </button>
      </div>

      {tab === 'screenshot' ? (
        <div className="map-export__body">
          <label className="map-export__field">
            <span className="map-export__label">File name</span>
            <span className="map-export__namerow">
              <input
                className="add-layers__search"
                value={name}
                maxLength={120}
                onChange={(e) => setName(e.target.value)}
                aria-label="PNG file name"
              />
              <span className="map-export__ext">.png</span>
            </span>
          </label>
          <p className="map-export__hint">
            Saves the current map view — visible layers, a legend, and the required map attribution —
            as a PNG. Tool panels aren’t included.
          </p>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void run(() => props.onDownloadPng(name.trim() || 'map'))}
          >
            {busy ? 'Preparing…' : 'Download PNG'}
          </button>
          {error && (
            <p className="map-export__error" role="alert">
              {error}
            </p>
          )}
        </div>
      ) : (
        <div className="map-export__body">
          <label className="map-export__field">
            <span className="map-export__label">Title</span>
            <input
              className="add-layers__search"
              value={title}
              maxLength={120}
              onChange={(e) => setTitle(e.target.value)}
              aria-label="PDF title"
            />
          </label>

          <div className="map-export__field">
            <span className="map-export__label">Orientation</span>
            <div className="map-export__segmented" role="group" aria-label="Orientation">
              <button
                type="button"
                className={`map-export__seg${orientation === 'landscape' ? ' is-active' : ''}`}
                aria-pressed={orientation === 'landscape'}
                onClick={() => setOrientation('landscape')}
              >
                Landscape
              </button>
              <button
                type="button"
                className={`map-export__seg${orientation === 'portrait' ? ' is-active' : ''}`}
                aria-pressed={orientation === 'portrait'}
                onClick={() => setOrientation('portrait')}
              >
                Portrait
              </button>
            </div>
          </div>

          <label className="map-export__field">
            <span className="map-export__label">Resolution</span>
            <select
              className="add-layers__search"
              value={dpi}
              onChange={(e) => setDpi(Number(e.target.value))}
              aria-label="PDF resolution"
            >
              {DPI_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>

          <p className="map-export__hint">
            A one-page US&nbsp;Letter PDF — title, map, legend, scale bar, north arrow and attribution.
            Higher DPI is sharper for print (larger file); the map re-renders at that resolution.
          </p>
          <p className="map-export__hint">
            {props.pitched
              ? 'Tilted view: this PDF will not be georeferenced. Reset the tilt to get a map that opens in Avenza Maps.'
              : 'Georeferenced: opens as a map in Avenza Maps, QGIS and Acrobat.'}
          </p>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() =>
              void run(() => props.onDownloadPdf({ title: title.trim() || 'Map', orientation, dpi }))
            }
          >
            {busy ? 'Rendering…' : 'Download PDF'}
          </button>
          {error && (
            <p className="map-export__error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
    </div>
  )
}
