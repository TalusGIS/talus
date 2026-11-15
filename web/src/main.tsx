import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { AuthProvider } from 'react-oidc-context'
import { createBrowserRouter } from 'react-router'
import { RouterProvider } from 'react-router/dom'

import App from './App'
import { _setApiBaseUrl } from '@/lib/api/client'
import { buildOidcConfig } from '@/lib/auth/oidcConfig'
import { loadRuntimeConfig } from '@/lib/runtimeConfig'

// Talus 41 — self-hosted brand fonts (no Google CDN; air-gapped-ready) + the
// vendored design-system tokens, imported BEFORE global.css so every var(--…)
// resolves. global.css still owns the visible look until the commit-2 remap.
import '@fontsource-variable/inter/index.css'
import '@fontsource-variable/jetbrains-mono/index.css'
import './styles/talus-ds/index.css'
import './styles/global.css'

// A DATA router (decision-nav-blocker.md): the minimal mount `useBlocker` requires — one catch-all
// route rendering the whole app; PortalShell's descendant <Routes> stay exactly as they were. The
// blocker (AssetMap) intercepts in-app nav-aways from a dirty map; `beforeunload` covers the rest.
const router = createBrowserRouter([{ path: '*', element: <App /> }])

// 9a bootstrap: resolve runtime deployment config BEFORE anything renders — the OIDC
// authority and API base must be final before AuthProvider mounts or the first fetch
// fires. Fail-open: no /config.json (dev, legacy deploys) → build-time fallbacks.
async function bootstrap() {
  const runtime = await loadRuntimeConfig()
  if (runtime?.apiBase) _setApiBaseUrl(runtime.apiBase)
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <AuthProvider {...buildOidcConfig(runtime)}>
        <RouterProvider router={router} />
      </AuthProvider>
    </StrictMode>,
  )
}

void bootstrap()
