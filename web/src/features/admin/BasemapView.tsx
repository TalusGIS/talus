import BasemapSection from '@/features/admin/BasemapSection'

/**
 * Admin → Map data → Basemap (handoff-admin-1a §3.5): BasemapSection promoted from a card
 * inside the Settings catch-all to its own routed destination (finding D2 — Basemap and
 * Geocoders are the same kind of thing, map-data providers, and now live together). The
 * section already owns its load/save cycle against /admin/basemap — this adds only the page
 * header and a linkable home, so Health's staleness nudge can point somewhere real.
 */
export default function BasemapView() {
  return (
    <div className="admin">
      <h1 className="admin__title">Basemap</h1>
      <p className="admin__note">
        What maps in this portal draw behind your data. A streamed map comes straight from its
        publisher over the internet; a downloaded area is stored on this server and works with
        no internet connection.
      </p>
      <BasemapSection />
    </div>
  )
}
