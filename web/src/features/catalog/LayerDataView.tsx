import { useState } from 'react'
import { ChevronLeft } from 'lucide-react'

import type { MyLayer } from '@/lib/api'

import FieldsView from './FieldsView'
import TableView from './TableView'

type Sub = 'table' | 'fields'

/**
 * A layer's Data interface — the ArcGIS-Enterprise-style item Data page (IA-A): a Table ⇄
 * Fields toggle over one layer, reached from the catalog. The full item page
 * (Overview / Visualization / Settings) is future; only Data ships this slice.
 */
export default function LayerDataView({ layer, onBack }: { layer: MyLayer; onBack: () => void }) {
  const [sub, setSub] = useState<Sub>('table')
  return (
    <div className="dview">
      <div className="dview__header">
        <button className="btn btn--sm" onClick={onBack}>
          <ChevronLeft size={13} aria-hidden="true" /> Catalog
        </button>
        <h2 className="dview__title">{layer.displayName ?? layer.id}</h2>
        <div className="view-toggle" role="group" aria-label="Data view">
          <button
            className={`view-toggle__btn${sub === 'table' ? ' is-active' : ''}`}
            onClick={() => setSub('table')}
          >
            Table
          </button>
          <button
            className={`view-toggle__btn${sub === 'fields' ? ' is-active' : ''}`}
            onClick={() => setSub('fields')}
          >
            Fields
          </button>
        </div>
      </div>
      {sub === 'table' ? <TableView layer={layer} /> : <FieldsView layer={layer} />}
    </div>
  )
}
