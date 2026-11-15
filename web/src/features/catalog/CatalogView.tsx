import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, Star } from 'lucide-react'
import { useNavigate, useParams, useSearchParams } from 'react-router'
import { useAuth } from 'react-oidc-context'

import {
  ApiError,
  addFavorite,
  deleteExternalRef,
  deleteFieldProject,
  deleteLayer,
  deleteLayerView,
  deleteRaster,
  deleteWebMap,
  fetchLayerUsage,
  fetchMyExternalRefs,
  fetchMyFieldProjects,
  fetchMyLayers,
  fetchMyRasters,
  fetchMyWebMaps,
  fetchRasterUsage,
  removeFavorite,
  USAGE_DISCLAIMER,
} from '@/lib/api'
import type { ExternalRefSummary, FieldProjectSummary, GeometryType, MyFieldProjects, MyLayer, MyRasters, MyWebMaps, RasterSummary, ShareLevel, UsageRef, WebMapSummary } from '@/lib/api'
import { useIsAdmin, useIsPublisher } from '@/lib/auth/roles'
import { EXT_KIND_LABEL } from '@/features/map/addFromUrl'
import CreateLayerWizard, { type WizardGeometry } from '@/features/map/CreateLayerWizard'
import CreateExternalRefDialog from '@/features/catalog/CreateExternalRefDialog'
import BatchGeocodeDialog from '@/features/catalog/BatchGeocodeDialog'
import ManageMapDialog from '@/features/catalog/ManageMapDialog'
import ManageExternalRefDialog from '@/features/catalog/ManageExternalRefDialog'
import ManageLayerDialog from '@/features/catalog/ManageLayerDialog'
import type { ManageTab } from '@/features/catalog/ManageTabs'
import ManageFieldProjectDialog from '@/features/catalog/ManageFieldProjectDialog'
import ManageRasterDialog from '@/features/catalog/ManageRasterDialog'
import FieldProjectImportDialog from '@/features/catalog/FieldProjectImportDialog'
import UploadFieldProjectDialog from '@/features/catalog/UploadFieldProjectDialog'
import ImportDataDialog from '@/features/catalog/ImportDataDialog'
import DatabaseSourcesView from '@/features/admin/DatabaseSourcesView'
import { usePaidCatalogFacets } from '@paid'
import UploadRasterDialog from '@/features/catalog/UploadRasterDialog'
import UseInWizard from '@/features/catalog/UseInWizard'
import RasterUseInWizard from '@/features/catalog/RasterUseInWizard'

import { FieldProjectIcon, GeometryIcon, GlobeIcon, GroupIcon, MapIcon, OrgIcon, PersonIcon, PlugIcon, ReferenceIcon } from './ItemTypeIcons'
import RasterThumb from './RasterThumb'
import LayerDataView from './LayerDataView'
import CloseButton from '@/components/CloseButton'
import useConfirm from '@/components/useConfirm'

/**
 * The catalog — Talus's item-browsing surface (the concept ArcGIS calls "Content"). Lists the
 * layers the caller can view (GET /me/layers), each opening its Data interface, with a
 * lightweight scope filter — All · Owned by me · Shared with me (Talus 18). This is AGO's
 * "My Content / My Organization" concept as ONE legible filter, not five tabs.
 *
 * Selection is URL-driven: `/catalog` is the list, `/catalog/:layerId` is that layer's Data
 * view — so a layer is deep-linkable, survives refresh, and rides browser back/forward. The
 * scope filter accepts an optional `?scope=owned|shared` to seed its initial value (read once
 * on mount), so a link can land the catalog pre-scoped — e.g. Home's "owned by you" → /catalog?scope=owned.
 */

/** The three scope buckets. "shared" = viewable but not owned (every returned layer is
 * viewable, so it's exactly the complement of owner). */
type Scope = 'all' | 'owned' | 'shared'

const SCOPES: { key: Scope; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'owned', label: 'Owned by me' },
  { key: 'shared', label: 'Shared with me' },
]

function inScope(layer: MyLayer, scope: Scope): boolean {
  if (scope === 'owned') return layer.owner
  // "Shared with me" = a genuine share. For a regular caller this == !owner; for an admin, layers visible
  // only via admin override are sharedWithMe:false → they appear in "All" only (admin-sees-all). Fall back
  // to !owner when the flag is absent (older payloads).
  if (scope === 'shared') return layer.sharedWithMe ?? !layer.owner
  return true
}

/** Share-status labels — the tooltip/aria text behind each share-status icon (Talus 36; was the
 * text-pill label pre-T36). 'owner' = private. Also feeds the share-filter empty-state copy. */
const SHARE_LABEL: Record<string, string> = {
  owner: 'Owner',
  organization: 'Organization',
  groups: 'Groups',
  public: 'Public',
}

/** Share level → glyph (Talus 36). Precedence is computed server-side (broadest tier wins), so this
 * is a straight 1:1 map: private→person, groups→people, org→building, public→globe. */
const SHARE_GLYPH: Record<ShareLevel, (p: { className?: string }) => ReactNode> = {
  owner: PersonIcon,
  groups: GroupIcon,
  organization: OrgIcon,
  public: GlobeIcon,
}

/** The at-a-glance share-status icon for a catalog row (Talus 36, replacing the text pill). The
 * glyph is decorative; the meaning rides on the labeled span (title tooltip + aria-label) so it's
 * legible on hover and to screen readers. Public is tinted amber (the notable state). */
function ShareStatusIcon({ level }: { level: ShareLevel }) {
  const Glyph = SHARE_GLYPH[level]
  const label = SHARE_LABEL[level]
  return (
    <span
      className={`catalog__share-icon catalog__share-icon--${level}`}
      role="img"
      aria-label={`Sharing: ${label}`}
      title={label}
    >
      <Glyph />
    </span>
  )
}

/** The layer type icon — a by-geometry glyph (point / line / polygon) so the row reads its shape at a
 * glance, tinted orange when the layer is empty (Talus 36 health tell — count === 0). A null/undefined
 * count (no backing table, or maps which have none) gets the default color. The empty state also
 * carries a tooltip/aria-label so "why orange" reads on hover and to AT. */
function LayerTypeIcon({
  featureCount,
  geomType,
}: {
  featureCount?: number | null
  // 'none' genuinely flows here: non-spatial tables list in the catalog and GeometryIcon renders
  // their table glyph (the nonspatial-tables widening this prop missed — tsc -b fix, 2026-07-03).
  geomType?: GeometryType | 'none' | null
}) {
  if (featureCount === 0) {
    return (
      <span
        className="catalog__health"
        role="img"
        aria-label="Empty layer — no features"
        title="Empty layer — no features"
      >
        <GeometryIcon geomType={geomType} className="catalog__type-icon catalog__type-icon--empty" />
      </span>
    )
  }
  return <GeometryIcon geomType={geomType} className="catalog__type-icon" />
}

/** The share-status filter (Talus 24): 'all' or one specific share level. Shared across both lists. */
type ShareFilter = 'all' | ShareLevel

/** Does an item's share level satisfy the active share-status filter? A null/absent level (an
 * OpenFGA hiccup that left shareLevel unset) matches ONLY 'all'. */
function matchesShare(level: ShareLevel | null | undefined, filter: ShareFilter): boolean {
  return filter === 'all' || level === filter
}

/** Apply the shared catalog filters (★ favorites, share-status, name search) and the Modified sort
 * to a list of catalog rows. Both MyLayer and WebMapSummary carry `favorite`, `shareLevel`, and
 * `updated_at`, so one helper serves both lists. `updated_at` is ISO-8601, which sorts correctly
 * lexically, so the raw strings compare directly (no Date parsing). The search is the same
 * case-insensitive name substring match as the map's Add Layers panel; each list passes its own
 * `name` accessor since the display-name field differs per item type. Returns a NEW array — never
 * mutates the input. */
function filterAndSort<T extends { favorite?: boolean; shareLevel?: ShareLevel | null; updated_at: string }>(
  items: T[],
  favoritesOnly: boolean,
  shareFilter: ShareFilter,
  sortDesc: boolean,
  query: string,
  name: (it: T) => string,
): T[] {
  return items
    .filter(
      (it) =>
        (!favoritesOnly || it.favorite) &&
        matchesShare(it.shareLevel, shareFilter) &&
        (!query || name(it).toLowerCase().includes(query)),
    )
    .sort((a, b) =>
      sortDesc ? b.updated_at.localeCompare(a.updated_at) : a.updated_at.localeCompare(b.updated_at),
    )
}

/** The favorites + share-status + Modified-sort controls, shared by both lists so the affordances
 * are identical regardless of item type (Talus 24 c3). Scope/Map-scope stay per-list (they're
 * computed differently per type); these three are uniform concepts and share their state. */
function SharedFilters({
  favoritesOnly,
  onFavoritesOnly,
  shareFilter,
  onShareFilter,
  sortDesc,
  onSortDesc,
}: {
  favoritesOnly: boolean
  onFavoritesOnly: (v: boolean) => void
  shareFilter: ShareFilter
  onShareFilter: (v: ShareFilter) => void
  sortDesc: boolean
  onSortDesc: (v: boolean) => void
}) {
  return (
    <>
      <button
        type="button"
        className={`catalog__favtoggle${favoritesOnly ? ' catalog__favtoggle--active' : ''}`}
        aria-pressed={favoritesOnly}
        onClick={() => onFavoritesOnly(!favoritesOnly)}
      >
        <Star className="catalog__favstar" size={13} fill="currentColor" aria-hidden="true" /> Favorites
      </button>

      <label className="catalog__sharefilter">
        <span className="catalog__sharefilter-label">Sharing</span>
        <select
          className="catalog__select"
          value={shareFilter}
          onChange={(e) => onShareFilter(e.target.value as ShareFilter)}
        >
          <option value="all">All</option>
          <option value="owner">Owner</option>
          <option value="organization">Organization</option>
          <option value="groups">Groups</option>
          <option value="public">Public</option>
        </select>
      </label>

      <button
        type="button"
        className="catalog__sort"
        aria-label={`Sort by modified date, ${sortDesc ? 'newest first' : 'oldest first'}`}
        title="Sort by modified date"
        onClick={() => onSortDesc(!sortDesc)}
      >
        Modified{' '}
        {sortDesc ? <ArrowDown size={13} aria-hidden="true" /> : <ArrowUp size={13} aria-hidden="true" />}
      </button>
    </>
  )
}

/** One catalog row — identical shape across Layers, Maps (and the future Raster tab): a clickable
 * body (type icon · name · share-status icon · owner · modified date) followed by the uniform
 * action cluster (★ favorite · ⚙ Manage · 🗑 Delete). The actions are ALWAYS rendered regardless of
 * the caller's permission (Talus 24, per parity) — the handlers decide whether to act or explain via
 * a notice, and the server re-enforces every write. One component so every item type reads the same. */
function CatalogRow({
  icon,
  name,
  tag,
  shareLevel,
  owner,
  meta,
  noun,
  favorite,
  deleteProtected,
  onOpen,
  onToggleFavorite,
  onUseIn,
  onManage,
  onDelete,
}: {
  icon: ReactNode
  name: string
  /** Optional small pill after the name — e.g. the Talus 38 "View" indicator for a view layer. */
  tag?: ReactNode
  shareLevel?: ShareLevel | null
  /** Owner display name for the at-a-glance Owner column (Talus 36); absent when no owner
   * resolves — the column is simply blank then. */
  owner?: string | null
  meta: string
  /** Singular noun for the action labels: 'layer' | 'map'. */
  noun: string
  favorite: boolean
  /** When true, the delete action wears a padlock so protection reads at a glance (Talus 24 c4). */
  deleteProtected: boolean
  onOpen: () => void
  onToggleFavorite: () => void
  /** Interop I-1: opens the "Use in…" connection assistant. Layer/table rows only — maps, rasters
   * and references have no OGC story yet, so those rows simply omit it. */
  onUseIn?: () => void
  onManage: () => void
  onDelete: () => void
}) {
  return (
    <li className="catalog__row">
      <button className="catalog__item" onClick={onOpen}>
        {icon}
        <span className="catalog__name">{name}</span>
        {tag}
        {shareLevel && <ShareStatusIcon level={shareLevel} />}
        <span className="catalog__meta">
          {owner && (
            <span className="catalog__owner" title={`Owned by: ${owner}`}>
              Owned by: {owner}
            </span>
          )}
          <span className="catalog__id">{meta}</span>
        </span>
      </button>
      <button
        type="button"
        className={`iact cact${favorite ? ' is-fav' : ''}`}
        aria-pressed={favorite}
        aria-label={favorite ? 'Unfavorite' : 'Favorite'}
        title={favorite ? 'Remove from favorites' : 'Add to favorites'}
        onClick={onToggleFavorite}
      >
        <svg viewBox="0 0 24 24" fill={favorite ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M12 2 15.09 8.26 22 9.27l-5 4.87 1.18 6.88L12 17.77 5.82 21l1.18-6.88-5-4.87 6.91-1.01L12 2Z" />
        </svg>
      </button>
      {onUseIn && (
        <button
          type="button"
          className="iact cact"
          aria-label={`Use this ${noun} in a desktop app or script`}
          title="Use in… (ArcGIS Pro, QGIS, scripts, download)"
          onClick={onUseIn}
        >
          <PlugIcon />
        </button>
      )}
      <button
        type="button"
        className="iact cact"
        aria-label={`Manage ${noun}`}
        title={`Manage ${noun}`}
        onClick={onManage}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
        </svg>
      </button>
      <button
        type="button"
        className="iact cact danger"
        aria-label={deleteProtected ? `Delete ${noun} (delete-protected)` : `Delete ${noun}`}
        title={
          deleteProtected
            ? `Delete protection is on — turn it off in Manage to delete this ${noun}`
            : `Delete ${noun}`
        }
        onClick={onDelete}
      >
        {deleteProtected ? (
          // Protected rows show a padlock instead of the trash (delete-protected reads at a glance).
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <rect x="3" y="11" width="18" height="11" rx="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
          </svg>
        )}
      </button>
    </li>
  )
}

/** Per-scope empty-state copy — so the message names WHY the list is empty under this filter. */
const EMPTY_COPY: Record<Scope, string> = {
  all: 'No layers available to you.',
  owned: 'No layers owned by you yet.',
  shared: 'No layers shared with you.',
}

/** The 'all' scope with nothing narrowing it is the first screen of every fresh install — the
 * catalog ships empty by ruling (no seed data; the user brings their own), so it names the next
 * step instead of dead-ending, the way the Maps facet already does. Gated on `canImport` because
 * "Import data" is publisher-only: naming a control a viewer cannot see would be a worse dead end
 * than the plain sentence. */
const EMPTY_ALL_IMPORT = 'No layers yet. Add one with Import data.'

/** Per-scope empty-state copy for the Maps facet (Talus 21). */
const MAP_EMPTY_COPY: Record<Scope, string> = {
  all: 'No saved maps yet. Compose one in New Map and Save it.',
  owned: 'No maps owned by you yet.',
  shared: 'No maps shared with you.',
}

/** Empty-state copy when a name search is active — takes precedence over every other filter,
 * since the query is the most specific (and most recently typed) narrowing. `plural` is the
 * item-type plural: 'layers' | 'tables' | 'maps' | 'rasters' | 'references'. */
function searchEmptyMessage(plural: string, query: string): string {
  return `No ${plural} match “${query}”.`
}

/** Empty-state copy, folding in the share-status + ★ Favorites filters so the reason is exact for
 * any scope+share+favorites combination. The more specific share filter takes precedence. */
function emptyMessage(scope: Scope, favoritesOnly: boolean, shareFilter: ShareFilter, canImport: boolean): string {
  if (shareFilter !== 'all') {
    return `No layers with “${SHARE_LABEL[shareFilter]}” sharing${favoritesOnly ? ' in your favorites' : ''}.`
  }
  if (favoritesOnly) {
    if (scope === 'owned') return 'No owned layers favorited yet.'
    if (scope === 'shared') return 'No shared layers favorited yet.'
    return 'No favorites yet — tap a layer’s star to add one.'
  }
  if (scope === 'all' && canImport) return EMPTY_ALL_IMPORT
  return EMPTY_COPY[scope]
}

/** Maps counterpart of emptyMessage — same precedence (share filter, then favorites, then scope). */
function mapEmptyMessage(scope: Scope, favoritesOnly: boolean, shareFilter: ShareFilter): string {
  if (shareFilter !== 'all') {
    return `No maps with “${SHARE_LABEL[shareFilter]}” sharing${favoritesOnly ? ' in your favorites' : ''}.`
  }
  if (favoritesOnly) {
    if (scope === 'owned') return 'No owned maps favorited yet.'
    if (scope === 'shared') return 'No shared maps favorited yet.'
    return 'No favorite maps yet — tap a map’s star to add one.'
  }
  return MAP_EMPTY_COPY[scope]
}

/** Per-scope empty-state copy for the Rasters facet (V045+). */
const RASTER_EMPTY_COPY: Record<Scope, string> = {
  all: 'No rasters yet. Upload a Cloud-Optimized GeoTIFF (COG).',
  owned: 'No rasters owned by you yet.',
  shared: 'No rasters shared with you.',
}

/** Rasters counterpart of emptyMessage — same precedence (share filter, then favorites, then scope). */
function rasterEmptyMessage(scope: Scope, favoritesOnly: boolean, shareFilter: ShareFilter): string {
  if (shareFilter !== 'all') {
    return `No rasters with “${SHARE_LABEL[shareFilter]}” sharing${favoritesOnly ? ' in your favorites' : ''}.`
  }
  if (favoritesOnly) {
    if (scope === 'owned') return 'No owned rasters favorited yet.'
    if (scope === 'shared') return 'No shared rasters favorited yet.'
    return 'No favorite rasters yet — tap a raster’s star to add one.'
  }
  return RASTER_EMPTY_COPY[scope]
}

/** Per-scope empty-state copy for the References facet (decision-external-catalog-items.md). */
const REF_EMPTY_COPY: Record<Scope, string> = {
  all: 'No references yet. Use “+ New reference” to point at a service another organization hosts.',
  owned: 'No references owned by you yet.',
  shared: 'No references shared with you.',
}

/** References counterpart of emptyMessage — same precedence (share filter, then favorites, then scope). */
function refEmptyMessage(scope: Scope, favoritesOnly: boolean, shareFilter: ShareFilter): string {
  if (shareFilter !== 'all') {
    return `No references with “${SHARE_LABEL[shareFilter]}” sharing${favoritesOnly ? ' in your favorites' : ''}.`
  }
  if (favoritesOnly) {
    if (scope === 'owned') return 'No owned references favorited yet.'
    if (scope === 'shared') return 'No shared references favorited yet.'
    return 'No favorite references yet — tap a reference’s star to add one.'
  }
  return REF_EMPTY_COPY[scope]
}

/** Per-scope empty-state copy for the Field projects facet (V085, decision-field-projects.md). */
const PROJECT_EMPTY_COPY: Record<Scope, string> = {
  all: 'No field projects yet. Upload a QField project (.qgz) to give crews one import URL.',
  owned: 'No field projects owned by you yet.',
  shared: 'No field projects shared with you.',
}

/** Field-projects counterpart of emptyMessage — same precedence (share filter, favorites, scope). */
function projectEmptyMessage(scope: Scope, favoritesOnly: boolean, shareFilter: ShareFilter): string {
  if (shareFilter !== 'all') {
    return `No field projects with “${SHARE_LABEL[shareFilter]}” sharing${favoritesOnly ? ' in your favorites' : ''}.`
  }
  if (favoritesOnly) {
    if (scope === 'owned') return 'No owned field projects favorited yet.'
    if (scope === 'shared') return 'No shared field projects favorited yet.'
    return 'No favorite field projects yet — tap a project’s star to add one.'
  }
  return PROJECT_EMPTY_COPY[scope]
}

export default function CatalogView() {
  const auth = useAuth()
  const navigate = useNavigate()
  const { layerId } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const [layers, setLayers] = useState<MyLayer[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // A transient, dismissible message for action-level outcomes (a permission denial, a blocked or
  // failed delete) — distinct from `error`, which is a fatal load failure that replaces the catalog.
  // This keeps the list on screen so the user can react (e.g. open Manage and clear protection).
  const [notice, setNotice] = useState<string | null>(null)
  // Seed the scope from `?scope=` once on mount (validated; anything else → 'all'). This is a
  // one-time initial value, not a synced source of truth — toggling the control doesn't rewrite
  // the URL — so a link like /catalog?scope=owned lands pre-scoped without further coupling.
  const [scope, setScope] = useState<Scope>(() => {
    const s = searchParams.get('scope')
    return s === 'owned' || s === 'shared' ? s : 'all'
  })
  const [favoritesOnly, setFavoritesOnly] = useState(false)
  // Name search, shared across all facets like favorites/share (uniform concept). Matching mirrors
  // the map's Add Layers panel: trimmed, case-insensitive substring on the item's display name.
  const [search, setSearch] = useState('')
  const q = search.trim().toLowerCase()
  // Talus 24 c3 — share-status filter + Modified sort. Shared across both lists (layers + maps):
  // they're uniform concepts, unlike the per-type owned/shared scope. Sort defaults newest-first.
  const [shareFilter, setShareFilter] = useState<ShareFilter>('all')
  const [sortDesc, setSortDesc] = useState(true)
  // Talus 20 — the catalog browses item types: Layers, Maps (saved web maps), Rasters (V045+), and
  // References (external catalog items, V060).
  // Databases (decision-sql-source-pull.md, placement ruled 2026-10-01): a publisher's own database
  // connections and pulls — its own facet beside References, never combined with it (a reference is a
  // live pointer the browser fetches; a database source is a server-side login whose pulls land in
  // Tables). Deep link: /catalog?tab=databases (the New ▾ menu).
  // The Talus+ facets: Dashboards · Stories · Reports beside Maps — a saved dashboard is found
  // where a saved map is, the way ArcGIS Online's Content page lists every item type. Entitled
  // modules only (the stub returns none); deep link `/catalog?tab=<key>` like Databases.
  const paidFacets = usePaidCatalogFacets()
  const [mode, setMode] = useState<string>(() => {
    const tab = new URLSearchParams(window.location.search).get('tab')
    return tab === 'databases' || tab === 'dashboards' || tab === 'stories' || tab === 'reports' ? tab : 'layers'
  })
  const paidFacet = paidFacets.find((f) => f.key === mode) ?? null
  // Talus 21 — maps come in two buckets (owned + shared-with-me); a Maps scope filters between
  // them, mirroring the layer scope. `mapScope` is independent of the layer `scope`.
  const [myMaps, setMyMaps] = useState<MyWebMaps | null>(null)
  const [mapScope, setMapScope] = useState<Scope>('all')
  // Rasters facet (V045+) — own buckets (owned + shared-with-me) + scope, mirroring Maps.
  const [myRasters, setMyRasters] = useState<MyRasters | null>(null)
  const [rasterScope, setRasterScope] = useState<Scope>('all')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  // References facet (decision-external-catalog-items.md) — a flat list with owner/shared flags
  // (like layers, unlike the maps/rasters buckets) + its own scope.
  const [myRefs, setMyRefs] = useState<ExternalRefSummary[] | null>(null)
  const [refScope, setRefScope] = useState<Scope>('all')
  const [createRefOpen, setCreateRefOpen] = useState(false)
  const [managingRef, setManagingRef] = useState<ExternalRefSummary | null>(null)
  // Field projects facet (V085, decision-field-projects.md) — buckets + scope, mirroring Rasters.
  const [myProjects, setMyProjects] = useState<MyFieldProjects | null>(null)
  const [projectScope, setProjectScope] = useState<Scope>('all')
  const [uploadProjectOpen, setUploadProjectOpen] = useState(false)
  // The project whose Import-URL dialog is open (the F7 OPEN affordance — never a viewer).
  const [importProject, setImportProject] = useState<FieldProjectSummary | null>(null)
  const [managingProject, setManagingProject] = useState<FieldProjectSummary | null>(null)
  // Talus 21/24 — the map whose Manage dialog is open (null = closed).
  const [managingMap, setManagingMap] = useState<WebMapSummary | null>(null)
  // Talus 24 — the layer whose Manage dialog is open (null = closed).
  const [managingLayer, setManagingLayer] = useState<MyLayer | null>(null)
  // Interop I-1 — the layer whose "Use in…" wizard is open (null = closed). Any viewer may open it
  // (consuming a shared layer in QGIS is exactly the viewer use case; the server scopes everything).
  const [useInLayer, setUseInLayer] = useState<MyLayer | null>(null)
  // Interop I-2 — the raster whose "Use in…" (WMTS) wizard is open (null = closed).
  const [useInRaster, setUseInRaster] = useState<RasterSummary | null>(null)
  // How to open the layer Manage dialog: the normal "Manage" button uses the default ({}, → Sharing tab);
  // the create flow opens it on the Fields tab and optionally auto-expands the schema import panel.
  const [manageIntent, setManageIntent] = useState<{ defaultTab?: ManageTab; openImport?: boolean }>({})
  // V045+ — the raster whose Manage dialog is open (null = closed).
  const [managingRaster, setManagingRaster] = useState<RasterSummary | null>(null)
  // Talus 20 — Create layer moved here from the map (content management lives in the Catalog).
  const isPublisher = useIsPublisher()
  const isAdmin = useIsAdmin()
  // `/catalog?import=1` opens Import data — where the map's Add from file sends a file too large for
  // it. The flag is consumed (dropped from the address) so a reload or Back does not reopen the
  // dialog. Publishers only, like the button; the door itself checks again on the server.
  useEffect(() => {
    if (searchParams.get('import') !== '1' || !isPublisher) return
    setMode('layers')
    setImportOpen(true)
    const next = new URLSearchParams(searchParams)
    next.delete('import')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams, isPublisher])
  // null = closed; a geometry string opens the wizard pre-set to it ('none' from the Tables tab → a table).
  const [createGeometry, setCreateGeometry] = useState<WizardGeometry | null>(null)
  const [batchOpen, setBatchOpen] = useState(false)
  // The import dialog's address-only KML handoff (decision-kml-address-geocode.md F5).
  const [geocodeFile, setGeocodeFile] = useState<File | null>(null)

  const reloadLayers = useCallback(() => {
    const token = auth.user?.access_token
    if (!token) return
    fetchMyLayers(token)
      .then((r) => setLayers(r.layers))
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
  }, [auth.user?.access_token])

  // Explicit maps refresh (after an ownership transfer, where a row may leave the Owned bucket — a
  // local patch can't express the bucket change, so we refetch). The lazy first-load effect below
  // still handles the initial population.
  const reloadMaps = useCallback(() => {
    const token = auth.user?.access_token
    if (!token) return
    fetchMyWebMaps(token)
      .then(setMyMaps)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
  }, [auth.user?.access_token])

  const reloadRasters = useCallback(() => {
    const token = auth.user?.access_token
    if (!token) return
    fetchMyRasters(token)
      .then(setMyRasters)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
  }, [auth.user?.access_token])

  const reloadRefs = useCallback(() => {
    const token = auth.user?.access_token
    if (!token) return
    fetchMyExternalRefs(token)
      .then(setMyRefs)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
  }, [auth.user?.access_token])

  const reloadProjects = useCallback(() => {
    const token = auth.user?.access_token
    if (!token) return
    fetchMyFieldProjects(token)
      .then(setMyProjects)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
  }, [auth.user?.access_token])

  useEffect(() => {
    reloadLayers()
  }, [reloadLayers])

  // Load rasters lazily the first time the Rasters facet is opened.
  useEffect(() => {
    const token = auth.user?.access_token
    if (!token || mode !== 'rasters' || myRasters !== null) return
    let active = true
    fetchMyRasters(token)
      .then((r) => active && setMyRasters(r))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [auth.user?.access_token, mode, myRasters])

  // Load references lazily the first time the References facet is opened.
  useEffect(() => {
    const token = auth.user?.access_token
    if (!token || mode !== 'refs' || myRefs !== null) return
    let active = true
    fetchMyExternalRefs(token)
      .then((r) => active && setMyRefs(r))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [auth.user?.access_token, mode, myRefs])

  // Load field projects lazily the first time the Field projects facet is opened.
  useEffect(() => {
    const token = auth.user?.access_token
    if (!token || mode !== 'projects' || myProjects !== null) return
    let active = true
    fetchMyFieldProjects(token)
      .then((p) => active && setMyProjects(p))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [auth.user?.access_token, mode, myProjects])

  // Load saved maps lazily the first time the Maps facet is opened.
  useEffect(() => {
    const token = auth.user?.access_token
    if (!token || mode !== 'maps' || myMaps !== null) return
    let active = true
    fetchMyWebMaps(token)
      .then((m) => active && setMyMaps(m))
      .catch((e) => active && setError(e instanceof ApiError ? e.message : String(e)))
    return () => {
      active = false
    }
  }, [auth.user?.access_token, mode, myMaps])

  // Manage/Delete are shown on EVERY row (parity). Permission is checked on interaction: a caller
  // who can't manage gets a notice instead of the dialog/delete; the server re-enforces regardless.
  // Name the owner when we know them (Talus 26), so the user knows whom to ask; fall back to
  // admin-only wording when no owner resolves (nobody to name).
  const { confirm, confirmDialog } = useConfirm()

  const NO_MANAGE = (noun: string, ownerName?: string | null) =>
    ownerName
      ? `You don’t have permission to manage this ${noun} — only its owner ${ownerName} or an administrator can.`
      : `You don’t have permission to manage this ${noun} — only an administrator can.`
  const NO_DELETE = (noun: string, ownerName?: string | null) =>
    ownerName
      ? `You don’t have permission to delete this ${noun} — only its owner ${ownerName} or an administrator can.`
      : `You don’t have permission to delete this ${noun} — only an administrator can.`

  function openManageMap(m: WebMapSummary, canManage: boolean): void {
    if (canManage) {
      setNotice(null)
      setManagingMap(m)
    } else setNotice(NO_MANAGE('map', m.ownerName))
  }

  function openManageLayer(l: MyLayer): void {
    if (l.canManage) {
      setNotice(null)
      setManageIntent({})
      setManagingLayer(l)
    } else setNotice(NO_MANAGE('layer', l.ownerName))
  }

  async function deleteMap(m: WebMapSummary, canManage: boolean): Promise<void> {
    if (!canManage) return setNotice(NO_DELETE('map', m.ownerName))
    const token = auth.user?.access_token
    if (!token) return
    confirm({
      title: `Delete “${m.title}”?`,
      consequence:
        'The map and the way it’s shared are removed. The layers it showed are untouched — but the map itself can’t be brought back.',
      confirmLabel: 'Delete map',
      onConfirm: async () => {
        setNotice(null)
        try {
          await deleteWebMap(m.id, token)
          setMyMaps((prev) =>
            prev ? { owned: prev.owned.filter((x) => x.id !== m.id), shared: prev.shared.filter((x) => x.id !== m.id) } : prev,
          )
        } catch (e) {
          // A delete rejection (e.g. 409 delete-protected) leaves the list intact and explains via the
          // notice, so the user can open Manage and turn protection off.
          setNotice(e instanceof ApiError ? e.message : String(e))
        }
      },
    })
  }

  // When delete protection is toggled inside a Manage dialog, patch the matching row's local state
  // so its padlock updates live (no reload). The dialog's PUT is the source of truth; this mirrors it.
  function onLayerProtectionChange(id: string, val: boolean): void {
    setLayers((prev) => prev?.map((l) => (l.id === id ? { ...l, deleteProtected: val } : l)) ?? prev)
  }
  function onMapProtectionChange(id: string, val: boolean): void {
    setMyMaps((prev) =>
      prev
        ? {
            owned: prev.owned.map((m) => (m.id === id ? { ...m, deleteProtected: val } : m)),
            shared: prev.shared.map((m) => (m.id === id ? { ...m, deleteProtected: val } : m)),
          }
        : prev,
    )
  }

  // When sharing is changed inside a Manage dialog, patch the matching row's shareLevel so its
  // badge updates live (no reload) — the same live-mirror seam as the delete-protection padlock.
  function onLayerSharesChange(id: string, level: ShareLevel): void {
    setLayers((prev) => prev?.map((l) => (l.id === id ? { ...l, shareLevel: level } : l)) ?? prev)
  }
  function onMapSharesChange(id: string, level: ShareLevel): void {
    // Sharing is only editable on maps you manage, which live in the owned bucket — so patch owned
    // only (a shared-with-me row's sharing can't be changed from here, T36 server fix notwithstanding).
    setMyMaps((prev) =>
      prev
        ? { ...prev, owned: prev.owned.map((m) => (m.id === id ? { ...m, shareLevel: level } : m)) }
        : prev,
    )
  }

  // Patch a raster row's share badge live after the Manage dialog saves (owned bucket only).
  function onRasterSharesChange(id: string, level: ShareLevel): void {
    setMyRasters((prev) =>
      prev
        ? { ...prev, owned: prev.owned.map((r) => (r.id === id ? { ...r, shareLevel: level } : r)) }
        : prev,
    )
  }

  // Mirror a raster's delete-protection flip to its row's padlock (both buckets — an admin manages
  // shared rasters too).
  function onRasterProtectionChange(id: string, val: boolean): void {
    setMyRasters((prev) =>
      prev
        ? {
            owned: prev.owned.map((r) => (r.id === id ? { ...r, deleteProtected: val } : r)),
            shared: prev.shared.map((r) => (r.id === id ? { ...r, deleteProtected: val } : r)),
          }
        : prev,
    )
  }

  // Where-used delete warning (decision-where-used.md §4.3): a soft warn-and-confirm when web maps
  // reference the item — never a block, and best-effort (a failed usage fetch degrades to the plain
  // confirm; the server-side hard blocks for view/geocoder deps still land after confirm as today).
  // The §3.1 disclaimer rides along because the map list is viewer-filtered.
  async function mapUsageWarning(
    label: string,
    load: () => Promise<{ webMaps: UsageRef[] }>,
  ): Promise<string[]> {
    try {
      const { webMaps } = await load()
      if (webMaps.length === 0) return []
      const titles = webMaps.slice(0, 5).map((m) => `“${m.title}”`).join(', ')
      const more = webMaps.length > 5 ? ` and ${webMaps.length - 5} more` : ''
      return [
        `“${label}” is used by ${webMaps.length} map${webMaps.length === 1 ? '' : 's'} you can view: ` +
          `${titles}${more}. Deleting it will break those maps.`,
        USAGE_DISCLAIMER,
      ]
    } catch {
      return [] // warning is informational — its absence must never stop a legitimate delete
    }
  }

  /** The where-used warning as the confirm dialog's body — above the consequence callout. */
  function usageBody(warn: string[]): ReactNode {
    if (warn.length === 0) return undefined
    return warn.map((line, i) => (
      <p className="modal__muted" key={i}>
        {line}
      </p>
    ))
  }

  async function deleteLayerRow(l: MyLayer): Promise<void> {
    if (!l.canManage) return setNotice(NO_DELETE('layer', l.ownerName))
    const token = auth.user?.access_token
    if (!token) return
    const label = l.displayName ?? l.id
    // Talus 38 — a view layer is removed via its own endpoint (DROP VIEW + deregister); it carries no
    // feature data of its own, so the confirm copy differs and DELETE /layers/{id} would 409 ("built-in").
    const isView = l.renderMode === 'features'
    const warn = await mapUsageWarning(label, () => fetchLayerUsage(l.id, token))
    confirm({
      title: isView ? `Delete the view “${label}”?` : `Delete “${label}”?`,
      body: usageBody(warn),
      consequence: isView
        ? 'The view and its sharing are removed. The source layer and every row in it are untouched.'
        : 'The layer is released and its data is wiped. This can’t be undone.',
      confirmLabel: isView ? 'Delete view' : 'Delete layer',
      onConfirm: async () => {
        setNotice(null)
        try {
          // For a built-in (non-slot) layer the server returns a clear 409; surfaced via the notice.
          if (isView) await deleteLayerView(l.id, token)
          else await deleteLayer(l.id, token)
          setLayers((prev) => prev?.filter((x) => x.id !== l.id) ?? prev)
        } catch (e) {
          setNotice(e instanceof ApiError ? e.message : String(e))
        }
      },
    })
  }

  // Layers vs Tables share one /me/layers fetch, partitioned by geomType: a standalone non-spatial table
  // (decision-nonspatial-tables.md) is a geometry-less layer (geomType==='none'). The Layers tab excludes
  // tables; the Tables tab shows only them. Same scope/favorites/share/sort filters for both.
  const visible = useMemo(
    () =>
      filterAndSort(
        (layers ?? []).filter((l) => inScope(l, scope) && l.geomType !== 'none'),
        favoritesOnly, shareFilter, sortDesc, q, (l) => l.displayName ?? l.id,
      ),
    [layers, scope, favoritesOnly, shareFilter, sortDesc, q],
  )
  const visibleTables = useMemo(
    () =>
      filterAndSort(
        (layers ?? []).filter((l) => inScope(l, scope) && l.geomType === 'none'),
        favoritesOnly, shareFilter, sortDesc, q, (l) => l.displayName ?? l.id,
      ),
    [layers, scope, favoritesOnly, shareFilter, sortDesc, q],
  )

  /** Star/unstar a layer. Optimistic: flip local state first, then PUT/DELETE; on failure
   * revert so the star always reflects the server. The star is a sibling button (not nested
   * in the navigate button), so a click here never opens the layer. */
  async function toggleFavorite(layer: MyLayer): Promise<void> {
    const token = auth.user?.access_token
    if (!token) return
    const next = !layer.favorite
    setLayers((prev) => prev?.map((l) => (l.id === layer.id ? { ...l, favorite: next } : l)) ?? prev)
    try {
      await (next ? addFavorite('layer', layer.id, token) : removeFavorite('layer', layer.id, token))
    } catch (e) {
      setLayers((prev) => prev?.map((l) => (l.id === layer.id ? { ...l, favorite: !next } : l)) ?? prev)
      console.error('favorite toggle failed', e)
    }
  }

  /* Map favorite toggle — same optimistic pattern, flipping the flag in both web-map buckets. */
  async function toggleMapFavorite(map: WebMapSummary): Promise<void> {
    const token = auth.user?.access_token
    if (!token) return
    const next = !map.favorite
    const setFav = (val: boolean) => (mm: MyWebMaps | null) =>
      mm
        ? {
            owned: mm.owned.map((m) => (m.id === map.id ? { ...m, favorite: val } : m)),
            shared: mm.shared.map((m) => (m.id === map.id ? { ...m, favorite: val } : m)),
          }
        : mm
    setMyMaps((prev) => setFav(next)(prev))
    try {
      await (next ? addFavorite('web_map', map.id, token) : removeFavorite('web_map', map.id, token))
    } catch (e) {
      setMyMaps((prev) => setFav(!next)(prev))
      console.error('map favorite toggle failed', e)
    }
  }

  /* Raster favorite toggle — same optimistic pattern across both raster buckets. */
  async function toggleRasterFavorite(raster: RasterSummary): Promise<void> {
    const token = auth.user?.access_token
    if (!token) return
    const next = !raster.favorite
    const setFav = (val: boolean) => (rr: MyRasters | null) =>
      rr
        ? {
            owned: rr.owned.map((r) => (r.id === raster.id ? { ...r, favorite: val } : r)),
            shared: rr.shared.map((r) => (r.id === raster.id ? { ...r, favorite: val } : r)),
          }
        : rr
    setMyRasters((prev) => setFav(next)(prev))
    try {
      await (next ? addFavorite('raster', raster.id, token) : removeFavorite('raster', raster.id, token))
    } catch (e) {
      setMyRasters((prev) => setFav(!next)(prev))
      console.error('raster favorite toggle failed', e)
    }
  }

  /* Reference favorite toggle — same optimistic pattern over the flat refs list. */
  async function toggleRefFavorite(ref: ExternalRefSummary): Promise<void> {
    const token = auth.user?.access_token
    if (!token) return
    const next = !ref.favorite
    setMyRefs((prev) => prev?.map((r) => (r.id === ref.id ? { ...r, favorite: next } : r)) ?? prev)
    try {
      await (next ? addFavorite('external', ref.id, token) : removeFavorite('external', ref.id, token))
    } catch (e) {
      setMyRefs((prev) => prev?.map((r) => (r.id === ref.id ? { ...r, favorite: !next } : r)) ?? prev)
      console.error('reference favorite toggle failed', e)
    }
  }

  // Live-mirror seams for the References Manage dialog (badge + row fields), mirroring layers.
  function onRefSharesChange(id: string, level: ShareLevel): void {
    setMyRefs((prev) => prev?.map((r) => (r.id === id ? { ...r, shareLevel: level } : r)) ?? prev)
  }
  function onRefUpdated(updated: ExternalRefSummary): void {
    setMyRefs((prev) => prev?.map((r) => (r.id === updated.id ? updated : r)) ?? prev)
  }

  async function deleteRefRow(ref: ExternalRefSummary): Promise<void> {
    if (!ref.canManage) return setNotice(NO_DELETE('reference', ref.ownerName))
    const token = auth.user?.access_token
    if (!token) return
    // Plain delete (fork §3.3): maps that reference this item keep rendering from their snapshot,
    // so the confirm copy says exactly that (better than AGO's break-on-delete).
    confirm({
      title: `Delete the reference “${ref.title}”?`,
      consequence:
        'Maps that already added it keep working — they remember the service URL. It just disappears from the catalog, and adding it again means re-entering the URL.',
      confirmLabel: 'Delete reference',
      onConfirm: async () => {
        setNotice(null)
        try {
          await deleteExternalRef(ref.id, token)
          setMyRefs((prev) => prev?.filter((x) => x.id !== ref.id) ?? prev)
        } catch (e) {
          setNotice(e instanceof ApiError ? e.message : String(e))
        }
      },
    })
  }

  async function deleteRasterRow(r: RasterSummary, canManage: boolean): Promise<void> {
    if (!canManage) return setNotice(NO_DELETE('raster', r.ownerName))
    const token = auth.user?.access_token
    if (!token) return
    const warn = await mapUsageWarning(r.title, () => fetchRasterUsage(r.id, token))
    confirm({
      title: `Delete “${r.title}”?`,
      body: usageBody(warn),
      consequence: 'The raster and its files are removed from the server. This can’t be undone.',
      confirmLabel: 'Delete raster',
      onConfirm: async () => {
        setNotice(null)
        try {
          await deleteRaster(r.id, token)
          setMyRasters((prev) =>
            prev
              ? { owned: prev.owned.filter((x) => x.id !== r.id), shared: prev.shared.filter((x) => x.id !== r.id) }
              : prev,
          )
        } catch (e) {
          setNotice(e instanceof ApiError ? e.message : String(e))
        }
      },
    })
  }

  /* Field-project favorite toggle — same optimistic pattern across both buckets. */
  async function toggleProjectFavorite(project: FieldProjectSummary): Promise<void> {
    const token = auth.user?.access_token
    if (!token) return
    const next = !project.favorite
    const setFav = (val: boolean) => (pp: MyFieldProjects | null) =>
      pp
        ? {
            owned: pp.owned.map((p) => (p.id === project.id ? { ...p, favorite: val } : p)),
            shared: pp.shared.map((p) => (p.id === project.id ? { ...p, favorite: val } : p)),
          }
        : pp
    setMyProjects((prev) => setFav(next)(prev))
    try {
      await (next
        ? addFavorite('field_project', project.id, token)
        : removeFavorite('field_project', project.id, token))
    } catch (e) {
      setMyProjects((prev) => setFav(!next)(prev))
      console.error('field project favorite toggle failed', e)
    }
  }

  // Live-mirror seams for the Field-project Manage dialog (badge + padlock), mirroring rasters.
  function onProjectSharesChange(id: string, level: ShareLevel): void {
    setMyProjects((prev) =>
      prev
        ? {
            owned: prev.owned.map((p) => (p.id === id ? { ...p, shareLevel: level } : p)),
            shared: prev.shared.map((p) => (p.id === id ? { ...p, shareLevel: level } : p)),
          }
        : prev,
    )
  }
  function onProjectProtectionChange(id: string, value: boolean): void {
    setMyProjects((prev) =>
      prev
        ? {
            owned: prev.owned.map((p) => (p.id === id ? { ...p, deleteProtected: value } : p)),
            shared: prev.shared.map((p) => (p.id === id ? { ...p, deleteProtected: value } : p)),
          }
        : prev,
    )
  }

  async function deleteProjectRow(p: FieldProjectSummary, canManage: boolean): Promise<void> {
    if (!canManage) return setNotice(NO_DELETE('field project', p.ownerName))
    const token = auth.user?.access_token
    if (!token) return
    confirm({
      title: `Delete “${p.title}”?`,
      consequence:
        'Crews can no longer import it — copies already on phones keep working, and their edits still sync. This can’t be undone.',
      confirmLabel: 'Delete field project',
      onConfirm: async () => {
        setNotice(null)
        try {
          await deleteFieldProject(p.id, token)
          setMyProjects((prev) =>
            prev
              ? { owned: prev.owned.filter((x) => x.id !== p.id), shared: prev.shared.filter((x) => x.id !== p.id) }
              : prev,
          )
        } catch (e) {
          setNotice(e instanceof ApiError ? e.message : String(e))
        }
      },
    })
  }

  // The Layers/Tables branch is shared; only the list + labels differ (tables = geometry-less layers).
  const tablesMode = mode === 'tables'
  const layerList = tablesMode ? visibleTables : visible

  if (error) return <p className="signs__error">{error}</p>
  if (!layers) return <p className="signs__empty">Loading catalog…</p>

  if (layerId) {
    const selected = layers.find((l) => l.id === layerId)
    if (!selected) return <p className="signs__empty">That layer isn’t available to you.</p>
    return <LayerDataView layer={selected} onBack={() => navigate('/catalog')} />
  }

  return (
    <div className="catalog">
      <div className="catalog__head">
        <p className="eyebrow">Content</p>
        <h1 className="catalog__title">Catalog</h1>
      </div>

      {notice && (
        <div className="catalog__notice" role="alert">
          <span>{notice}</span>
          <CloseButton onClick={() => setNotice(null)} label="Dismiss" className="catalog__notice-close" />
        </div>
      )}

      {/* Item-type facet: Layers | Tables (non-spatial) | Maps | [Dashboards | Stories | Reports — Talus+]
          | Rasters | References | Databases | Field projects. */}
      <div className="catalog__segmented catalog__types" role="group" aria-label="Item type">
        {(
          [
            { key: 'layers', label: 'Layers' },
            { key: 'tables', label: 'Tables' },
            { key: 'maps', label: 'Maps' },
            ...paidFacets.map((f) => ({ key: f.key, label: f.label })),
            { key: 'rasters', label: 'Rasters' },
            { key: 'refs', label: 'References' },
            ...(isPublisher ? [{ key: 'databases', label: 'Databases' }] : []),
            { key: 'projects', label: 'Field projects' },
          ] as { key: string; label: string }[]
        ).map((m) => (
          <button
            key={m.key}
            type="button"
            className={`catalog__seg${mode === m.key ? ' catalog__seg--active' : ''}`}
            aria-pressed={mode === m.key}
            onClick={() => setMode(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>

      {/* Name search — shared across all facets, sitting above each facet's filter row. Same
          matching as the map's Add Layers panel search. */}
      {mode !== 'databases' && paidFacet === null && (
        <input
          className="catalog__search"
          type="search"
          placeholder="Search by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search catalog items by name"
        />
      )}

      {paidFacet !== null ? (
        paidFacet.element
      ) : mode === 'dashboards' || mode === 'stories' || mode === 'reports' ? (
        /* A deep link to a Talus+ facet while its entitlement probe is still in flight (or unlicensed). */
        <div className="catalog__paid-facet" />
      ) : mode === 'databases' ? (
        <DatabaseSourcesView scope="mine" embedded />
      ) : mode === 'layers' || mode === 'tables' ? (
        <>
          <div className="catalog__filters">
            <div className="catalog__segmented" role="group" aria-label="Scope">
              {SCOPES.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  className={`catalog__seg${scope === s.key ? ' catalog__seg--active' : ''}`}
                  aria-pressed={scope === s.key}
                  onClick={() => setScope(s.key)}
                >
                  {s.label}
                </button>
              ))}
            </div>

            <SharedFilters
              favoritesOnly={favoritesOnly}
              onFavoritesOnly={setFavoritesOnly}
              shareFilter={shareFilter}
              onShareFilter={setShareFilter}
              sortDesc={sortDesc}
              onSortDesc={setSortDesc}
            />

            {/* Create a layer or (from the Tables tab) a non-spatial table — publisher/admin. */}
            {isPublisher && (
              <button
                type="button"
                className="btn btn--sm catalog__newlayer"
                onClick={() => setCreateGeometry(tablesMode ? 'none' : 'point')}
              >
                {tablesMode ? '+ New table' : '+ New layer'}
              </button>
            )}
            {isPublisher && !tablesMode && (
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => setBatchOpen(true)}
                title="Turn a file or a table of addresses into points through any locator"
              >
                Geocode addresses
              </button>
            )}
            {isPublisher && !tablesMode && (
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => setImportOpen(true)}
                title="County-scale zipped Shapefile / GeoPackage / GeoJSON / KML / CSV, or an ArcGIS feature layer URL — imported in the background"
              >
                Import data
              </button>
            )}
          </div>

          {layerList.length === 0 ? (
            <p className="signs__empty">
              {q
                ? searchEmptyMessage(tablesMode ? 'tables' : 'layers', search.trim())
                : tablesMode
                  ? 'No tables yet — use “+ New table” to create a standalone non-spatial table.'
                  : emptyMessage(scope, favoritesOnly, shareFilter, isPublisher)}
            </p>
          ) : (
            <ul className="catalog__list">
              {layerList.map((l) => {
                const isView = l.renderMode === 'features'
                // Lineage: "View of <source>" when the source is known (the caller can also view it).
                const lineage = isView && l.sourceName ? `View of ${l.sourceName} · ` : isView ? 'View · ' : ''
                return (
                  <CatalogRow
                    key={l.id}
                    icon={<LayerTypeIcon featureCount={l.featureCount} geomType={l.geomType} />}
                    name={l.displayName ?? l.id}
                    tag={isView ? <span className="catalog__viewtag">View</span> : undefined}
                    shareLevel={l.shareLevel}
                    owner={l.ownerName}
                    meta={`${lineage}Updated ${new Date(l.updated_at).toLocaleDateString()}`}
                    noun="layer"
                    favorite={l.favorite}
                    deleteProtected={l.deleteProtected}
                    onOpen={() => navigate(`/catalog/${l.id}`)}
                    onToggleFavorite={() => toggleFavorite(l)}
                    onUseIn={() => setUseInLayer(l)}
                    onManage={() => openManageLayer(l)}
                    onDelete={() => void deleteLayerRow(l)}
                  />
                )
              })}
            </ul>
          )}
        </>
      ) : mode === 'maps' ? (
        myMaps === null ? (
          <p className="signs__empty">Loading maps…</p>
        ) : (
        (() => {
          const ownedIds = new Set(myMaps.owned.map((m) => m.id))
          // shared excludes owned server-side, so owned ∪ shared is disjoint.
          const basePool =
            mapScope === 'owned'
              ? myMaps.owned
              : mapScope === 'shared'
                ? // genuine shares only; an admin's shared bucket also holds admin-override-only maps
                  // (sharedWithMe:false) which belong in "All", not "Shared with me".
                  myMaps.shared.filter((m) => m.sharedWithMe ?? true)
                : [...myMaps.owned, ...myMaps.shared]
          const pool = filterAndSort(basePool, favoritesOnly, shareFilter, sortDesc, q, (m) => m.title)
          return (
            <>
              <div className="catalog__filters">
                <div className="catalog__segmented" role="group" aria-label="Map scope">
                  {SCOPES.map((s) => (
                    <button
                      key={s.key}
                      type="button"
                      className={`catalog__seg${mapScope === s.key ? ' catalog__seg--active' : ''}`}
                      aria-pressed={mapScope === s.key}
                      onClick={() => setMapScope(s.key)}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>

                <SharedFilters
                  favoritesOnly={favoritesOnly}
                  onFavoritesOnly={setFavoritesOnly}
                  shareFilter={shareFilter}
                  onShareFilter={setShareFilter}
                  sortDesc={sortDesc}
                  onSortDesc={setSortDesc}
                />
              </div>

              {pool.length === 0 ? (
                <p className="signs__empty">
                  {q ? searchEmptyMessage('maps', search.trim()) : mapEmptyMessage(mapScope, favoritesOnly, shareFilter)}
                </p>
              ) : (
                <ul className="catalog__list">
                  {pool.map((m) => {
                    // Owned maps are always manageable by their owner; a shared map only by an admin
                    // (owner-or-admin). Used only to decide act-vs-notice on Manage/Delete — the
                    // buttons themselves are always shown (parity); the server re-enforces regardless.
                    const canManage = ownedIds.has(m.id) || isAdmin
                    return (
                      <CatalogRow
                        key={m.id}
                        icon={<MapIcon className="catalog__type-icon" />}
                        name={m.title}
                        shareLevel={m.shareLevel}
                        owner={m.ownerName}
                        meta={`Updated ${new Date(m.updated_at).toLocaleDateString()}`}
                        noun="map"
                        favorite={!!m.favorite}
                        deleteProtected={!!m.deleteProtected}
                        onOpen={() => navigate(`/map/${m.id}`)}
                        onToggleFavorite={() => toggleMapFavorite(m)}
                        onManage={() => openManageMap(m, canManage)}
                        onDelete={() => void deleteMap(m, canManage)}
                      />
                    )
                  })}
                </ul>
              )}
            </>
          )
        })()
        )
      ) : mode === 'refs' ? (
        myRefs === null ? (
          <p className="signs__empty">Loading references…</p>
        ) : (
          (() => {
            const basePool = myRefs.filter((r) =>
              refScope === 'owned' ? r.owner : refScope === 'shared' ? r.sharedWithMe : true,
            )
            const pool = filterAndSort(basePool, favoritesOnly, shareFilter, sortDesc, q, (r) => r.title)
            return (
              <>
                <div className="catalog__filters">
                  <div className="catalog__segmented" role="group" aria-label="Reference scope">
                    {SCOPES.map((s) => (
                      <button
                        key={s.key}
                        type="button"
                        className={`catalog__seg${refScope === s.key ? ' catalog__seg--active' : ''}`}
                        aria-pressed={refScope === s.key}
                        onClick={() => setRefScope(s.key)}
                      >
                        {s.label}
                      </button>
                    ))}
                  </div>

                  <SharedFilters
                    favoritesOnly={favoritesOnly}
                    onFavoritesOnly={setFavoritesOnly}
                    shareFilter={shareFilter}
                    onShareFilter={setShareFilter}
                    sortDesc={sortDesc}
                    onSortDesc={setSortDesc}
                  />

                  {isPublisher && (
                    <button
                      type="button"
                      className="btn btn--sm catalog__newlayer"
                      onClick={() => setCreateRefOpen(true)}
                    >
                      + New reference
                    </button>
                  )}
                </div>

                {pool.length === 0 ? (
                  <p className="signs__empty">
                    {q ? searchEmptyMessage('references', search.trim()) : refEmptyMessage(refScope, favoritesOnly, shareFilter)}
                  </p>
                ) : (
                  <ul className="catalog__list">
                    {pool.map((r) => (
                      <CatalogRow
                        key={r.id}
                        icon={
                          <span style={{ color: r.color ?? undefined }}>
                            <ReferenceIcon
                              externalKind={r.externalKind}
                              geomType={r.geometryType}
                              className="catalog__type-icon"
                            />
                          </span>
                        }
                        name={r.title}
                        shareLevel={r.shareLevel}
                        owner={r.ownerName}
                        meta={`${EXT_KIND_LABEL[r.externalKind]} · Updated ${new Date(r.updated_at).toLocaleDateString()}`}
                        noun="reference"
                        favorite={r.favorite}
                        deleteProtected={false}
                        onOpen={() => {
                          if (r.canManage) {
                            setNotice(null)
                            setManagingRef(r)
                          } else {
                            setNotice(
                              'A reference has no data view of its own — add it to a map from the New Map panel’s Browse catalog.',
                            )
                          }
                        }}
                        onToggleFavorite={() => void toggleRefFavorite(r)}
                        onManage={() => {
                          if (r.canManage) {
                            setNotice(null)
                            setManagingRef(r)
                          } else setNotice(NO_MANAGE('reference', r.ownerName))
                        }}
                        onDelete={() => void deleteRefRow(r)}
                      />
                    ))}
                  </ul>
                )}
              </>
            )
          })()
        )
      ) : mode === 'projects' ? (
        myProjects === null ? (
          <p className="signs__empty">Loading field projects…</p>
        ) : (
          (() => {
            const ownedIds = new Set(myProjects.owned.map((p) => p.id))
            const basePool =
              projectScope === 'owned'
                ? myProjects.owned
                : projectScope === 'shared'
                  ? myProjects.shared
                  : [...myProjects.owned, ...myProjects.shared]
            const pool = filterAndSort(basePool, favoritesOnly, shareFilter, sortDesc, q, (p) => p.title)
            return (
              <>
                <div className="catalog__filters">
                  <div className="catalog__segmented" role="group" aria-label="Field project scope">
                    {SCOPES.map((s) => (
                      <button
                        key={s.key}
                        type="button"
                        className={`catalog__seg${projectScope === s.key ? ' catalog__seg--active' : ''}`}
                        aria-pressed={projectScope === s.key}
                        onClick={() => setProjectScope(s.key)}
                      >
                        {s.label}
                      </button>
                    ))}
                  </div>

                  <SharedFilters
                    favoritesOnly={favoritesOnly}
                    onFavoritesOnly={setFavoritesOnly}
                    shareFilter={shareFilter}
                    onShareFilter={setShareFilter}
                    sortDesc={sortDesc}
                    onSortDesc={setSortDesc}
                  />

                  {isPublisher && (
                    <button
                      type="button"
                      className="btn btn--sm catalog__newlayer"
                      onClick={() => setUploadProjectOpen(true)}
                    >
                      + Upload field project
                    </button>
                  )}
                </div>

                {pool.length === 0 ? (
                  <p className="signs__empty">
                    {q ? searchEmptyMessage('field projects', search.trim()) : projectEmptyMessage(projectScope, favoritesOnly, shareFilter)}
                  </p>
                ) : (
                  <ul className="catalog__list">
                    {pool.map((p) => {
                      // Owned projects are manageable by their owner; a shared one only by an admin.
                      const canManage = ownedIds.has(p.id) || isAdmin
                      return (
                        <CatalogRow
                          key={p.id}
                          icon={<FieldProjectIcon className="catalog__type-icon" />}
                          name={p.title}
                          shareLevel={p.shareLevel}
                          owner={p.ownerName}
                          meta={`Updated ${new Date(p.updated_at).toLocaleDateString()} · ${(p.sizeBytes / (1024 * 1024)).toFixed(1)} MB${typeof p.layerCount === 'number' ? ` · ${p.layerCount} layer${p.layerCount === 1 ? '' : 's'}` : ''}${(p.warnings?.length ?? 0) > 0 ? ' · ⚠ check Manage' : ''}`}
                          noun="field project"
                          favorite={!!p.favorite}
                          deleteProtected={!!p.deleteProtected}
                          // The F7 affordance: opening a field project offers the Import URL +
                          // crew workflow — never a viewer (it's a phone artifact).
                          onOpen={() => setImportProject(p)}
                          onToggleFavorite={() => toggleProjectFavorite(p)}
                          onManage={() => {
                            if (canManage) {
                              setNotice(null)
                              setManagingProject(p)
                            } else setNotice(NO_MANAGE('field project', p.ownerName))
                          }}
                          onDelete={() => void deleteProjectRow(p, canManage)}
                        />
                      )
                    })}
                  </ul>
                )}
              </>
            )
          })()
        )
      ) : myRasters === null ? (
        <p className="signs__empty">Loading rasters…</p>
      ) : (
        (() => {
          const ownedIds = new Set(myRasters.owned.map((r) => r.id))
          const basePool =
            rasterScope === 'owned'
              ? myRasters.owned
              : rasterScope === 'shared'
                ? myRasters.shared
                : [...myRasters.owned, ...myRasters.shared]
          const pool = filterAndSort(basePool, favoritesOnly, shareFilter, sortDesc, q, (r) => r.title)
          return (
            <>
              <div className="catalog__filters">
                <div className="catalog__segmented" role="group" aria-label="Raster scope">
                  {SCOPES.map((s) => (
                    <button
                      key={s.key}
                      type="button"
                      className={`catalog__seg${rasterScope === s.key ? ' catalog__seg--active' : ''}`}
                      aria-pressed={rasterScope === s.key}
                      onClick={() => setRasterScope(s.key)}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>

                <SharedFilters
                  favoritesOnly={favoritesOnly}
                  onFavoritesOnly={setFavoritesOnly}
                  shareFilter={shareFilter}
                  onShareFilter={setShareFilter}
                  sortDesc={sortDesc}
                  onSortDesc={setSortDesc}
                />

                {isPublisher && (
                  <button
                    type="button"
                    className="btn btn--sm catalog__newlayer"
                    onClick={() => setUploadOpen(true)}
                  >
                    + Upload raster
                  </button>
                )}
              </div>

              {pool.length === 0 ? (
                <p className="signs__empty">
                  {q ? searchEmptyMessage('rasters', search.trim()) : rasterEmptyMessage(rasterScope, favoritesOnly, shareFilter)}
                </p>
              ) : (
                <ul className="catalog__list">
                  {pool.map((r) => {
                    // Owned rasters are manageable by their owner; a shared one only by an admin.
                    const canManage = ownedIds.has(r.id) || isAdmin
                    return (
                      <CatalogRow
                        key={r.id}
                        icon={
                          <RasterThumb
                            id={r.id}
                            hasThumbnail={r.hasThumbnail}
                            token={auth.user?.access_token}
                            className="catalog__type-icon"
                          />
                        }
                        name={r.title}
                        shareLevel={r.shareLevel}
                        owner={r.ownerName}
                        meta={`Updated ${new Date(r.updated_at).toLocaleDateString()}`}
                        noun="raster"
                        favorite={!!r.favorite}
                        deleteProtected={!!r.deleteProtected}
                        onOpen={() => navigate(`/map?raster=${r.id}`)}
                        onToggleFavorite={() => toggleRasterFavorite(r)}
                        onUseIn={() => setUseInRaster(r)}
                        onManage={() => {
                          if (canManage) {
                            setNotice(null)
                            setManagingRaster(r)
                          } else setNotice(NO_MANAGE('raster', r.ownerName))
                        }}
                        onDelete={() => void deleteRasterRow(r, canManage)}
                      />
                    )
                  })}
                </ul>
              )}
            </>
          )
        })()
      )}

      <CreateLayerWizard
        open={createGeometry !== null}
        initialGeometry={createGeometry ?? 'point'}
        onClose={() => setCreateGeometry(null)}
        onCreated={() => reloadLayers()}
        accessToken={auth.user?.access_token ?? ''}
      />

      {batchOpen && (
        <BatchGeocodeDialog
          token={auth.user?.access_token}
          onClose={() => { setBatchOpen(false); setGeocodeFile(null); reloadLayers() }}
          onOpenLayer={(id) => navigate(`/catalog/${id}`)}
          initialFile={geocodeFile}
        />
      )}

      <UploadRasterDialog
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => reloadRasters()}
        accessToken={auth.user?.access_token ?? ''}
      />

      <ImportDataDialog
        open={importOpen}
        onClose={() => { setImportOpen(false); reloadLayers() }}
        onGeocodeInstead={(f) => { setImportOpen(false); setGeocodeFile(f); setBatchOpen(true) }}
        onImported={() => reloadLayers()} // stay on the catalog list — the fresh layer sorts to the top
        appendTargets={visible
          .filter((l) => l.renderMode !== 'features' && l.geomType !== 'none')
          .map((l) => ({ id: l.id, name: l.displayName ?? l.id }))}
        accessToken={auth.user?.access_token ?? ''}
      />

      <UploadFieldProjectDialog
        open={uploadProjectOpen}
        onClose={() => setUploadProjectOpen(false)}
        onUploaded={() => reloadProjects()}
        accessToken={auth.user?.access_token ?? ''}
      />

      {importProject && (
        <FieldProjectImportDialog
          project={importProject}
          accessToken={auth.user?.access_token ?? ''}
          onClose={() => setImportProject(null)}
        />
      )}

      {managingProject && (
        <ManageFieldProjectDialog
          project={managingProject}
          accessToken={auth.user?.access_token ?? ''}
          onSharesChange={(level) => onProjectSharesChange(managingProject.id, level)}
          onProtectionChange={(val) => onProjectProtectionChange(managingProject.id, val)}
          onChanged={() => reloadProjects()}
          onTransferred={() => reloadProjects()}
          onClose={() => setManagingProject(null)}
        />
      )}

      {managingMap && (
        <ManageMapDialog
          map={managingMap}
          accessToken={auth.user?.access_token ?? ''}
          onProtectionChange={(val) => onMapProtectionChange(managingMap.id, val)}
          onSharesChange={(level) => onMapSharesChange(managingMap.id, level)}
          onTransferred={() => reloadMaps()}
          onFieldProjectGenerated={() => reloadProjects()}
          onClose={() => setManagingMap(null)}
        />
      )}

      {useInLayer && (
        <UseInWizard
          layer={useInLayer}
          accessToken={auth.user?.access_token ?? ''}
          onClose={() => setUseInLayer(null)}
        />
      )}

      {useInRaster && (
        <RasterUseInWizard
          raster={useInRaster}
          accessToken={auth.user?.access_token ?? ''}
          onClose={() => setUseInRaster(null)}
        />
      )}

      {managingLayer && (
        <ManageLayerDialog
          layer={managingLayer}
          accessToken={auth.user?.access_token ?? ''}
          defaultTab={manageIntent.defaultTab}
          openImport={manageIntent.openImport}
          onProtectionChange={(val) => onLayerProtectionChange(managingLayer.id, val)}
          onSharesChange={(level) => onLayerSharesChange(managingLayer.id, level)}
          onTransferred={() => reloadLayers()}
          onViewCreated={() => reloadLayers()}
          onClose={() => setManagingLayer(null)}
        />
      )}

      {createRefOpen && (
        <CreateExternalRefDialog
          accessToken={auth.user?.access_token ?? ''}
          onCreated={() => reloadRefs()}
          onClose={() => setCreateRefOpen(false)}
        />
      )}

      {managingRef && (
        <ManageExternalRefDialog
          refItem={managingRef}
          accessToken={auth.user?.access_token ?? ''}
          onUpdated={onRefUpdated}
          onSharesChange={(level) => onRefSharesChange(managingRef.id, level)}
          onTransferred={() => reloadRefs()}
          onClose={() => setManagingRef(null)}
        />
      )}

      {managingRaster && (
        <ManageRasterDialog
          raster={managingRaster}
          accessToken={auth.user?.access_token ?? ''}
          onSharesChange={(level) => onRasterSharesChange(managingRaster.id, level)}
          onProtectionChange={(val) => onRasterProtectionChange(managingRaster.id, val)}
          onTransferred={() => reloadRasters()}
          onClose={() => setManagingRaster(null)}
        />
      )}
      {confirmDialog}
    </div>
  )
}
