// Talus 41: set the theme before first paint so dark mode never flashes.
// Seeds "dark" when there's no stored pref (F-theme-default); the toggle persists
// to localStorage["talus-theme"]. Keep in sync with lib/theme.ts.
//
// Kept as an EXTERNAL same-origin script (loaded synchronously in <head> before
// paint) rather than inline, so the enforcing SPA Content-Security-Policy can stay
// `script-src 'self'` — no `'unsafe-inline'` and no per-build hash for an operator to
// track (decision-csp-enforce.md §4). An injected inline <script> is still blocked.
;(function () {
  try {
    var t = localStorage.getItem('talus-theme') || 'dark'
    document.documentElement.setAttribute('data-theme', t)
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'dark')
  }
})()
