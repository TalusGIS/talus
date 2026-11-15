import { useEffect, useMemo, useState } from 'react'

import {
  allPointSymbols,
  pointSymbolCategories,
  pointSymbolsLoaded,
  preloadPointSymbols,
} from '@/features/map/pointIcons'
import type { PointSymbol } from '@/features/map/point-symbols/manifest'

/**
 * The bundled marker picker (decision-point-icons.md). Categorised, searchable, and tinted live to
 * the symbology colour so what you pick is what the map draws.
 *
 * The symbol set is a lazily-loaded chunk, so this mounts in a loading state and fills in — the
 * Style panel is the first place most sessions touch it.
 */
export interface IconPickerProps {
  /** Currently selected icon name, or null for the plain circle. */
  value: string | null
  onChange: (name: string | null) => void
  /** Symbology colour, so the grid previews in the colour the map will paint. */
  color: string
}

function Swatch({ sym, color }: { sym: PointSymbol; color: string }): React.JSX.Element {
  return (
    <svg className="icon-picker__glyph" viewBox={sym.viewBox} aria-hidden="true" fill={color}>
      {sym.paths.map((d, i) => (
        <path key={i} d={d} />
      ))}
    </svg>
  )
}

export default function IconPicker({ value, onChange, color }: IconPickerProps): React.JSX.Element {
  const [ready, setReady] = useState(pointSymbolsLoaded())
  const [q, setQ] = useState('')
  const [cat, setCat] = useState('')

  useEffect(() => {
    if (ready) return
    let cancelled = false
    void preloadPointSymbols().then(() => {
      if (!cancelled) setReady(pointSymbolsLoaded())
    })
    return () => {
      cancelled = true
    }
  }, [ready])

  const cats = ready ? pointSymbolCategories() : []
  const shown = useMemo(() => {
    if (!ready) return []
    const needle = q.trim().toLowerCase()
    return allPointSymbols().filter(
      (s) => (!cat || s.cat === cat) && (!needle || s.name.includes(needle)),
    )
  }, [ready, q, cat])

  if (!ready) return <p className="styles-panel__muted">Loading symbols…</p>

  return (
    <div className="icon-picker">
      <div className="icon-picker__controls">
        <select value={cat} onChange={(e) => setCat(e.target.value)} aria-label="Symbol category">
          <option value="">All categories</option>
          {cats.map((c) => (
            <option key={c.key} value={c.key}>
              {c.label}
            </option>
          ))}
        </select>
        <input
          type="search"
          value={q}
          placeholder="Search…"
          aria-label="Search symbols by name"
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div className="icon-picker__grid" role="listbox" aria-label="Point symbol">
        <button
          type="button"
          role="option"
          aria-selected={value === null}
          className={`icon-picker__cell${value === null ? ' is-selected' : ''}`}
          title="No icon — draw a plain circle"
          onClick={() => onChange(null)}
        >
          <span className="icon-picker__dot" style={{ background: color }} />
        </button>
        {shown.map((s) => (
          <button
            key={s.name}
            type="button"
            role="option"
            aria-selected={value === s.name}
            className={`icon-picker__cell${value === s.name ? ' is-selected' : ''}`}
            title={s.name}
            onClick={() => onChange(s.name)}
          >
            <Swatch sym={s} color={color} />
          </button>
        ))}
      </div>
      {shown.length === 0 && <p className="styles-panel__muted">No symbols match.</p>}
    </div>
  )
}
