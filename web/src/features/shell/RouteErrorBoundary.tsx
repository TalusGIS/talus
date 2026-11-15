/**
 * Route-level error boundary (decision-map-rename.md hardening rider, 2026-07-02). A runtime error in
 * any routed view previously rendered a BLANK page — no message, nothing actionable (the const-TDZ
 * class made that concrete twice). This catches render/lifecycle errors below the NavBar and shows a
 * visible, calm fallback instead: the shell stays alive, the error is logged, and navigating to another
 * page automatically resets the boundary (reset-on-route-change, NOT keyed remounts — normal navigation
 * behavior is untouched).
 *
 * Scope: React render errors only (event handlers and async code report through their own paths).
 * The security boundary is unaffected — this is presentation resilience.
 */
import { Component, type ReactNode } from 'react'
import { useLocation } from 'react-router'

class Boundary extends Component<
  { resetKey: string; children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error }
  }

  componentDidCatch(error: Error, info: { componentStack?: string | null }): void {
    // React logs the error itself in dev; this line keeps a greppable marker in prod consoles.
    console.error('[route-error-boundary]', error, info.componentStack ?? '')
  }

  componentDidUpdate(prev: { resetKey: string }): void {
    // Reset ONLY after a crash, when the user navigates elsewhere — the broken route stays fallback
    // until left, every other route renders normally.
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children
    return (
      <div className="route-error" role="alert">
        <h2>Something went wrong on this page</h2>
        <p className="route-error__msg">{this.state.error.message}</p>
        <p className="route-error__hint">
          Try reloading. If it keeps happening, note what you clicked and let your administrator know —
          the details are in the browser console.
        </p>
        <button type="button" className="btn" onClick={() => window.location.reload()}>
          Reload page
        </button>
      </div>
    )
  }
}

export default function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const location = useLocation()
  return <Boundary resetKey={location.pathname}>{children}</Boundary>
}
