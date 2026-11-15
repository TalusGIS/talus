import { useEffect, useState, type CSSProperties } from 'react'
import { useAuth } from 'react-oidc-context'

import HeroMotif from '@/features/shell/HeroMotif'
import { DEFAULT_HEADING, DEFAULT_INTRO, HERO_EYEBROW } from '@/features/shell/homeContent'
import {
  ApiError,
  fetchOrgSettings,
  updateOrgHome,
  type OrgHome,
} from '@/lib/api'

/**
 * Admin → Home page (handoff-admin-1a §3.3, split out of the Settings catch-all). The
 * public landing page's heading / intro / hero image, saved on its own endpoint
 * (PUT /org-settings/home — all three fields together, blank clears to the built-in
 * default). Renders the ACTUAL hero band above the fields (finding D5), updating as you
 * type — the same markup and defaults the live page uses, so what you preview is what
 * ships. Admin-gated server-side; behind RequireAdmin.
 */
export default function HomePageView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [saved, setSaved] = useState<OrgHome | null>(null)
  const [heading, setHeading] = useState('')
  const [intro, setIntro] = useState('')
  const [hero, setHero] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [justSaved, setJustSaved] = useState(false)

  useEffect(() => {
    if (!token) return
    let active = true
    fetchOrgSettings(token)
      .then((s) => {
        if (!active) return
        setSaved(s.home)
        setHeading(s.home.heading ?? '')
        setIntro(s.home.intro ?? '')
        setHero(s.home.heroImageUrl ?? '')
        setError(null)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : 'Failed to load settings'))
    return () => {
      active = false
    }
  }, [token])

  const dirty =
    saved !== null &&
    ((heading.trim() || null) !== (saved.heading ?? null) ||
      (intro.trim() || null) !== (saved.intro ?? null) ||
      (hero.trim() || null) !== (saved.heroImageUrl ?? null))

  function edit(fn: () => void): void {
    fn()
    setJustSaved(false)
  }

  async function save(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      const next = await updateOrgHome(
        {
          heading: heading.trim() || null,
          intro: intro.trim() || null,
          heroImageUrl: hero.trim() || null,
        },
        token,
      )
      setSaved(next)
      setHeading(next.heading ?? '')
      setIntro(next.intro ?? '')
      setHero(next.heroImageUrl ?? '')
      setJustSaved(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  const heroTrimmed = hero.trim()
  const heroImgStyle: CSSProperties | undefined = heroTrimmed
    ? {
        backgroundImage: `url(${heroTrimmed})`,
        backgroundSize: 'cover',
        backgroundPosition: 'center',
      }
    : undefined

  return (
    <div className="admin">
      <h1 className="admin__title">Home page</h1>
      <p className="admin__note">
        Customize the public landing page — the first thing visitors (public, contractors,
        staff) see. Maps and layers shared to the <strong>Home (Featured)</strong> group appear
        in the featured gallery. Leave a field blank to use the built-in default.
      </p>

      {error !== null && <p className="sa__error">{error}</p>}

      {saved === null && error === null ? (
        <p className="sa__muted">Loading…</p>
      ) : (
        <>
          <div className="home-hero-preview">
            <div className="hero hero--compact">
              {heroTrimmed ? <div className="hero__img" style={heroImgStyle} /> : <HeroMotif />}
              <div className="hero__scrim" />
              <div className="hero__inner">
                <p className="hero__eyebrow">{HERO_EYEBROW}</p>
                <h2 className="hero__title">{heading.trim() || DEFAULT_HEADING}</h2>
                <p className="hero__lede">{intro.trim() || DEFAULT_INTRO}</p>
              </div>
            </div>
          </div>

          <section className="org-settings__card">
            <label className="sign-form__field">
              <span>Heading</span>
              <input
                type="text"
                value={heading}
                maxLength={140}
                placeholder="e.g. City of Example — Maps & Data"
                onChange={(e) => edit(() => setHeading(e.target.value))}
              />
            </label>
            <label className="sign-form__field">
              <span>Intro</span>
              <textarea
                value={intro}
                rows={2}
                maxLength={1000}
                placeholder="e.g. A collection of maps and data our organization makes available to the public."
                onChange={(e) => edit(() => setIntro(e.target.value))}
              />
            </label>
            <label className="sign-form__field">
              <span>Hero image URL</span>
              <input
                type="url"
                value={hero}
                maxLength={1000}
                placeholder="https://… (optional background image)"
                onChange={(e) => edit(() => setHero(e.target.value))}
              />
            </label>

            <div className="sign-form__actions">
              {justSaved && !dirty && <span className="org-settings__saved">Saved.</span>}
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void save()}
                disabled={busy || !dirty}
              >
                {busy ? 'Saving…' : 'Save home page'}
              </button>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
