// Color ramps for the Types renderer (Talus 14). A ramp is applied ACROSS the unique
// values: categorical ramps cycle their distinct hues; sequential/diverging ramps
// interpolate across however many values there are (right for ordinal fields like
// condition Good->Replace). Individual stop colors stay editable after a ramp is applied.
// NOTE: this is the unique-value ramp picker — NOT the deferred graduated renderer (which
// bins a continuous field). Sequential ramps here are sampled across discrete classes.

export interface ColorRamp {
  name: string
  kind: 'sequential' | 'categorical'
  colors: string[] // control colors (>=2)
}

export const RAMPS: ColorRamp[] = [
  { name: 'Condition (Green→Red)', kind: 'sequential', colors: ['#1a9850', '#fee08b', '#fc8d59', '#d73027'] },
  { name: 'Blue→Red (diverging)', kind: 'sequential', colors: ['#2c7bb6', '#ffffbf', '#d7191c'] },
  { name: 'Viridis', kind: 'sequential', colors: ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725'] },
  { name: 'Blues', kind: 'sequential', colors: ['#eff3ff', '#9ecae1', '#3182bd', '#08519c'] },
  { name: 'Bright', kind: 'categorical', colors: ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'] },
  { name: 'Pastel', kind: 'categorical', colors: ['#a6cee3', '#b2df8a', '#fb9a99', '#fdbf6f', '#cab2d6', '#ffff99'] },
]

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
}

function rgbToHex(r: number, g: number, b: number): string {
  const c = (n: number) => Math.round(n).toString(16).padStart(2, '0')
  return `#${c(r)}${c(g)}${c(b)}`
}

function lerpHex(a: string, b: string, t: number): string {
  const [ar, ag, ab] = hexToRgb(a)
  const [br, bg, bb] = hexToRgb(b)
  return rgbToHex(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t)
}

/** N colors from a ramp. Categorical: cycle the hues. Sequential: interpolate across them. */
export function rampColors(ramp: ColorRamp, n: number, flip: boolean): string[] {
  if (n <= 0) return []
  const cols = flip ? [...ramp.colors].reverse() : ramp.colors
  if (ramp.kind === 'categorical') {
    return Array.from({ length: n }, (_, i) => cols[i % cols.length])
  }
  if (n === 1) return [cols[0]]
  return Array.from({ length: n }, (_, i) => {
    const pos = (i / (n - 1)) * (cols.length - 1)
    const lo = Math.floor(pos)
    const hi = Math.min(lo + 1, cols.length - 1)
    return lerpHex(cols[lo], cols[hi], pos - lo)
  })
}

/** A CSS linear-gradient string for previewing a ramp in the picker. */
export function rampPreview(ramp: ColorRamp): string {
  return `linear-gradient(to right, ${ramp.colors.join(', ')})`
}
