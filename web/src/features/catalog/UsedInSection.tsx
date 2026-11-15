import { useEffect, useState } from 'react'

import {
  ApiError,
  fetchLayerUsage,
  fetchRasterUsage,
  USAGE_DISCLAIMER,
  type LayerUsage,
  type UsageRef,
} from '@/lib/api'

/**
 * Read-only "Used in" section for the Manage dialogs (decision-where-used.md §4.4) — the non-admin
 * surface of the where-used capability, beside delete protection ("what depends on this"). Lists the
 * referencing items the CALLER can view, grouped by kind; non-viewable referencing items are omitted
 * server-side (§3.1), which is why the disclaimer renders verbatim under every non-empty list.
 * Read-only by design: no links, no management — the report and the delete warning are the actions.
 */
export default function UsedInSection({
  kind,
  itemId,
  accessToken,
}: {
  kind: 'layer' | 'raster'
  itemId: string
  accessToken: string
}) {
  const [usage, setUsage] = useState<LayerUsage | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setUsage(null)
    setError(null)
    const load =
      kind === 'layer'
        ? fetchLayerUsage(itemId, accessToken)
        : // Raster usage is the webMaps-only subset; widen it with empty layer fields so one
          // render path serves both kinds.
          fetchRasterUsage(itemId, accessToken).then((u) => ({
            ...u,
            viewLayers: [],
            geocoders: [],
            openData: false,
            homeFeatured: false,
          }))
    load
      .then((u) => {
        if (active) setUsage(u)
      })
      .catch((e: unknown) => {
        if (active) setError(e instanceof ApiError ? e.message : 'Couldn’t load usage')
      })
    return () => {
      active = false
    }
  }, [kind, itemId, accessToken])

  const groups: Array<[string, UsageRef[]]> = usage
    ? [
        ['Maps', usage.webMaps],
        ['Views', usage.viewLayers],
        ['Geocoders', usage.geocoders.map((g) => ({ id: g.id, title: g.name }))],
      ]
    : []
  const empty =
    usage !== null &&
    groups.every(([, items]) => items.length === 0) &&
    !usage.openData &&
    !usage.homeFeatured

  return (
    <section className="modal__section">
      <p className="modal__section-title">Used in</p>
      {usage === null && error === null && <p className="modal__muted">Loading…</p>}
      {error !== null && <p className="modal__muted">Couldn’t load usage ({error})</p>}
      {empty && <p className="modal__muted">Not used by anything you can view.</p>}
      {usage !== null && !empty && (
        <>
          {groups.map(
            ([label, items]) =>
              items.length > 0 && (
                <p key={label} className="modal__muted">
                  <strong>{label}:</strong> {items.map((i) => i.title).join(', ')}
                </p>
              ),
          )}
          {(usage.openData || usage.homeFeatured) && (
            <p className="modal__muted">
              {[usage.openData && 'Listed in Open Data', usage.homeFeatured && 'Featured on Home']
                .filter(Boolean)
                .join(' · ')}
            </p>
          )}
          <p className="modal__muted">
            <em>{USAGE_DISCLAIMER}</em>
          </p>
        </>
      )}
    </section>
  )
}
