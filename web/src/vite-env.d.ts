/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_OIDC_AUTHORITY?: string
  readonly VITE_OIDC_CLIENT_ID?: string
  readonly VITE_OIDC_REDIRECT_URI?: string
  readonly VITE_OIDC_POST_LOGOUT_REDIRECT_URI?: string
  readonly VITE_OIDC_SCOPE?: string
  readonly VITE_API_BASE_URL?: string
  readonly VITE_BASEMAP_PMTILES_URL?: string
  readonly VITE_IMAGERY_BASEMAP?: string
  /** The release this build is (set by the release build, e.g. v0.9.0-beta.17); absent in dev. */
  readonly VITE_TALUS_VERSION?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
