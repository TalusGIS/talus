/**
 * Is this page the embed mini-app, and is it inside someone else's frame?
 *
 * The edge decides which responses may be FRAMED: `proxy/Caddyfile` sends `frame-ancestors *` for
 * `/embed/*` and forbids framing everywhere else. The app decides what to RENDER. If the two read
 * a path differently, the edge hands out a frameable page and the app boots the full, signed-in
 * portal inside it — a clickjacking surface (once-over S4, decision-once-over-edge-deployment.md).
 *
 * The edge matches the path decoded, without regard to case, and with repeated slashes merged:
 * `/EMBED/x`, `/%65mbed/x`, `//embed/x`, `/embed%2Fx` and `/%2Fembed/x` are all served frameable
 * (measured on the dev edge, 2026-11-14). `isEmbedPath` therefore reads the path the same way.
 * It is not an exact mirror: the edge also resolves `.` and `..` hidden behind `%2F`
 * (`/x%2F..%2Fembed/y`), and decodes escapes that are not valid UTF-8, which `decodeURIComponent`
 * refuses. Those — and any spelling nobody thought of — land on the second lock, `isFramed`:
 * App renders NOTHING when it is framed on any other path. A blank frame, never the portal.
 */
export function isEmbedPath(pathname: string): boolean {
  let path = pathname
  try {
    path = decodeURIComponent(pathname)
  } catch {
    // a malformed escape (`%E0%A4%A`): compare the text as it came
  }
  return path.replace(/\/{2,}/g, '/').toLowerCase().startsWith('/embed/')
}

/** True when this window is not the top window. Reading `top` across origins can throw; a window
 *  that cannot see its top is, by that fact, framed by another origin. */
export function isFramed(win: { self: unknown; top: unknown }): boolean {
  try {
    return win.self !== win.top
  } catch {
    return true
  }
}
