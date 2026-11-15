/**
 * Talus 18c — the map address-search control. A debounced autocomplete box that
 * CONSUMES the /geocode proxy (Talus never builds a geocoder): type an address ->
 * suggestions from the configured external locator -> pick one (or press Enter on
 * free text) -> the parent flies the map to it and drops an ephemeral marker.
 *
 * Pure UI: it owns the input + suggestion list + request lifecycle (debounce, cancel
 * in-flight on each keystroke) and hands the resolved candidate up via onPick; AssetMap
 * owns the map (fly-to + marker). Fails soft — an empty/unconfigured/down geocoder just
 * yields no suggestions, never an error the user must clear.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { MapPin } from 'lucide-react'

import {
  ApiError,
  fetchGeocodeLocators,
  fetchGeocodeSearch,
  fetchGeocodeSuggest,
} from '@/lib/api'
import type { GeoCandidate, GeoSuggestion } from '@/lib/api'
import CloseButton from '@/components/CloseButton'


const DEBOUNCE_MS = 300
// Don't fire until there's enough to match — keeps the autocomplete quiet on 1-2 chars.
const MIN_CHARS = 3

export interface GeocodeSearchProps {
  accessToken: string | undefined
  onPick: (candidate: GeoCandidate) => void
  onClear: () => void
  // The current map viewport as "xmin,ymin,xmax,ymax" (EPSG:4326), to bias/restrict results
  // to what the user is looking at. Returns undefined before the map is ready.
  getViewportBBox: () => string | undefined
  // Report the "Search in" picker's selection up (null = All), so the map's "What's here?" reverse-geocode
  // can use the SAME locator the user has chosen for forward search (decision-reverse-geocode.md).
  onScopeChange?: (locator: string | null) => void
}

export default function GeocodeSearch({
  accessToken,
  onPick,
  onClear,
  getViewportBBox,
  onScopeChange,
}: GeocodeSearchProps) {
  const [q, setQ] = useState('')
  const [suggestions, setSuggestions] = useState<GeoSuggestion[]>([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeIdx, setActiveIdx] = useState(-1)
  // Collapsed by default: the control is just a search icon in the lower-left control stack
  // (mirrors ArcGIS Online); clicking it reveals the bar (ESC / × collapses + clears).
  const [expanded, setExpanded] = useState(false)
  // The configured locators + which one the "Search in" picker has selected (null = All).
  const [locators, setLocators] = useState<string[]>([])
  const [scope, setScope] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // Set before a programmatic setQ (resolving a pick) so writing the resolved label back
  // into the box doesn't re-fire autocomplete and re-open the dropdown on what was just picked.
  const skipNextSuggestRef = useRef(false)

  // Focus the input when the bar opens so the user can type immediately.
  useEffect(() => {
    if (expanded) inputRef.current?.focus()
  }, [expanded])

  // Load the configured locator names once (drives the "Search in" picker). Fails quiet —
  // an empty list just means no picker, search still works against the default order.
  useEffect(() => {
    if (!accessToken) return
    let cancelled = false
    fetchGeocodeLocators(accessToken)
      .then((ls) => {
        if (!cancelled) setLocators(ls)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [accessToken])

  // Debounced autocomplete; the previous in-flight request is aborted on each keystroke
  // so a slow response can't land after a newer one (last-write-wins).
  useEffect(() => {
    if (skipNextSuggestRef.current) {
      skipNextSuggestRef.current = false
      setOpen(false)
      return
    }
    const text = q.trim()
    if (!accessToken || text.length < MIN_CHARS) {
      setSuggestions([])
      setOpen(false)
      return
    }
    const timer = window.setTimeout(() => {
      abortRef.current?.abort()
      const ctrl = new AbortController()
      abortRef.current = ctrl
      setBusy(true)
      setError(null)
      fetchGeocodeSuggest(
        text,
        accessToken,
        { locator: scope ?? undefined, bbox: getViewportBBox() },
        ctrl.signal,
      )
        .then(({ suggestions: s, unavailable }) => {
          setSuggestions(s)
          setOpen(s.length > 0)
          setActiveIdx(-1)
          // Every configured locator failed (not "no match"): say so instead of an empty list.
          if (unavailable) setError('Address search is unavailable — every locator failed; tell your administrator')
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return // superseded
          setSuggestions([])
          setError(err instanceof ApiError ? `Search failed (HTTP ${err.status})` : 'Search failed')
        })
        .finally(() => setBusy(false))
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [q, accessToken, scope, getViewportBBox])

  const resolve = useCallback(
    async (params: { q?: string; source?: string; magicKey?: string }) => {
      if (!accessToken) return
      abortRef.current?.abort()
      setBusy(true)
      setError(null)
      setOpen(false)
      try {
        const candidates = await fetchGeocodeSearch(
          { ...params, locator: scope ?? undefined, bbox: getViewportBBox() },
          accessToken,
        )
        if (candidates.length > 0) {
          onPick(candidates[0])
          skipNextSuggestRef.current = true // writing the label back must not re-open the list
          setQ(candidates[0].label)
        } else {
          setError('No match found')
        }
      } catch (err) {
        // A 5xx carries the server's own sentence and its error code (the Helper answers about that code).
        setError(err instanceof ApiError ? (err.status >= 500 ? err.message : `Search failed (HTTP ${err.status})`) : 'Search failed')
      } finally {
        setBusy(false)
      }
    },
    [accessToken, onPick, scope, getViewportBBox],
  )

  const pickSuggestion = (s: GeoSuggestion): void => {
    void resolve({ q: s.text, source: s.source, magicKey: s.magic_key ?? undefined })
  }

  const submitFreeText = (): void => {
    const text = q.trim()
    if (text.length >= MIN_CHARS) void resolve({ q: text })
  }

  const clear = (): void => {
    abortRef.current?.abort()
    setQ('')
    setSuggestions([])
    setOpen(false)
    setError(null)
    setActiveIdx(-1)
    onClear()
  }

  // × / Escape: dismiss the search — clear the box + pin and fold back to the icon.
  const collapse = (): void => {
    clear()
    setExpanded(false)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setOpen(true)
      setActiveIdx((i) => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx((i) => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      if (open && activeIdx >= 0 && suggestions[activeIdx]) pickSuggestion(suggestions[activeIdx])
      else submitFreeText()
    } else if (e.key === 'Escape') {
      if (open) setOpen(false)
      else collapse()
    }
  }

  // Collapsed: just the search icon, sitting in the lower-left control stack.
  if (!expanded) {
    return (
      <div className="geocode-search geocode-search--collapsed">
        <button
          className="geocode-search__toggle"
          type="button"
          aria-label="Search address"
          onClick={() => setExpanded(true)}
        >
          <MapPin size={16} aria-hidden="true" />
        </button>
      </div>
    )
  }

  // Expanded: the bar (anchored low-left) with suggestions opening UPWARD above it.
  return (
    <div className="geocode-search geocode-search--expanded">
      {open && suggestions.length > 0 && (
        <ul className="geocode-search__list" role="listbox">
          {suggestions.map((s, i) => (
            <li key={`${s.source}:${s.magic_key ?? s.text}:${i}`}>
              <button
                type="button"
                role="option"
                aria-selected={i === activeIdx}
                className={`geocode-search__opt${i === activeIdx ? ' is-active' : ''}`}
                onMouseEnter={() => setActiveIdx(i)}
                onClick={() => pickSuggestion(s)}
              >
                {s.text}
              </button>
            </li>
          ))}
        </ul>
      )}
      {error !== null && <div className="geocode-search__error">{error}</div>}
      <div className="geocode-search__bar">
        <span className="geocode-search__icon" aria-hidden="true">
          <MapPin size={16} aria-hidden="true" />
        </span>
        {locators.length > 1 && (
          <select
            className="geocode-search__scope"
            aria-label="Search in"
            title="Search in"
            value={scope ?? ''}
            onChange={(e) => {
              const next = e.target.value || null
              setScope(next)
              onScopeChange?.(next)
            }}
          >
            <option value="">All</option>
            {locators.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        )}
        <input
          ref={inputRef}
          className="geocode-search__input"
          type="text"
          value={q}
          placeholder="Find address or place"
          aria-label="Search address"
          autoComplete="off"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => {
            if (suggestions.length > 0) setOpen(true)
          }}
        />
        {busy && <span className="geocode-search__spinner" aria-hidden="true" />}
        <CloseButton onClick={collapse} label="Close search" className="geocode-search__clear" />
      </div>
    </div>
  )
}
