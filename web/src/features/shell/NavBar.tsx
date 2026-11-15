import { useRef, useState } from 'react'
import { ChevronDown, Map as MapIcon, Moon, Sun, Database } from 'lucide-react'
import { Link, NavLink, useNavigate } from 'react-router'
import { PaidHelpAction, PaidNavLinks, usePaidCreateEntries } from '@paid'
import { useAuth } from 'react-oidc-context'

import LayerContextMenu from '@/features/map/LayerContextMenu'
import { hasAdminRole, primaryRoleLabel, useIsPublisher } from '@/lib/auth/roles'
import { LICENSES_URL, SOURCE_URL } from '@/lib/release'
import { userDisplayName } from '@/lib/auth/user'
import { useTheme } from '@/lib/theme'

import SyncIndicator from './SyncIndicator'

type Tab = { to: string; label: string; end?: boolean }

/**
 * Tabs for a SIGNED-IN user — the PLACES. The verb (`New ▾` / `New map`) is rendered ahead of them
 * (design-handoff-nav-new-menu.md §3: verbs left, places middle, identity right). Admin is appended
 * after the Talus+ Field tab (below). `end` marks tabs whose path must match exactly for the active
 * state — without it, Home (`/`) reads as active on every route.
 */
const AUTHED_TABS: Tab[] = [
  { to: '/', label: 'Home', end: true },
  { to: '/catalog', label: 'Catalog' },
  { to: '/groups', label: 'Groups' },
  { to: '/domains', label: 'Domains' },
  { to: '/open-data', label: 'Open Data' },
]

// Anonymous visitors (Talus 30 public shell) get the public destinations; Catalog / Admin require
// sign-in, so they're omitted. The `New map` link IS offered to anon (amending
// decision-home-public.md's "New Map absent"): /map stays behind RequireAuth, so the link
// is the sign-in affordance for a visitor who wants to make one. The featured gallery + the Open
// Data catalog (Talus 33) remain the anon discovery surface.
const ANON_TABS: Tab[] = [
  { to: '/', label: 'Home', end: true },
  { to: '/open-data', label: 'Open Data' },
]

/** The create link when there is no menu to open — a public build, an unlicensed org, an anonymous
 *  visitor. Today's `New Map` tab, in sentence case. */
const NEW_MAP: Tab = { to: '/map', label: 'New map' }

const ADMIN_TAB: Tab = { to: '/admin', label: 'Admin' }

function tabClass({ isActive }: { isActive: boolean }) {
  return `navbar__tab${isActive ? ' is-active' : ''}`
}

/**
 * The portal's persistent top bar: wordmark + primary nav + identity. With the public shell it adapts
 * to anonymous visitors — a "Sign in" button instead of the signed-in identity, and the public-only tab
 * set.
 */
export default function NavBar() {
  const auth = useAuth()
  const isAuthed = auth.isAuthenticated
  const { theme, toggle } = useTheme()
  const navigate = useNavigate()
  // The identity chip's dropdown (Interop I-1: Profile · Sign out) — anchored to the chip's
  // bottom-left, reusing the app's positioned-menu component.
  const [userMenu, setUserMenu] = useState<{ x: number; y: number } | null>(null)
  // the Sign in button's redirect was refused (the sign-in service unreachable) — the pill explains
  const [signinFailed, setSigninFailed] = useState(false)
  // The `New ▾` menu (design-handoff-nav-new-menu.md): Map, plus whichever Talus+ create actions the
  // deployment is entitled to (the paid seam runs the module probes — the stub returns none). With
  // nothing but Map there is no menu: a dropdown of one is worse than the link it replaced.
  const paidCreate = usePaidCreateEntries()
  // Database pull (decision-sql-source-pull.md, F11 amended 2026-10-01): a core create action for
  // publishers — it opens the pull wizard under Catalog → Database sources.
  const isPublisher = useIsPublisher()
  const hasNewMenu = isAuthed && (paidCreate.length > 0 || isPublisher)
  const [newMenu, setNewMenu] = useState<{ x: number; y: number } | null>(null)
  const newTrigger = useRef<HTMLButtonElement>(null)
  // Escape / click-away close the menu; return focus to the trigger so the keyboard user isn't
  // dropped at <body> (§7). The positioned-menu primitive doesn't own focus; the trigger does.
  function closeNewMenu(refocus: boolean): void {
    setNewMenu(null)
    if (refocus) newTrigger.current?.focus()
  }

  // Security (Talus 35): the service worker runtime-caches authenticated GET reads (feature data,
  // /me capabilities, tiles) keyed by URL, not by user. On a SHARED device, a later user who goes
  // offline could otherwise see the previous user's cached data via the NetworkFirst fallback. So
  // clear the caches AND the persisted OIDC session before redirecting to Keycloak sign-out.
  async function logout(): Promise<void> {
    try {
      if ('caches' in window) {
        const keys = await caches.keys()
        await Promise.all(keys.map((k) => caches.delete(k)))
      }
    } catch {
      /* best-effort — never block sign-out on a cache-clear failure */
    }
    try {
      await auth.removeUser()
    } catch {
      /* ignore — sign-out proceeds regardless */
    }
    void auth.signoutRedirect()
  }

  const tabs = isAuthed ? AUTHED_TABS : ANON_TABS
  const role = primaryRoleLabel(auth.user)

  return (
    <header className="navbar">
      <Link to="/" className="navbar__brand">
        Talus GIS
      </Link>
      <nav className="navbar__tabs" aria-label="Primary">
        {/* The verb, first. A button (not a NavLink): a menu has no active state, and the map editor
            is full-screen with its own chrome — never synthesise one, it would read as "New is the
            current page". */}
        {hasNewMenu ? (
          <button
            ref={newTrigger}
            type="button"
            className={`navbar__new${newMenu !== null ? ' is-open' : ''}`}
            aria-haspopup="menu"
            aria-expanded={newMenu !== null}
            // Same reason as the identity chip: the menu's document-level pointerdown click-away would
            // otherwise close it before this click re-opens it.
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              const r = e.currentTarget.getBoundingClientRect()
              setNewMenu(newMenu === null ? { x: r.left, y: r.bottom + 4 } : null)
            }}
          >
            New
            <ChevronDown size={13} aria-hidden="true" />
          </button>
        ) : (
          <NavLink to={NEW_MAP.to} className={tabClass}>
            {NEW_MAP.label}
          </NavLink>
        )}
        {newMenu !== null && (
          <LayerContextMenu
            x={newMenu.x}
            y={newMenu.y}
            items={[
              { label: 'Map', to: '/map', icon: <MapIcon size={16} aria-hidden="true" /> },
              ...(isPublisher
                ? [{ label: 'Database pull', to: '/catalog?tab=databases', icon: <Database size={16} aria-hidden="true" /> }]
                : []),
              ...paidCreate,
            ].map((e) => ({
              label: e.label,
              icon: e.icon,
              onSelect: () => {
                closeNewMenu(false)
                navigate(e.to)
              },
            }))}
            onClose={() => closeNewMenu(true)}
            ariaLabel="New"
          />
        )}
        <span className="navbar__rule" aria-hidden="true" />
        {tabs.map((t) => (
          <NavLink key={t.to} to={t.to} end={t.end} className={tabClass}>
            {t.label}
          </NavLink>
        ))}
        {/* Talus+ nav entries via the '@paid' seam (decision-story-maps.md F6): the stub renders
            nothing (public builds), and the real component self-hides unless the deployment's
            license grants the feature — "nav absent entirely", enforced by the component's own
            entitlement probe against the API. Today that is Field alone; the create actions for the
            other modules live in the New menu above. */}
        <PaidNavLinks />
        {isAuthed && hasAdminRole(auth.user) && (
          <NavLink to={ADMIN_TAB.to} className={tabClass}>
            {ADMIN_TAB.label}
          </NavLink>
        )}
      </nav>
      <div className="navbar__user">
        {/* Talus+ Helper (AI): same seam posture as PaidNavLinks — the stub renders nothing and the
            real component self-hides unless GET /ai/status says the Helper is available here. */}
        <PaidHelpAction />
        <button
          type="button"
          className="navbar__theme-toggle"
          onClick={toggle}
          aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
        >
          {theme === 'dark' ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
        </button>
        <SyncIndicator signinFailed={signinFailed} />
        {isAuthed ? (
          <>
            <button
              type="button"
              className="navbar__user-chip"
              aria-haspopup="menu"
              aria-expanded={userMenu !== null}
              // Without this, the menu's document-level click-away (pointerdown) closes it before
              // this button's click re-opens it — the chip could then never toggle the menu shut.
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                const r = e.currentTarget.getBoundingClientRect()
                setUserMenu(userMenu === null ? { x: r.left, y: r.bottom + 4 } : null)
              }}
            >
              <span className="navbar__user-name">Signed in as {userDisplayName(auth.user)}</span>
              {role && <span className="navbar__role">{role}</span>}
              <ChevronDown className="navbar__user-caret" size={13} aria-hidden="true" />
            </button>
            {userMenu !== null && (
              <LayerContextMenu
                x={userMenu.x}
                y={userMenu.y}
                items={[
                  {
                    label: 'Profile',
                    onSelect: () => {
                      setUserMenu(null)
                      navigate('/profile')
                    },
                  },
                  // What the browser bundle contains, under which licenses, and this release's source.
                  {
                    label: 'Open-source licenses',
                    onSelect: () => {
                      setUserMenu(null)
                      window.open(LICENSES_URL, '_blank', 'noopener,noreferrer')
                    },
                  },
                  ...(SOURCE_URL
                    ? [
                        {
                          label: 'Source code',
                          onSelect: () => {
                            setUserMenu(null)
                            window.open(SOURCE_URL, '_blank', 'noopener,noreferrer')
                          },
                        },
                      ]
                    : []),
                  {
                    label: 'Sign out',
                    onSelect: () => {
                      setUserMenu(null)
                      void logout()
                    },
                  },
                ]}
                onClose={() => setUserMenu(null)}
                ariaLabel="Account"
              />
            )}
          </>
        ) : (
          <>
            {/* Anonymous visitors on a public-face deployment never see the sign-in screen and have no
                account menu, yet they receive the browser bundle too — so its licenses get a quiet link
                here. Signed-in users find it in the account menu. */}
            <a
              className="navbar__legal"
              href={LICENSES_URL}
              target="_blank"
              rel="noopener noreferrer"
              title="Open-source licenses"
            >
              Licenses
            </a>
            <button
              className="btn btn--sm"
              onClick={() => {
                // A rejected redirect (the auth address unreachable or its certificate refused) used
                // to be swallowed: the button did nothing, silently. Now the pill says what to open.
                setSigninFailed(false)
                auth.signinRedirect().catch(() => setSigninFailed(true))
              }}
            >
              Sign in
            </button>
          </>
        )}
      </div>
    </header>
  )
}
