import { useMemo } from 'react'
import qrcode from 'qrcode-generator'

/**
 * QR of the composed Import URL (decision-qr-import-url.md). Client-side only — the
 * qrcode-generator matrix is drawn twice from the same modules: a crisp inline SVG preview,
 * and on demand a print-resolution PNG (canvas raster + caption strip) the project manager
 * saves or prints for crews. The PNG is a DELIBERATE durable credential export (F3/Security):
 * the caption carries the project title + token LABEL (never the token text) so a printed
 * sheet is attributable — and revoking the crew token kills every saved copy at once.
 *
 * Renders ONLY from the composed URL state (F2) — never from user-supplied markup.
 */

const QUIET = 4 // modules of quiet zone on every side (spec minimum)

function buildModules(value: string): boolean[][] {
  const qr = qrcode(0, 'M') // type 0 = auto-size; error correction M (F3)
  qr.addData(value)
  qr.make()
  const n = qr.getModuleCount()
  return Array.from({ length: n }, (_, r) =>
    Array.from({ length: n }, (_, c) => qr.isDark(r, c)),
  )
}

function safeFilename(title: string): string {
  return `${title.replace(/[^A-Za-z0-9_-]+/g, '_').replace(/^_+|_+$/g, '') || 'field-project'}-import-qr.png`
}

export default function ImportQr({
  value,
  title,
  tokenLabel,
}: {
  /** The COMPLETE credentialed Import URL (composed in the dialog; never a placeholder). */
  value: string
  title: string
  /** Caption attribution — the minted token's label; omit for pasted tokens. */
  tokenLabel?: string
}) {
  const modules = useMemo(() => buildModules(value), [value])
  const n = modules.length
  const size = n + QUIET * 2
  const path = useMemo(() => {
    let d = ''
    modules.forEach((row, r) =>
      row.forEach((dark, c) => {
        if (dark) d += `M${c + QUIET} ${r + QUIET}h1v1h-1z`
      }),
    )
    return d
  }, [modules])

  function downloadPng(): void {
    const scale = Math.ceil(1024 / size)
    const px = size * scale
    const captionLines = [title, tokenLabel ? `token: ${tokenLabel}` : null, 'Scan in QField ▸ Import URL'].filter(
      (l): l is string => l !== null,
    )
    const lineH = 34
    const captionH = captionLines.length * lineH + 18
    const canvas = document.createElement('canvas')
    canvas.width = px
    canvas.height = px + captionH
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = '#000000'
    modules.forEach((row, r) =>
      row.forEach((dark, c) => {
        if (dark) ctx.fillRect((c + QUIET) * scale, (r + QUIET) * scale, scale, scale)
      }),
    )
    ctx.textAlign = 'center'
    captionLines.forEach((line, i) => {
      ctx.fillStyle = i === 0 ? '#000000' : '#444444'
      ctx.font = i === 0 ? 'bold 26px system-ui, sans-serif' : '22px system-ui, sans-serif'
      ctx.fillText(line, px / 2, px + 8 + (i + 1) * lineH - 10, px - 2 * QUIET * scale)
    })
    canvas.toBlob((blob) => {
      if (!blob) return
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = safeFilename(title)
      a.click()
      URL.revokeObjectURL(url)
    }, 'image/png')
  }

  return (
    <div>
      <svg
        viewBox={`0 0 ${size} ${size}`}
        width={220}
        height={220}
        shapeRendering="crispEdges"
        role="img"
        aria-label="QR code of the Import URL"
        style={{ display: 'block', margin: '8px 0' }}
      >
        <rect width={size} height={size} fill="#ffffff" />
        <path d={path} fill="#000000" />
      </svg>
      <div className="sign-form__actions" style={{ justifyContent: 'flex-start' }}>
        <button type="button" className="btn btn--sm" onClick={downloadPng}>
          Download PNG
        </button>
      </div>
    </div>
  )
}
