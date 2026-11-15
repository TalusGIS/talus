import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

/**
 * Right-click layer options menu (decision-map-layer-table.md) — the app's first right-click-a-layer
 * affordance (map tools LEFT, data tools RIGHT; the canvas already right-clicks for "What's here?").
 * A positioned popover opened from a TOC row's onContextMenu OR its ⋯ kebab; items come from the
 * caller so later options (zoom-to, field calc, the Tables-section rows) slot in without touching
 * this component. Closes on Escape, click-away, or selecting an item.
 */

export interface LayerMenuItem {
  label: string
  /** Leading glyph (16px-ish, currentColor) — sized by .layer-menu__icon. */
  icon?: ReactNode
  /** A greyed placeholder with the app's "Soon" pill (the Add-layers coming-soon treatment). */
  soon?: boolean
  /** Greyed + inert, no pill — a real item that just can't apply right now (e.g. Zoom to selected
   *  with an empty selection — decision-zoom-to-selected.md: disabled, no hint copy). */
  disabled?: boolean
  onSelect?: () => void
}

export default function LayerContextMenu({
  x,
  y,
  items,
  onClose,
  ariaLabel = 'Layer options',
}: {
  /** Viewport coordinates: the pointer position (right-click) or the kebab's bottom-left corner. */
  x: number
  y: number
  items: LayerMenuItem[]
  onClose: () => void
  /** Menu name for assistive tech — override when reusing outside the layer TOC (e.g. the navbar
   *  identity chip). */
  ariaLabel?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: x, top: y })

  // Clamp into the viewport so a right-click near the bottom/right edge doesn't clip the menu.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setPos({
      left: Math.max(8, Math.min(x, window.innerWidth - r.width - 8)),
      top: Math.max(8, Math.min(y, window.innerHeight - r.height - 8)),
    })
  }, [x, y])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onDown)
    }
  }, [onClose])

  return (
    <div ref={ref} className="layer-menu" style={pos} role="menu" aria-label={ariaLabel}>
      {items.map((it) => (
        <button
          key={it.label}
          type="button"
          className={`layer-menu__item${it.soon ? ' is-soon' : ''}`}
          role="menuitem"
          disabled={it.soon || it.disabled}
          title={it.soon ? 'Coming soon' : undefined}
          onClick={it.onSelect}
        >
          {it.icon && <span className="layer-menu__icon">{it.icon}</span>}
          <span className="layer-menu__label">{it.label}</span>
          {it.soon && <span className="layer-menu__soon">Soon</span>}
        </button>
      ))}
    </div>
  )
}
