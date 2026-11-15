import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import { ApiError, fetchLayerColumns, queryLayerFeatures } from '@/lib/api'
import type { GeometryType, LayerColumn, LayerFeature } from '@/lib/api'

import RecordDetail from './RecordDetail'
import RecordDrawer from './RecordDrawer'

/**
 * Self-fetching wrapper around RecordDetail for callers that have only (layerId, recordId) —
 * the map popup's "Details" action. It loads the same generic data the catalog table already
 * holds (column schema + /domains + the single feature via an id:eq query), so the drawer
 * renders IDENTICALLY from either surface. The catalog table feeds RecordDetail directly
 * (it already has the data); the map uses this loader.
 */
export interface RecordDetailLoaderProps {
  layerId: string
  layerLabel: string
  recordId: string
  onClose: () => void
  /** Layer identity the drawer needs and /layers/{id}/columns does not carry — only /me/layers does,
   *  so the map hands them down from the layer it already has (handoff-record-table-3 §7.1). */
  displayField?: string | null
  geomType?: GeometryType | 'none' | null
  displayColor?: string | null
  /** Live-map PNG capture for the Talus+ "Generate sheet" action (absent off the map). */
  captureMap?: () => Promise<Blob | null>
}

interface Loaded {
  columns: LayerColumn[]
  properties: Record<string, unknown>
  /** Kept, not dropped: the location strip (§3.2) needs it and it rides this very response. */
  geometry: LayerFeature['geometry']
}

export default function RecordDetailLoader({
  layerId,
  layerLabel,
  recordId,
  onClose,
  displayField,
  geomType,
  displayColor,
  captureMap,
}: RecordDetailLoaderProps) {
  const auth = useAuth()
  const token = auth.user?.access_token
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!token) return
    let active = true
    const ctrl = new AbortController()
    setData(null)
    setError(null)
    Promise.all([
      fetchLayerColumns(layerId, token),
      queryLayerFeatures(layerId, { filters: [`id:eq:${recordId}`], limit: 1 }, token, ctrl.signal),
    ])
      .then(([cols, fc]) => {
        if (!active) return
        const feature = fc.features[0]
        if (!feature) {
          setError('record not found')
          return
        }
        setData({
          columns: cols.columns,
          properties: feature.properties,
          geometry: feature.geometry,
        })
      })
      .catch((e) => {
        if (!active || e?.name === 'AbortError') return
        setError(e instanceof ApiError ? e.message : String(e))
      })
    return () => {
      active = false
      ctrl.abort()
    }
  }, [layerId, recordId, token])

  if (error || !data) {
    // Through the SAME shell as the loaded state (§2.1) — the two headers used to be hand-copied
    // and could drift.
    return (
      <RecordDrawer
        ariaLabel={`${layerLabel} record detail`}
        title={error ? 'Record unavailable' : 'Loading…'}
        subtitle={
          <>
            {layerLabel}
            <span className="record-detail__id" title={recordId}>
              {' '}#{recordId}
            </span>
          </>
        }
        onClose={onClose}
      >
        <p className="panel-section__note">
          {error ? `Couldn’t load record (${error})` : 'Loading…'}
        </p>
      </RecordDrawer>
    )
  }

  return (
    <RecordDetail
      layerId={layerId}
      layerLabel={layerLabel}
      columns={data.columns}
      recordId={recordId}
      properties={data.properties}
      onClose={onClose}
      geometry={data.geometry}
      displayField={displayField}
      geomType={geomType}
      displayColor={displayColor}
      captureMap={captureMap}
    />
  )
}
