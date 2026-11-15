import type { CSSProperties, ReactNode } from 'react'

import type { GeometryType } from '@/lib/api/types'

/**
 * Catalog item-type icons (Talus 23 follow-up) — one cohesive line-icon family (Feather style:
 * 24×24 grid, stroke = currentColor, 2px round strokes) so the types read as a set rather than a
 * random mix. `currentColor` means the layer icon can be tinted with its symbology color while the
 * map/raster icons inherit the row's text color.
 *
 * - LayerIcon  — stacked layers (vector feature layer)
 * - MapIcon    — folded map (web map)
 * - RasterIcon — framed image (raster / imagery; for the eventual raster item type)
 *
 * Geometry glyphs (Talus UI icons) — the by-geometry layer icon shown in the Catalog and the map TOC,
 * a design-compliant take on the classic ArcMap point/line/polygon clipart redrawn into THIS family
 * (24×24, currentColor) so a layer reads as point vs. line vs. polygon at a glance. Filled marks
 * (point dots, vertex nodes, the polygon's wash) use `fill="currentColor"` so they tint with the row
 * text color in the catalog and with the symbology color in the map TOC. Use via `GeometryIcon`.
 * - PointIcon   — a scatter of feature markers (point layer)
 * - LineIcon    — a vertexed polyline (line layer)
 * - PolygonIcon — a closed, lightly-filled ring (polygon layer)
 *
 * Share-status glyphs (Talus 36) — same family, used for the catalog's at-a-glance share icon
 * (replacing the old text pill). Precedence public(globe) > org(building) > groups(people) >
 * private(person):
 * - PersonIcon — owner / private
 * - GroupIcon  — shared to groups
 * - OrgIcon    — shared org-wide
 * - GlobeIcon  — public
 */

type IconProps = { className?: string; style?: CSSProperties }

function Icon({ className, style, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={style}
      aria-hidden
    >
      {children}
    </svg>
  )
}

export function LayerIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <polygon points="12 2 2 7 12 12 22 7 12 2" />
      <polyline points="2 17 12 22 22 17" />
      <polyline points="2 12 12 17 22 12" />
    </Icon>
  )
}

export function MapIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6" />
      <line x1="8" y1="2" x2="8" y2="18" />
      <line x1="16" y1="6" x2="16" y2="22" />
    </Icon>
  )
}

export function RasterIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
      <circle cx="8.5" cy="8.5" r="1.5" />
      <polyline points="21 15 16 10 5 21" />
    </Icon>
  )
}

/* Geometry glyphs ----------------------------------------------------------------
 * Filled marks set fill="currentColor" stroke="none" so they read solid at small sizes (the wrapper
 * defaults to fill="none"); strokes inherit the 2px round currentColor outline from <Icon>. */

/** Point layer — a scatter of feature markers (the attached "three dots" motif, design-compliant). */
export function PointIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="7.5" cy="9" r="2.3" fill="currentColor" stroke="none" />
      <circle cx="16.5" cy="10" r="2.3" fill="currentColor" stroke="none" />
      <circle cx="11" cy="16.5" r="2.3" fill="currentColor" stroke="none" />
    </Icon>
  )
}

/** Line layer — a vertexed polyline (open path with node markers at its vertices). */
export function LineIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <polyline points="4 16 12 9 20 15" />
      <circle cx="4" cy="16" r="1.9" fill="currentColor" stroke="none" />
      <circle cx="12" cy="9" r="1.9" fill="currentColor" stroke="none" />
      <circle cx="20" cy="15" r="1.9" fill="currentColor" stroke="none" />
    </Icon>
  )
}

/** Polygon layer — a closed ring with a light currentColor wash so the area (and its tint) reads. */
export function PolygonIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <polygon points="12 3 20 9 17 19 7 19 4 9" fill="currentColor" fillOpacity={0.18} />
      <circle cx="12" cy="3" r="1.8" fill="currentColor" stroke="none" />
      <circle cx="20" cy="9" r="1.8" fill="currentColor" stroke="none" />
      <circle cx="17" cy="19" r="1.8" fill="currentColor" stroke="none" />
      <circle cx="7" cy="19" r="1.8" fill="currentColor" stroke="none" />
      <circle cx="4" cy="9" r="1.8" fill="currentColor" stroke="none" />
    </Icon>
  )
}

/** Standalone non-spatial table (decision-nonspatial-tables.md, geom_type 'none') — a row grid. */
export function TableIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="4" y="5" width="16" height="14" rx="1.5" />
      <line x1="4" y1="10" x2="20" y2="10" />
      <line x1="4" y1="14.5" x2="20" y2="14.5" />
      <line x1="10" y1="10" x2="10" y2="19" />
    </Icon>
  )
}

const GEOMETRY_GLYPH: Record<GeometryType | 'none', (props: IconProps) => ReactNode> = {
  point: PointIcon,
  line: LineIcon,
  polygon: PolygonIcon,
  none: TableIcon, // a standalone non-spatial table
}

/** The by-geometry layer glyph. Defaults to the point glyph when geomType is absent (matching the
 *  API default for pre-V055 layers), so every vector layer always gets a sensible mark. */
export function GeometryIcon({ geomType, ...props }: IconProps & { geomType?: GeometryType | 'none' | null }) {
  const Glyph = GEOMETRY_GLYPH[geomType ?? 'point'] ?? PointIcon
  return <Glyph {...props} />
}

/* Share-status glyphs (Talus 36). Feather "user" — one person = owner/private. */
export function PersonIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </Icon>
  )
}

/* Feather "users" — two people = shared to groups. */
export function GroupIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </Icon>
  )
}

/* Building — shared org-wide. */
export function OrgIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 21h18" />
      <path d="M5 21V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16" />
      <path d="M9 7h.01" />
      <path d="M15 7h.01" />
      <path d="M9 11h.01" />
      <path d="M15 11h.01" />
      <path d="M10 21v-4h4v4" />
    </Icon>
  )
}

/* Feather "globe" — public. */
export function GlobeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </Icon>
  )
}

/* Feather "link" — the chain glyph behind the reference badge (decision-external-catalog-items.md). */
export function LinkIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </Icon>
  )
}

/** The REFERENCE badge (decision-external-catalog-items.md §2.4): the item's base glyph — geometry
 * for vector kinds, framed image for tile/image kinds — with a small link overlay in the corner, so
 * "referenced external service" reads at a glance wherever the item appears (Catalog rows, the
 * Browse-picker group, the TOC). The overlay's backing disc uses the surface token so it stays
 * legible over any row background. */
export function ReferenceIcon({
  externalKind,
  geomType,
  ...props
}: IconProps & { externalKind: string; geomType?: GeometryType | null }) {
  const vector = externalKind === 'arcgis-feature' || externalKind === 'geojson'
  return (
    <span className="reficon" style={props.style}>
      {vector ? (
        <GeometryIcon geomType={geomType} className={props.className} />
      ) : (
        <RasterIcon className={props.className} />
      )}
      <span className="reficon__link" aria-hidden>
        <LinkIcon />
      </span>
    </span>
  )
}

/** The FIELD PROJECT glyph (decision-field-projects.md F7) — a phone with a location pin on its
 * screen: the item is a phone-bound collection artifact, not a browser map (the affordance split
 * from web maps). Same family: 24×24, currentColor, 2px round strokes. */
export function FieldProjectIcon(props: IconProps) {
  return (
    <Icon {...props}>
      {/* the handset */}
      <rect x="6" y="2.5" width="12" height="19" rx="2.2" />
      {/* the location pin on screen: teardrop arc + dot */}
      <path d="M12 15.5c2.2-2.3 3.3-4 3.3-5.5a3.3 3.3 0 1 0-6.6 0c0 1.5 1.1 3.2 3.3 5.5Z" />
      <circle cx="12" cy="9.8" r="0.6" fill="currentColor" />
    </Icon>
  )
}

/** The "Use in…" connect glyph (Interop I-1) — the plug-in-a-cord-loop motif redrawn into this
 * family: an open cord arc circling a 45°-angled plug whose prongs point through the gap. Replaces
 * the 🔌 emoji on the catalog action cluster (design-compliant per the reference art). */
export function PlugIcon(props: IconProps) {
  return (
    <Icon {...props}>
      {/* the cord: most of a circle, gap facing NE where the plug points out */}
      <path d="M20.9 13.6 A9 9 0 1 1 10.4 3.1" />
      {/* the plug, drawn upright then rotated 45° so the prongs aim into the gap */}
      <g transform="rotate(45 12 12)">
        <line x1="10" y1="4.5" x2="10" y2="8" />
        <line x1="14" y1="4.5" x2="14" y2="8" />
        <path d="M8 8h8v3a4 4 0 0 1-4 4 4 4 0 0 1-4-4z" />
        <line x1="12" y1="15" x2="12" y2="17.5" />
      </g>
    </Icon>
  )
}

/* Feather "info" — circled i; the map popup's per-feature Details affordance (sits by the title). */
export function InfoIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </Icon>
  )
}
