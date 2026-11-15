import { useEffect, useState } from 'react'

import { fetchRasterThumbnail } from '@/lib/api'

import { RasterIcon } from './ItemTypeIcons'

/**
 * The catalog thumbnail for a raster row (V045+). The thumbnail endpoint is auth-gated, so a plain
 * `<img src>` can't carry the bearer token — we fetch the bytes and render them via an object URL,
 * falling back to the generic raster icon when there's no thumbnail or the fetch fails.
 */
export default function RasterThumb({
  id,
  hasThumbnail,
  token,
  className,
}: {
  id: string
  hasThumbnail: boolean
  token: string | undefined
  className?: string
}) {
  const [url, setUrl] = useState<string | null>(null)

  useEffect(() => {
    if (!hasThumbnail || !token) return
    let active = true
    let obj: string | null = null
    fetchRasterThumbnail(id, token)
      .then((blob) => {
        if (!active) return
        obj = URL.createObjectURL(blob)
        setUrl(obj)
      })
      .catch(() => {
        /* fall back to the icon */
      })
    return () => {
      active = false
      if (obj) URL.revokeObjectURL(obj)
    }
  }, [id, hasThumbnail, token])

  if (url) return <img src={url} className={`${className ?? ''} catalog__thumb`} alt="" />
  return <RasterIcon className={className} />
}
