import { useCallback, useEffect, useRef, useState } from 'react'
import { LIGHT, layers as basemapLayers } from '@protomaps/basemaps'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

import { basemapArchiveUrl } from '@/lib/api'
import type { GeoCandidate, StreamedBasemap } from '@/lib/api'
import CloseButton from '@/components/CloseButton'
import {
  ensurePmtilesProtocol,
  evictPmtilesArchive,
  GLYPHS,
  SPRITE,
  streamedRasterSource,
  whenStyleReady,
} from '@/features/map/basemapRuntime'
import { startBoxDraw, type DrawBbox } from '@/features/map/drawBox'
import GeocodeSearch from '@/features/map/GeocodeSearch'

/**
 * The map-based Set Extent picker (decision-extent-picker.md) — AGO's "draw a rectangle" input
 * surface, expanded inline beneath a bbox input group (fork A: it drives the CONSUMER's own
 * inputs live, so the page's existing Fetch/Save buttons stay the only commit gates; there is no
 * Apply/Cancel here). Two consumers today, both on Settings → Basemap: the basemap-fetch bbox and
 * the V094 org default extent. Correction is redraw-to-replace (fork B) — the inputs are the
 * precision path.
 *
 * What the map draws on (fork C, restated by decision-streamed-basemap.md §15.10): a provisioned
 * archive (Light flavor), else a STREAMED map the organization offers — the bootstrap aid, which
 * before V115 was always USGS imagery. Provisioning is resolved ONLY via `installed` (the admin
 * status), never the DEV fallback archive, so the dev fixture cannot mask unprovisioned behaviour
 * on this surface. With no archive and no streamed map offered there is nothing to draw on: the
 * consumer hides the button and says so, and never mounts this component (the kickoff §0 ruling).
 *
 * `streamed` is a NAME the consumer took from the organization's offered list (`offeredStreamed`).
 * This component never reads a build flag or picks a publisher itself, and it has no way to be
 * handed an address: the name becomes one inside basemapRuntime.ts, as everywhere else. If it is
 * mounted with neither an archive nor a name it draws an empty style and fetches nothing.
 */

export interface ExtentPickerProps {
  /** The consumer's current bbox when complete and valid, else null — framed on open and shown
   *  as a persistent overlay box that follows typed edits. */
  value: DrawBbox | null
  /** Live write-through: fired per drag frame and at mouseup. `null` restores a draw session's
   *  pre-session value on Esc (which may clear inputs that held nothing valid). */
  onChange: (bbox: DrawBbox | null) => void
  accessToken: string | undefined
  /** BasemapAdminStatus.installed — true draws the organization's own archive. */
  installed: boolean
  /** What to draw when nothing is installed: a streamed map this organization OFFERS, by name, or
   *  null for none. Ignored while `installed` is true. */
  streamed: StreamedBasemap | null
  /** Installed archive coverage, for initial framing when `value` is null. */
  coverageBbox: DrawBbox | null
  onClose: () => void
}

const VALUE_SRC = '__extent-pick-value'
const VALUE_FILL = '__extent-pick-value-fill'
const VALUE_LINE = '__extent-pick-value-line'
const STREAM_SRC = '__extent-pick-streamed'
// The drawn-box blue drawBox.ts defaults to; the ephemeral search pin matches AssetMap's.
const BOX_COLOR = '#1f4d7a'
const MARKER_COLOR = '#e8602d'
const EMPTY_FC: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

function bboxPolygon([w, s, e, n]: DrawBbox): GeoJSON.Feature {
  return {
    type: 'Feature',
    properties: {},
    geometry: {
      type: 'Polygon',
      coordinates: [[[w, s], [w, n], [e, n], [e, s], [w, s]]],
    },
  }
}

/** ~0.11 m — enough for any jurisdiction bbox, and keeps drawn floats out of the inputs. */
const round6 = (b: DrawBbox): DrawBbox => b.map((v) => Number(v.toFixed(6))) as DrawBbox

export default function ExtentPicker({
  value,
  onChange,
  accessToken,
  installed,
  streamed,
  coverageBbox,
  onClose,
}: ExtentPickerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const markerRef = useRef<maplibregl.Marker | null>(null)
  const drawCleanupRef = useRef<(() => void) | null>(null)
  const [styleReady, setStyleReady] = useState(false)
  const [drawing, setDrawing] = useState(false)
  // Latest props for the map-lifetime closures (the construct effect deliberately depends only on
  // `installed` and the `streamed` name: the job poll hands the consumer a NEW coverage array every 2.5 s, and depending
  // on it would rebuild the map mid-edit on every poll tick).
  const valueRef = useRef(value)
  valueRef.current = value
  const coverageRef = useRef(coverageBbox)
  coverageRef.current = coverageBbox
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    let style: maplibregl.StyleSpecification
    if (installed) {
      ensurePmtilesProtocol()
      const archiveUrl = new URL(basemapArchiveUrl(), window.location.origin).toString()
      // A fresh box's earlier 404 poisons the protocol's header-promise cache for this exact URL
      // (the admin has typically just provisioned in THIS session) — evict before construction,
      // same as AssetMap's map-construction effect.
      evictPmtilesArchive(archiveUrl)
      style = {
        version: 8,
        glyphs: GLYPHS,
        sprite: SPRITE,
        sources: {
          protomaps: {
            type: 'vector',
            url: `pmtiles://${archiveUrl}`,
            attribution:
              '<a href="https://protomaps.com">Protomaps</a> © <a href="https://openstreetmap.org">OpenStreetMap</a>',
          },
        },
        layers: basemapLayers('protomaps', LIGHT, { lang: 'en' }),
      }
    } else if (streamed !== null) {
      style = {
        version: 8,
        sources: { [STREAM_SRC]: streamedRasterSource(streamed) },
        layers: [{ id: `${STREAM_SRC}-raster`, type: 'raster', source: STREAM_SRC }],
      }
    } else {
      // Nothing downloaded and nothing offered. The consumer should not have mounted the picker;
      // if it did, draw nothing rather than reach for a publisher the organization has not chosen.
      style = { version: 8, sources: {}, layers: [] }
    }
    const frame = valueRef.current ?? coverageRef.current
    const map = new maplibregl.Map({
      container,
      style,
      ...(frame !== null
        ? { bounds: frame, fitBoundsOptions: { padding: 32 } }
        : { center: [0, 20], zoom: 1 }),
      // OpenStreetMap's credit must stay in view while its tiles draw, not sit behind the compact
      // button, which closes on the first drag (OSMF attribution guidelines; §14.3).
      attributionControl: { compact: installed || streamed !== 'osm' },
    })
    mapRef.current = map
    // The panel has just expanded: one post-layout resize so a 0-measure at construction can
    // never pin maplibre's 400×300 default (the kickoff trap §4.2 rule — kept regardless).
    requestAnimationFrame(() => map.resize())
    const unsubscribe = whenStyleReady(map, () => {
      map.addSource(VALUE_SRC, { type: 'geojson', data: EMPTY_FC })
      map.addLayer({
        id: VALUE_FILL,
        type: 'fill',
        source: VALUE_SRC,
        paint: { 'fill-color': BOX_COLOR, 'fill-opacity': 0.12 },
      })
      map.addLayer({
        id: VALUE_LINE,
        type: 'line',
        source: VALUE_SRC,
        paint: { 'line-color': BOX_COLOR, 'line-width': 2 },
      })
      setStyleReady(true)
    })
    return () => {
      unsubscribe()
      drawCleanupRef.current?.()
      drawCleanupRef.current = null
      markerRef.current?.remove()
      markerRef.current = null
      map.remove()
      mapRef.current = null
      setStyleReady(false)
      setDrawing(false)
    }
  }, [installed, streamed])

  // The persistent overlay mirrors the consumer's value (typed edits included). Hidden while a
  // draw session runs — the session's own rubber band is the live feedback then.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !styleReady) return
    const src = map.getSource(VALUE_SRC) as maplibregl.GeoJSONSource | undefined
    src?.setData(value !== null && !drawing ? bboxPolygon(value) : EMPTY_FC)
  }, [value, styleReady, drawing])

  function startDraw(): void {
    const map = mapRef.current
    if (!map || !styleReady || drawing) return
    const before = valueRef.current // Esc restores what the inputs held when the session began
    setDrawing(true)
    drawCleanupRef.current = startBoxDraw(
      map,
      {
        onProgress: (b) => onChangeRef.current(round6(b)),
        onComplete: (b) => {
          drawCleanupRef.current = null
          setDrawing(false)
          onChangeRef.current(round6(b))
        },
        onCancel: () => {
          drawCleanupRef.current = null
          setDrawing(false)
          onChangeRef.current(before)
        },
      },
      { idPrefix: '__extent-pick-draw' },
    )
  }

  const flyToCandidate = useCallback((c: GeoCandidate): void => {
    const map = mapRef.current
    if (!map) return
    markerRef.current?.remove()
    markerRef.current = new maplibregl.Marker({ color: MARKER_COLOR })
      .setLngLat([c.lon, c.lat])
      .addTo(map)
    // Town scale, not AssetMap's address scale (z17) — the user lands to frame a jurisdiction,
    // not to inspect a parcel (fork F).
    map.flyTo({ center: [c.lon, c.lat], zoom: Math.max(map.getZoom(), 12), essential: true })
  }, [])

  const clearMarker = useCallback((): void => {
    markerRef.current?.remove()
    markerRef.current = null
  }, [])

  const getViewportBBox = useCallback((): string | undefined => {
    const map = mapRef.current
    if (!map) return undefined
    const b = map.getBounds()
    return `${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`
  }, [])

  return (
    <div className="extent-picker">
      <div className="extent-picker__toolbar">
        <button
          type="button"
          className="btn btn--sm"
          onClick={startDraw}
          disabled={!styleReady || drawing}
        >
          {drawing ? 'Drag on the map…' : value !== null ? 'Redraw box' : 'Draw box'}
        </button>
        <span className="sa__muted">
          Drag a box — the coordinates above follow. Draw again to replace; Esc cancels.
        </span>
        <CloseButton
          onClick={onClose}
          label="Close the map picker"
          className="extent-picker__close"
        />
      </div>
      <div className="extent-picker__map" ref={containerRef} />
      <GeocodeSearch
        accessToken={accessToken}
        onPick={flyToCandidate}
        onClear={clearMarker}
        getViewportBBox={getViewportBBox}
      />
    </div>
  )
}
