/**
 * The release this build is, and the two links every recipient of the bundle can follow: the
 * open-source licenses of what it contains (written beside the bundle at build time by
 * scripts/third-party-licenses.mjs, served as a static file, readable signed out) and the exact
 * source of this release — which also helps anyone who modifies Talus meet AGPL §13 for their users.
 */
export const TALUS_VERSION: string | undefined = import.meta.env.VITE_TALUS_VERSION || undefined

export const LICENSES_URL = '/third-party-licenses.txt'

/** The release's tagged source tree; absent in a development build, which has no tag to point at. */
export const SOURCE_URL: string | undefined = TALUS_VERSION
  ? `https://github.com/TalusGIS/talus/tree/${encodeURIComponent(TALUS_VERSION)}`
  : undefined
