import { useEffect, useState } from 'react'

import { fetchAttachmentBlob, fetchAttachmentThumbnailBlob, fetchFeatureAttachments } from '@/lib/api'
import type { FeatureAttachment } from '@/lib/api'

/**
 * The A-4 popup attachments strip (decision-attachments-a4.md §2/§3). A READ surface on a feature's
 * popup page: the first 4 attachments as server-resized image thumbnails or typed file chips, plus a
 * "+N more → view in details" overflow into the record-detail drawer. No upload/delete/rename here —
 * management is the drawer (the popup's Details icon → onDetails).
 *
 * Lazy by construction: it does nothing until it mounts (the popup page rendered), and nothing at all
 * on attachment-less layers — it first awaits the per-layer `probe` (promise-cached once per layer per
 * map session by AssetMap), and only then fetches this feature's list. Image bytes are fetched to a Blob
 * the component turns into an object URL it owns (a bearer-gated <img src> can't carry Authorization);
 * every URL is revoked on unmount / feature change.
 */
export interface AttachmentStripProps {
  layerId: string
  featureId: string
  /** Bearer for authed maps; omitted on a public/anon map (the fetches are token-optional). */
  token?: string
  /** Per-layer has-attachments probe, cached in AssetMap. `false` → the strip renders nothing. */
  probe: Promise<boolean>
  /** Open the record-detail drawer (view-all / manage). */
  onDetails?: () => void
}

const CAP = 4

interface StripItem {
  att: FeatureAttachment
  thumbUrl: string | null // object URL for a decodable image thumbnail; null → typed file chip
}

export default function AttachmentStrip({ layerId, featureId, token, probe, onDetails }: AttachmentStripProps) {
  const [items, setItems] = useState<StripItem[]>([])
  const [total, setTotal] = useState(0)

  useEffect(() => {
    let cancelled = false
    const ctrl = new AbortController()
    const urls: string[] = []
    void (async () => {
      // 1) per-layer probe (cached): attachment-less layers stop here — no per-feature request.
      const has = await probe.catch(() => false)
      if (cancelled || !has) return
      // 2) this feature's list (view-gated; anon-when-public). Empty → nothing renders.
      let list: FeatureAttachment[]
      try {
        list = (await fetchFeatureAttachments(layerId, featureId, token, ctrl.signal)).attachments
      } catch {
        return
      }
      if (cancelled || list.length === 0) return
      setTotal(list.length)
      // 3) thumbnails for the first 4 images; non-images (and undecodable images → 404) become file chips.
      const built = await Promise.all(
        list.slice(0, CAP).map(async (att): Promise<StripItem> => {
          if (att.content_type.startsWith('image/')) {
            try {
              const blob = await fetchAttachmentThumbnailBlob(layerId, att.id, token, ctrl.signal)
              const u = URL.createObjectURL(blob)
              urls.push(u)
              return { att, thumbUrl: u }
            } catch {
              /* undecodable → fall through to a chip */
            }
          }
          return { att, thumbUrl: null }
        }),
      )
      if (cancelled) return
      setItems(built)
    })()
    return () => {
      cancelled = true
      ctrl.abort()
      urls.forEach((u) => URL.revokeObjectURL(u))
    }
  }, [layerId, featureId, token, probe])

  if (items.length === 0) return null

  // Full-size view / download: fetch to a Blob the browser can open in a new tab (bearer-gated links
  // can't carry auth). Revoke after the tab has had time to load.
  const openFull = async (att: FeatureAttachment): Promise<void> => {
    try {
      const blob = await fetchAttachmentBlob(layerId, att.id, token)
      const u = URL.createObjectURL(blob)
      window.open(u, '_blank', 'noopener')
      window.setTimeout(() => URL.revokeObjectURL(u), 60_000)
    } catch {
      /* ignore — a failed open is non-fatal for a read surface */
    }
  }

  const more = total - items.length
  return (
    <div className="attach-strip">
      <div className="attach-strip__items">
        {items.map(({ att, thumbUrl }) =>
          thumbUrl ? (
            <button
              key={att.id}
              type="button"
              className="attach-strip__thumb"
              title={att.filename}
              onClick={() => void openFull(att)}
            >
              <img src={thumbUrl} alt={att.filename} />
            </button>
          ) : (
            <button
              key={att.id}
              type="button"
              className="attach-strip__chip"
              title={att.filename}
              onClick={() => void openFull(att)}
            >
              <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
                <path
                  fill="currentColor"
                  d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 2 4 4h-4V4Z"
                />
              </svg>
              <span className="attach-strip__name">{att.filename}</span>
            </button>
          ),
        )}
      </div>
      {more > 0 && onDetails && (
        <button type="button" className="attach-strip__more" onClick={onDetails}>
          +{more} more — view in details
        </button>
      )}
    </div>
  )
}
