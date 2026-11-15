/**
 * The '@paid' alias's FALLBACK (decision-story-maps.md §4, the ruled mounting seam). When
 * paid/web/ is absent — every public-repo build — vite.config.ts and tsconfig.app.json resolve
 * '@paid' here instead, and the paid surfaces simply do not exist: no routes, no nav, no chunk.
 * That is F6 ("nav absent entirely") delivered structurally rather than by a flag.
 *
 * Contract: everything paid/web/src/index.tsx exports, exported here as inert equivalents, and
 * nothing else — core may only ever import '@paid', never a deeper path.
 */
import type { ReactNode } from 'react'

/** Route elements to splice into PortalShell's <Routes> (a function, not a component — <Routes>
 *  only accepts <Route>/fragment children, so the shell calls this inline). */
export function paidRoutes(): ReactNode {
  return null
}

/** Route elements nested under /admin (Talus+ admin pages). Inert here. */
export function paidAdminRoutes(): ReactNode {
  return null
}

/** The Talus+ group on the Administration rail. Inert here: no paid admin pages. */
export function PaidAdminRailItems(): ReactNode {
  return null
}

/** Nav entries for the top bar (a component — NavBar renders it like any child). Inert here:
 *  a public build has no paid destinations to link to. */
export function PaidNavLinks(): ReactNode {
  return null
}

/** A create action for the top bar's `New ▾` menu (design-handoff-nav-new-menu.md §4). */
export interface PaidCreateEntry {
  label: string
  to: string
  icon: ReactNode
}

/** The entitled Talus+ create entries for the `New ▾` menu. Inert here: no paid modules, so the
 *  menu never opens — NavBar renders its plain `New map` link, exactly as before the seam grew this. */
const NO_ENTRIES: PaidCreateEntry[] = []
export function usePaidCreateEntries(): PaidCreateEntry[] {
  return NO_ENTRIES
}

/** A Catalog facet for a Talus+ module's saved items. */
export interface PaidCatalogFacet {
  key: string
  label: string
  element: ReactNode
}

/** The entitled Catalog facets. Inert here: no paid modules, so the Catalog shows only its core
 *  item types, exactly as before the seam grew this. */
const NO_FACETS: PaidCatalogFacet[] = []
export function usePaidCatalogFacets(): PaidCatalogFacet[] {
  return NO_FACETS
}

/** The Talus+ Helper entry in the top bar (AI). Inert here: no AI module in a public build. */
export function PaidHelpAction(): ReactNode {
  return null
}

/** The record drawer's "Generate sheet" action (Talus+ reports). Inert here: no reports module,
 *  no sheets — the drawer renders exactly as it did before the seam grew this export. */
// The props are load-bearing as a TYPE — callers pass them and the real paid implementation
// consumes them — but a stub that renders nothing cannot use them. Waived per-line rather than
// adding `argsIgnorePattern: '^_'` to eslint.config.js: this is the only such case in the tree,
// and a per-line waiver cannot quietly cover a future genuinely-unused argument the way a config
// rule would. (The directive must sit immediately above the code, hence the prose first.)
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function RecordSheetAction(_props: {
  layerId: string
  objectId: number | null
  captureMap?: () => Promise<Blob | null>
}): ReactNode {
  return null
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function PopupSheetAction(_props: {
  layerId: string
  objectId: number | null
  captureMap?: () => Promise<Blob | null>
  token: string | undefined
  onOpenReport: (reportId: string) => void
}): ReactNode {
  return null
}

/** The map header's "Packs" action (Talus Field f3g). Inert here: no field module, no packs — the
 *  header renders exactly as it did before the seam grew this export. Same waiver as above. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function FieldPacksAction(_props: {
  mapId: string | null
  fieldReady: boolean
  canManage: boolean
  getViewBbox: () => [number, number, number, number] | null
}): ReactNode {
  return null
}

/** Admin → Users → "Devices" (Talus Field f5c). Inert here: no field module, no devices. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function FieldDevicesAction(_props: { userId: string; userLabel: string }): ReactNode {
  return null
}

/** Disable's field-packs sentence (Talus Field f5c). Nothing to say without the field module. */
export function fieldOffboardingNote(): string | null {
  return null
}

/** Admin ▸ Basemap's sentence about phones (decision-streamed-basemap.md §19). Nothing to say
 *  without the field module: in a Community build every map is a browser that can read the setting. */
export function fieldBasemapNote(): string | null {
  return null
}
