/**
 * The Talus lockup — TIN mark + wordmark.
 *
 * INLINED, deliberately: handoff-auth-opendata-4 §2.1 asked for
 * `assets/talus-lockup-standard.svg`, but an `<img src="*.svg">` renders in an isolated
 * context where the page's webfonts do NOT apply — the wordmark's `Inter` would silently
 * fall back to system-ui on the one screen whose entire job is to say "this is Talus".
 * Inline, it uses the real bundled Inter. It also lets the ink be `currentColor`, so one
 * component serves both themes instead of shipping a second white .svg and switching on
 * `[data-theme]`.
 *
 * Colours are tokens, not hex, and that is not a liberty: colors.css mints
 * `--blue-700` "CORE BRAND BLUE — logo" (#1f4d7a) and `--amber-500` "CORE ACCENT — logo
 * nodes" (#f59e0b) FROM this mark, so the tokens are the logo's own palette. `--accent`
 * resolves to #f59e0b in both themes, so the nodes hold; the ink follows `currentColor`
 * (callers pass `--text-brand`, which is per-theme). Same treatment as HeroMotif, which
 * already strokes the TIN with `var(--accent)`.
 */
export default function TalusLockup({ height = 26 }: { height?: number }) {
  return (
    <svg
      className="talus-lockup"
      viewBox="0 0 690 200"
      height={height}
      width={height * 3.45}
      role="img"
      aria-label="Talus"
    >
      <g transform="translate(120, 40)">
        <path
          d="M 0,0 L 131,0 L 131,55 L 76.5,55 L 76.5,120 L 54.5,120 L 54.5,55 L 0,55 Z"
          fill="currentColor"
        />
        <g
          transform="scale(0.546)"
          stroke="var(--accent)"
          strokeWidth={2.5}
          strokeLinecap="round"
          fill="none"
        >
          <line x1="30" y1="35" x2="120" y2="35" />
          <line x1="120" y1="35" x2="210" y2="35" />
          <line x1="30" y1="35" x2="120" y2="75" />
          <line x1="210" y1="35" x2="120" y2="75" />
          <line x1="120" y1="35" x2="120" y2="75" />
          <line x1="120" y1="75" x2="120" y2="205" />
        </g>
        <g transform="scale(0.546)" fill="var(--accent)">
          <polygon points="19,35 24.5,25.47 35.5,25.47 41,35 35.5,44.53 24.5,44.53" />
          <polygon points="109,35 114.5,25.47 125.5,25.47 131,35 125.5,44.53 114.5,44.53" />
          <polygon points="199,35 204.5,25.47 215.5,25.47 221,35 215.5,44.53 204.5,44.53" />
          <polygon points="108,75 114,64.61 126,64.61 132,75 126,85.39 114,85.39" />
          <polygon points="109,205 114.5,195.47 125.5,195.47 131,205 125.5,214.53 114.5,214.53" />
        </g>
      </g>
      <text
        x="280"
        y="160"
        fontFamily="var(--font-sans)"
        fontSize="140"
        fontWeight="700"
        fill="currentColor"
        letterSpacing="-4"
      >
        Talus
      </text>
    </svg>
  )
}
