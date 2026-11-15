import { useEffect, useState, type CSSProperties } from 'react'
import { Link } from 'react-router'
import { useAuth } from 'react-oidc-context'

import { fetchFeatured, type FeaturedItem, type OrgHome } from '@/lib/api'
import { GlobeIcon, LayerIcon, MapIcon } from '@/features/catalog/ItemTypeIcons'

import HeroMotif from './HeroMotif'
import { DEFAULT_HEADING, DEFAULT_INTRO, HERO_EYEBROW } from './homeContent'

/**
 * The portal landing (Talus 30) — a public discovery page for finding the org's maps & data, used by
 * the public, contractors, and staff. Hero text is admin-configurable (org_settings); the FEATURED
 * gallery is items shared to the reserved 'home' group, filtered server-side to what the viewer may
 * see (anon → public only). Renders for anonymous AND signed-in visitors. The primary destinations
 * (New Map / Catalog / Admin) live in the top nav, so the home stays a discovery surface — the only
 * card here is Open Data (not a nav destination). NOT a dashboard or a page-builder (drift fence).
 *
 * Refreshed (06-main-pages.md): a dark, always-dark branded hero (amber TIN motif + left scrim),
 * Featured tiles with a thumbnail band + Map/Layer type badge, and a single Open Data discovery card.
 */
export default function HomeView({ home }: { home: OrgHome | null }) {
  const auth = useAuth()
  // Send a token ONLY when truly signed in — a lingering EXPIRED token (auth.user persists with
  // isAuthenticated false) would otherwise 401 the optional-auth featured fetch and blank the public
  // gallery. The public home must always degrade to the anon (public-only) view (matches OpenDataView).
  const token = auth.isAuthenticated ? auth.user?.access_token : undefined

  const [featured, setFeatured] = useState<FeaturedItem[] | null>(null)

  useEffect(() => {
    let active = true
    fetchFeatured(token)
      .then((items) => active && setFeatured(items))
      .catch(() => active && setFeatured([])) // home must never break on a flaky featured fetch
    return () => {
      active = false
    }
  }, [token])

  const heading = home?.heading?.trim() || DEFAULT_HEADING
  const intro = home?.intro?.trim() || DEFAULT_INTRO
  // A custom admin hero image replaces the TIN motif (kept behind a dark scrim for legibility).
  const heroImgStyle: CSSProperties | undefined = home?.heroImageUrl
    ? { backgroundImage: `url(${home.heroImageUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' }
    : undefined

  return (
    <div className="home">
      <div className="hero">
        {home?.heroImageUrl ? <div className="hero__img" style={heroImgStyle} /> : <HeroMotif />}
        <div className="hero__scrim" />
        <div className="hero__inner">
          <p className="hero__eyebrow">{HERO_EYEBROW}</p>
          <h1 className="hero__title">{heading}</h1>
          <p className="hero__lede">{intro}</p>
        </div>
      </div>

      <div className="home__body">
        <section className="home__section">
          <div className="home__section-head">
            <h2 className="home__section-title">Featured</h2>
            {featured !== null && featured.length > 0 && (
              <span className="home__count">
                {featured.length} item{featured.length === 1 ? '' : 's'}
              </span>
            )}
          </div>
          {featured === null ? (
            <p className="home__muted">Loading…</p>
          ) : featured.length === 0 ? (
            <p className="home__muted">Nothing featured yet.</p>
          ) : (
            <div className="home__gallery">
              {featured.map((f) => (
                <FeaturedCard key={`${f.itemType}:${f.itemId}`} item={f} />
              ))}
            </div>
          )}
        </section>

        <section className="home__section">
          <Link to="/open-data" className="home__card">
            <span className="home__card-icon">
              <GlobeIcon />
            </span>
            <span className="home__card-text">
              <span className="home__card-title">Open Data</span>
              <span className="home__card-body">
                Explore, preview, and download our organization’s public datasets.
              </span>
            </span>
            <span className="home__card-chevron" aria-hidden>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="m9 18 6-6-6-6" />
              </svg>
            </span>
          </Link>
        </section>
      </div>
    </div>
  )
}

function FeaturedCard({ item }: { item: FeaturedItem }) {
  // Maps open in the viewer (public-capable); layers open in the catalog detail (sign-in for now).
  const isMap = item.itemType === 'web_map'
  const to = isMap ? `/map/${item.itemId}` : `/catalog/${item.itemId}`
  return (
    <Link to={to} className="home__tile">
      <span className={`home__tile-thumb${isMap ? ' home__tile-thumb--map' : ''}`}>
        {isMap ? <MapIcon /> : <LayerIcon />}
        <span className={`home__tile-badge${isMap ? ' home__tile-badge--map' : ''}`}>
          {isMap ? 'Map' : 'Layer'}
        </span>
      </span>
      <span className="home__tile-body">
        <span className="home__tile-title">{item.title}</span>
        {item.description && <span className="home__tile-desc">{item.description}</span>}
        {item.ownerName && <span className="home__tile-owner">by {item.ownerName}</span>}
      </span>
    </Link>
  )
}
