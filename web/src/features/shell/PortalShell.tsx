import { Navigate, Route, Routes } from 'react-router'
import { paidAdminRoutes, paidRoutes } from '@paid'

import AssetMap from '@/features/map/AssetMap'
import CatalogView from '@/features/catalog/CatalogView'
import DomainsView from '@/features/domains/DomainsView'
import GroupDetailView from '@/features/groups/GroupDetailView'
import GroupsView from '@/features/groups/GroupsView'
import OpenDataDetail from '@/features/opendata/OpenDataDetail'
import OpenDataRasterDetail from '@/features/opendata/OpenDataRasterDetail'
import OpenDataView from '@/features/opendata/OpenDataView'
import ProfileView from '@/features/profile/ProfileView'
import type { PublicConfig } from '@/lib/api'
import { SyncProvider } from '@/lib/offline/SyncProvider'

import AdminView from './AdminView'
import BackupsView from '@/features/admin/BackupsView'
import BasemapView from '@/features/admin/BasemapView'
import ConnectionsView from '@/features/admin/ConnectionsView'
import DatabaseSourcesView from '@/features/admin/DatabaseSourcesView'
import GeocodersView from '@/features/admin/GeocodersView'
import HealthView from '@/features/admin/HealthView'
import HomePageView from '@/features/admin/HomePageView'
import MaintenanceView from '@/features/admin/MaintenanceView'
import NoticesView from '@/features/admin/NoticesView'
import ServiceAccountsView from '@/features/admin/ServiceAccountsView'
import SharingView from '@/features/admin/SharingView'
import UsageView from '@/features/admin/UsageView'
import UsersView from '@/features/admin/UsersView'
import HomeView from './HomeView'
import InfoBanner from './InfoBanner'
import NavBar from './NavBar'
import RequireAdmin from './RequireAdmin'
import RequireAuth from './RequireAuth'
import RouteErrorBoundary from './RouteErrorBoundary'

/**
 * The portal shell — the coherence layer. A persistent top <NavBar> over a routed content area.
 *
 * Talus 30: the shell now renders for ANONYMOUS visitors too (when the deployment offers a public
 * face). `/` (Home) is public; viewing a saved map (`/map/:webMapId`) is public-capable (the API
 * serves only public maps to anon). Authoring (`/map`), the authenticated catalog, and `/admin`
 * are wrapped in RequireAuth/RequireAdmin — an anon visitor is routed to sign-in. The security
 * boundary is the API (fail-closed); these guards just avoid rendering surfaces that would 401.
 */
export default function PortalShell({ publicConfig }: { publicConfig: PublicConfig | null }) {
  return (
    <SyncProvider>
    <div className="app">
      <NavBar />
      <InfoBanner />
      <div className="app__content">
        {/* Any routed view that crashes at render shows a visible fallback instead of a blank page;
            the NavBar stays alive and navigating away resets the boundary (RouteErrorBoundary). */}
        <RouteErrorBoundary>
        <Routes>
          <Route path="/" element={<HomeView home={publicConfig?.home ?? null} />} />
          {/* Authoring a new map requires sign-in. */}
          <Route
            path="/map"
            element={
              <RequireAuth>
                <AssetMap />
              </RequireAuth>
            }
          />
          {/* Opening a saved web map: public-capable (the API serves only public maps to anon). */}
          <Route path="/map/:webMapId" element={<AssetMap />} />
          {/* Open Data (Talus 33): PUBLIC catalog + dataset pages — no RequireAuth (the API serves
              only curated public datasets to anon; F6-gated). Anon-capable like /map/:webMapId. */}
          <Route path="/open-data" element={<OpenDataView />} />
          {/* Static 'rasters' segment must be declared before the :layerId param route. */}
          <Route path="/open-data/rasters/:rasterId" element={<OpenDataRasterDetail />} />
          <Route path="/open-data/:layerId" element={<OpenDataDetail />} />
          <Route
            path="/catalog"
            element={
              <RequireAuth>
                <div className="table-view">
                  <CatalogView />
                </div>
              </RequireAuth>
            }
          />
          <Route
            path="/catalog/:layerId"
            element={
              <RequireAuth>
                <div className="table-view">
                  <CatalogView />
                </div>
              </RequireAuth>
            }
          />
          {/* Groups: browse my/org groups + per-group membership & shared content (manage-gated server-side). */}
          <Route
            path="/groups"
            element={
              <RequireAuth>
                <GroupsView />
              </RequireAuth>
            }
          />
          <Route
            path="/groups/:groupId"
            element={
              <RequireAuth>
                <GroupDetailView />
              </RequireAuth>
            }
          />
          {/* Domains (slice 2b): org-wide reusable picklists; browse for all, edit-gated server-side. */}
          <Route
            path="/domains"
            element={
              <RequireAuth>
                <DomainsView />
              </RequireAuth>
            }
          />
          {/* Profile (Interop I-1): identity + personal access tokens. Signed-in only. */}
          <Route
            path="/profile"
            element={
              <RequireAuth>
                <ProfileView />
              </RequireAuth>
            }
          />
          {/* Admin (handoff-admin-1a §1.1): nested, deep-linkable destinations under the ONE
              RequireAuth + RequireAdmin gate; AdminView is the layout — grouped rail + Outlet.
              An admin can now be SENT to the screen that fixes their problem. */}
          <Route
            path="/admin"
            element={
              <RequireAuth>
                <RequireAdmin>
                  <AdminView />
                </RequireAdmin>
              </RequireAuth>
            }
          >
            <Route index element={<Navigate to="health" replace />} />
            <Route path="health" element={<HealthView />} />
            <Route path="backups" element={<BackupsView />} />
            <Route path="maintenance" element={<MaintenanceView />} />
            <Route path="usage" element={<UsageView />} />
            <Route path="users" element={<UsersView />} />
            <Route path="service-accounts" element={<ServiceAccountsView />} />
            <Route path="notices" element={<NoticesView />} />
            <Route path="sharing" element={<SharingView />} />
            <Route path="home" element={<HomePageView />} />
            <Route path="connections" element={<ConnectionsView />} />
            {/* The retired Settings catch-all's URL (one release old) → its first successor. */}
            <Route path="settings" element={<Navigate to="/admin/notices" replace />} />
            <Route path="basemap" element={<BasemapView />} />
            <Route path="geocoders" element={<GeocodersView />} />
            <Route path="database-sources" element={<DatabaseSourcesView scope="admin" />} />
            {/* Talus+ admin pages (F8 f5) — the same '@paid' seam, nested under the admin gate. */}
            {paidAdminRoutes()}
          </Route>
          {/* Talus+ routes (decision-story-maps.md §4): a FUNCTION returning <Route> elements —
              <Routes> only accepts Route/fragment children. The '@paid' alias resolves to the
              real paid/web entry in the dev repo and to the inert stub in the public build, so
              a free install has no paid routes at all (F6, structural). */}
          {paidRoutes()}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </RouteErrorBoundary>
      </div>
    </div>
    </SyncProvider>
  )
}
