import { useState } from 'react'
import { Check, Copy, MapPin } from 'lucide-react'

import { ApiError, reverseGeocode, searchSpatialRefs, transformPoint } from '@/lib/api'
import type { SpatialRef } from '@/lib/api'
import CloseButton from '@/components/CloseButton'

/**
 * "What's here?" coordinate readout (E2/reverse-geocode, decision-whats-here.md + decision-reverse-geocode.md).
 * Opened from the map's right-click context menu. Two modes: a tiny menu ("What's here?"), then the readout —
 * WGS-84 lon/lat, an optional projected-CRS reprojection (via /transform-point), and a reverse-geocoded address
 * (via /geocode/reverse, fail-soft → "No address found"). Map-level: the CRS is chosen here.
 */
export default function WhatsHerePopup({
  lng,
  lat,
  token,
  locator,
  onClose,
}: {
  lng: number
  lat: number
  token?: string
  /** The "Search in" picker's selected locator (null = All) — reverse uses the same service as forward search. */
  locator?: string | null
  onClose: () => void
}) {
  const [mode, setMode] = useState<'menu' | 'readout'>('menu')
  const [srid, setSrid] = useState<number | null>(null)
  const [sridName, setSridName] = useState('')
  const [xy, setXy] = useState<{ x: number; y: number } | null>(null)
  const [q, setQ] = useState('')
  const [results, setResults] = useState<SpatialRef[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const [address, setAddress] = useState<string | null>(null)
  const [addrState, setAddrState] = useState<'idle' | 'loading' | 'done'>('idle')

  const lonLat = `${lng.toFixed(6)}, ${lat.toFixed(6)}`

  // Open the readout + kick off the reverse-geocode (once). Fail-soft → addrState 'done' with address null.
  function openReadout(): void {
    setMode('readout')
    if (!token || addrState !== 'idle') return
    setAddrState('loading')
    reverseGeocode(lng, lat, token, locator)
      .then((a) => setAddress(a))
      .catch(() => setAddress(null))
      .finally(() => setAddrState('done'))
  }

  async function search(v: string): Promise<void> {
    setQ(v)
    if (!token) return
    try {
      setResults(await searchSpatialRefs(v, token))
    } catch {
      setResults([])
    }
  }
  async function pickCrs(r: SpatialRef): Promise<void> {
    setSrid(r.srid)
    setSridName(r.name)
    setQ('')
    setResults([])
    setXy(null)
    setErr(null)
    if (!token) return
    try {
      const t = await transformPoint(lng, lat, r.srid, token)
      setXy({ x: t.x, y: t.y })
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Transform failed')
    }
  }
  function copy(text: string, which: string): void {
    void navigator.clipboard?.writeText(text)
    setCopied(which)
    setTimeout(() => setCopied(null), 1200)
  }

  if (mode === 'menu') {
    return (
      <div className="whats-here whats-here--menu">
        <button type="button" className="whats-here__menu-item" onClick={openReadout}>
          <MapPin size={14} aria-hidden="true" /> What’s here?
        </button>
        {/* Dismiss without opening the readout — e.g. an accidental right-click. */}
        <CloseButton onClick={onClose} className="whats-here__close" />
      </div>
    )
  }

  const xyText = xy ? `${xy.x.toFixed(3)}, ${xy.y.toFixed(3)}` : null
  return (
    <div className="whats-here">
      <div className="whats-here__head">
        <strong>What’s here?</strong>
        <CloseButton onClick={onClose} className="whats-here__close" />
      </div>

      <div className="whats-here__row">
        <div>
          <div className="whats-here__coord">{lonLat}</div>
          <div className="whats-here__label modal__muted">Coordinates (lon, lat) · WGS 84</div>
        </div>
        <button type="button" className="whats-here__copy" title="Copy" onClick={() => copy(lonLat, 'wgs')}>
          {copied === 'wgs' ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
        </button>
      </div>

      {srid != null && (
        <div className="whats-here__row">
          <div>
            <div className="whats-here__coord">{xyText ?? (err ? '—' : '…')}</div>
            <div className="whats-here__label modal__muted">
              {sridName} (EPSG:{srid}) · X, Y
            </div>
            {err && <div className="whats-here__err">{err}</div>}
          </div>
          {xyText && (
            <button type="button" className="whats-here__copy" title="Copy" onClick={() => copy(xyText, 'xy')}>
              {copied === 'xy' ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
            </button>
          )}
        </div>
      )}

      <input
        type="text"
        className="whats-here__search"
        placeholder="Show in another CRS — search (e.g. 3857)"
        value={q}
        onChange={(e) => void search(e.target.value)}
      />
      {results.length > 0 && (
        <ul className="whats-here__results">
          {results.map((r) => (
            <li key={r.srid}>
              <button type="button" onClick={() => void pickCrs(r)}>
                {r.name} <span className="modal__muted">(EPSG:{r.srid})</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Reverse-geocoded address (decision-reverse-geocode.md): fail-soft via /geocode/reverse. */}
      <div className="whats-here__row">
        <div style={{ minWidth: 0 }}>
          {addrState === 'loading' ? (
            <div className="whats-here__label modal__muted">Looking up address…</div>
          ) : address ? (
            <>
              <div style={{ wordBreak: 'break-word' }}>{address}</div>
              <div className="whats-here__label modal__muted">Address</div>
            </>
          ) : (
            <div className="whats-here__label modal__muted">No address found</div>
          )}
        </div>
        {address && (
          <button type="button" className="whats-here__copy" title="Copy" onClick={() => copy(address, 'addr')}>
            {copied === 'addr' ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          </button>
        )}
      </div>
    </div>
  )
}
