/**
 * Talus 41 — theme state. Single source of truth for the light/dark skin.
 *
 * The no-flash <script> in index.html sets `data-theme` on <html> before first
 * paint (seeding "dark" when there's no stored pref — F-theme-default). This hook
 * mirrors that attribute into React state and lets any component flip + persist it.
 * Persisted to localStorage["talus-theme"]; every `[data-theme="dark"]` remap in
 * the vendored tokens does the rest, so there are no per-component dark styles.
 */
import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'talus-theme'

/** The theme the no-flash init already committed to <html>, defaulting to dark. */
function currentTheme(): Theme {
  const attr = document.documentElement.getAttribute('data-theme')
  return attr === 'light' ? 'light' : 'dark'
}

/**
 * Subscribe to theme FLIPS, for code that cannot re-render its way to the new palette.
 *
 * ★ `useTheme()` below is deliberately per-caller local state — it mirrors the attribute and lets a
 * component flip it, but there is NO shared store, so a component that merely *reads* the theme
 * never learns that some other component toggled it. Anything that bakes concrete colour values at
 * render time (a canvas, in practice: ECharts reads CSS custom properties into a chart option and
 * keeps them until the option is rebuilt) needs the flip as an event.
 *
 * The `data-theme` attribute on <html> is the one signal every tree can see, including `paid/`
 * across the mounting seam — which is why this observes the DOM rather than exposing React state.
 * Returns an unsubscribe.
 */
export function subscribeTheme(onChange: () => void): () => void {
  const el = document.documentElement
  let last = el.getAttribute('data-theme')
  const mo = new MutationObserver(() => {
    const now = el.getAttribute('data-theme')
    if (now === last) return // other attribute writes on <html> are not our business
    last = now
    onChange()
  })
  mo.observe(el, { attributes: true, attributeFilter: ['data-theme'] })
  return () => mo.disconnect()
}

/**
 * A counter that increments on every theme flip. Add it to the dependency array of an effect that
 * has to rebuild colour-bearing state; the value itself is meaningless.
 *
 * Plain state + effect rather than `useSyncExternalStore`: that hook's `getSnapshot` must return a
 * cached value that changes ONLY when the store does, which for a DOM attribute means keeping a
 * module-level shadow copy — more machinery, and a caching contract to get wrong, for no gain here.
 * The updater stays pure, so StrictMode's double-invoke is harmless (same reason `useTheme` keeps
 * its side effect in an effect).
 */
export function useThemeVersion(): number {
  const [version, setVersion] = useState(0)
  useEffect(() => subscribeTheme(() => setVersion((n) => n + 1)), [])
  return version
}

export function useTheme(): { theme: Theme; toggle: () => void; setTheme: (t: Theme) => void } {
  const [theme, setThemeState] = useState<Theme>(currentTheme)

  // Side effect lives HERE, not in the setState updater: updaters must stay pure
  // (StrictMode double-invokes them, which made batched flips nondeterministic).
  // Reflecting state → DOM + storage in an effect is idempotent and order-safe.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme)
    } catch {
      /* private mode / storage disabled — the attribute still drives this session */
    }
  }, [theme])

  const setTheme = useCallback((t: Theme) => setThemeState(t), [])
  const toggle = useCallback(
    () => setThemeState((prev) => (prev === 'dark' ? 'light' : 'dark')),
    [],
  )

  return { theme, toggle, setTheme }
}
