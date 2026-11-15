# Basemap provisioning — admin runbook

*Audience: the deployment administrator.*

This runbook covers the **downloaded** street basemap: a single `.pmtiles` archive of your
jurisdiction, derived from OpenStreetMap via the official [Protomaps builds](https://maps.protomaps.com/builds).
The Light, Dark and Gray looks all render from this one archive. A portal can also list *streamed* maps,
which each viewer's browser fetches from OpenStreetMap or the U.S. Geological Survey with nothing to
install, and a new install starts that way. Download an area when you want maps that work with no
internet, the Light, Dark and Gray looks, Talus Field packs, or the map on report sheets. The choice
between the two is **Admin → Basemap → What this portal lists**. The archive lives in the media store, is covered by the standard
backup/DR cycle, and is served anonymously (public-map embeds need it) at
`/api/v1/basemap/archive`.

## Connected install (the normal path)

1. Sign in as an administrator and open **Admin → Basemap**.
2. Enter your jurisdiction's bounding box in WGS84 degrees (west, south, east, north) — or open
   the Map, pan/zoom to your area, come back, and press **Use last map view**.
3. Leave **Max zoom** at 15 (street detail) and **Build date** blank (= latest official build).
4. **Fetch basemap.** The server runs a bounded extract against the latest Protomaps planet
   build; a county-scale extract typically lands in tens of MB within a few minutes. Progress
   shows in the section; the map renders the new basemap on next load — no rebuild, no restart.

Guardrails: requests are capped at roughly state-scale (64 deg²); above ~1 deg² the UI warns
about size. One provisioning operation runs at a time.

## Air-gapped install

The fetch button needs egress to `build.protomaps.com` and `build-metadata.protomaps.dev`; without it, extract on ANY connected
machine (only Docker required) and walk the file in:

```bash
docker run --rm -v "$PWD:/out" protomaps/go-pmtiles extract \
  https://build.protomaps.com/$(date +%Y%m%d).pmtiles /out/jurisdiction.pmtiles \
  --bbox=WEST,SOUTH,EAST,NORTH --maxzoom=15
```

(Give your own area as west, south, east, north in WGS 84 degrees; if today's build isn't published yet, pick a recent
date from maps.protomaps.com/builds.) Then **Admin → Basemap → Upload .pmtiles…**. The file is
validated (PMTiles v3 header) in a staging location before it replaces anything; its bbox and
zoom are read from the archive itself. Upload cap: 4 GiB.

Labels and icons need no network either way — the fonts and symbols ship inside the web application,
so an air-gapped portal renders a complete street basemap with zero external requests. (Streamed maps
are the exception: each viewer's browser fetches them over the internet, so on a network with no outbound
access they do not load. After the upload, choose **Downloaded** under **What this portal lists** so they
are not offered; the street basemap is unaffected.)

**Order of operations:** on an air-gapped server the **Set on map** picker (the draw-a-box entry for
the fetch area and the organization's default extent) has nothing to draw on until a basemap exists.
Upload the packaged archive first; the picker and **Use basemap coverage** then work from it. On a
connected server the order doesn't matter — before any archive exists the picker opens on a streamed map
the portal offers.

## Updating

OpenStreetMap moves on; a basemap older than about six months is flagged on **Admin →
Health** and on the Basemap page. To update: **Admin → Basemap → Update basemap** (re-runs the extract against the
newest build) or upload a fresh file. The archive is replaced in place — clients pick it up on
next map load (the serving route revalidates by ETag within ~5 minutes).

## Operational notes

- **Storage:** `basemaps/current.pmtiles` in the media store (a file on the storage volume) — included in scheduled
  backups and the restore drill, but only as first archived: a replaced basemap is not archived again, so after
  a disaster-recovery restore fetch it again from **Admin → Basemap** (on an air-gapped server, upload the file
  you keep for it). Its area, build date, size and origin are recorded with the organization's settings.
- **Security posture:** anon read of exactly one fixed object; all writes admin-gated; fetch
  URLs constructed against the official hosts only; uploads validated in staging before
  promotion. Standing negative tests: `scripts/verify-invariants/inv19-basemap-gate.sh`.
