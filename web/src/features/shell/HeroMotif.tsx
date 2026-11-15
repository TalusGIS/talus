/**
 * The brand's signature "TIN" texture — a sparse triangulated-irregular-network of amber nodes and
 * edges — drawn behind the dark Home / Open Data heroes (06-main-pages.md). Low opacity; a left→right
 * dark scrim (see `.hero__scrim` in global.css) sits over it so the eyebrow/title read on solid dark.
 * `--accent` (amber) in both themes; the hero band itself is always-dark (`--blue-950`), so the motif
 * never needs per-theme handling.
 */
const NODES: [number, number][] = [
  [80, 60], [200, 120], [160, 220], [340, 70], [420, 160], [500, 100],
  [560, 40], [640, 130], [700, 90], [760, 200], [620, 240], [300, 210],
]

const EDGES = [
  'M80 60 200 120 160 220 40 180Z',
  'M200 120 340 70 420 160 300 210 160 220Z',
  'M340 70 500 100 560 40 640 130 420 160Z',
  'M560 40 700 90 760 200 620 240 560 40Z',
  'M420 160 560 40 620 240 480 250 300 210Z',
]

export default function HeroMotif() {
  return (
    <svg
      className="hero__motif"
      viewBox="0 0 800 300"
      preserveAspectRatio="xMidYMid slice"
      fill="none"
      aria-hidden
    >
      <g stroke="var(--accent)" strokeWidth={0.6} opacity={0.28}>
        {EDGES.map((d) => (
          <path key={d} d={d} />
        ))}
      </g>
      <g fill="var(--accent)">
        {NODES.map(([cx, cy]) => (
          <circle key={`${cx}-${cy}`} cx={cx} cy={cy} r={2.5} />
        ))}
      </g>
    </svg>
  )
}
