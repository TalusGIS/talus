// The 44 px thumb-target floor for Talus Field (design review P3.2, rung d3): every interactive block
// in paid/web/src/field/field.css that declares a min-height must declare 44px or more. Display-only
// blocks (text, hints, labels, the pointer-events:none accuracy chip) are exempt by name.
import { existsSync, readFileSync } from 'node:fs'

const cssUrl = new URL('../../paid/web/src/field/field.css', import.meta.url)
if (!existsSync(cssUrl)) {
  console.log('Talus Field is part of Talus+ and is not in this tree: nothing to check.')
  process.exit(0)
}
const css = readFileSync(cssUrl, 'utf8')
const EXEMPT = /(__hint|__text|__state|__title|__label|__sub|__row|__line|__muted|__file|__photo|^\.acc\b|__summary|__status|__heading|__flash\b|__notice|__steps|__qr|__brand\b|^\.gpsstrip\b)/
const bad = []
for (const m of css.matchAll(/(^|\n)(\.[^{\n]+)\{([^}]*)\}/g)) {
  const sel = m[2].trim()
  const mh = /min-height:\s*(\d+)px/.exec(m[3])
  if (!mh || EXEMPT.test(sel)) continue
  if (Number(mh[1]) < 44) bad.push(`${sel} → min-height: ${mh[1]}px`)
}
if (bad.length) {
  console.error('✗ field targets under the 44 px floor:\n  ' + bad.join('\n  '))
  process.exit(1)
}
console.log('✓ field targets: every interactive block with a min-height is ≥ 44 px')
