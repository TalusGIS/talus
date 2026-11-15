#!/usr/bin/env node
/* ============================================================
   check-tokens.mjs — token-pipeline guard (Talus 41)

   INVARIANT: design tokens (CSS custom properties) may only be
   DEFINED under web/src/styles/talus-ds/. Everywhere else in the
   CSS may only REFERENCE them via var(--…). This is what stops the
   token system re-fragmenting: no stray `:root { --brand: … }` or
   hard-coded palette creeping back into global.css or a feature file.

   Usage:
     node scripts/check-tokens.mjs          # report + non-zero exit on violation
   Wire into CI / package.json:
     "check:tokens": "node scripts/check-tokens.mjs src"
   and add it to the lint/build gate.

   Rule detail:
     • Scans every *.css under web/src EXCEPT styles/talus-ds/**.
     • Flags any custom-property DEFINITION — a line where `--name:`
       appears in property position (i.e. not inside a var() call).
     • var(--x) usages are ignored (a `--x` inside `var(` has no
       trailing colon in property position).
   Tune ALLOW below if a file legitimately needs a local scoped var.

   TSX guard (handoff-admin-1a §4): the SystemView token bugs lived in
   inline style={{…}} objects, which the CSS scan never sees — literal
   hex satisfied a computed-token check for a month. So *.tsx is also
   scanned, for:
     • var(--ok…) / var(--warn…) / var(--danger…) — tokens that do not
       exist (the real ones are --success-600 / --warning-600 /
       --danger-600 and friends).
     • a hex colour (#abc / #aabbcc / #aabbccdd) inside a style={{…}}
       object, including multi-line ones. Hex OUTSIDE style objects is
       untouched — data palettes (feature colours) are legitimate.

   COMPAT guard (handoff-auth-opendata-4 §6.3): the styles/talus-ds/
   compat.css `--color-*` shim is a SHRINKING set. Two aliases survive
   (--color-muted, --color-text); any OTHER `var(--color-…)` in CSS or
   TSX is either a retired alias coming back (--color-accent,
   --color-bg — both were correctness bugs, see compat.css) or one that
   was never defined at all (--color-danger &c., which silently fell
   through to a hardcoded hex and never flipped theme). Nothing new
   joins the shim.

   GLYPH guard (handoff-record-table-3 §1.1, docs/css-conventions.md §2):
     ×, ▾, ▸, ✕, ⋯, ⋮, ★, ☆, ↑, ↓, ‹, › as a CONTROL — the third
     recurrence of a finding that kept being fixed per-surface. Use
     lucide-react (via components/CloseButton.tsx and components/Caret.tsx).

     A glyph is a CONTROL when it is the ENTIRE trimmed content of a JSX
     text node or a string literal: <button>×</button>, {open ? '▾' : '▸'}.
     A glyph with other text beside it is PROSE and is left alone —
     "Account ▸ Access tokens" and "24×24" are correct copy for QGIS/QField
     users, and a corpus-wide ban would force rewriting them. Comment lines
     are skipped for the same reason.
   ============================================================ */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const ROOT = process.argv[2] || 'web/src';
const CANONICAL = join('styles', 'talus-ds'); // the one place definitions may live
const ALLOW = [
  // e.g. 'features/map/legacyThing.css'  — add sparingly, with a reason
];

// A custom-property DEFINITION: `--name:` in property position.
// Negative-ish guard: we strip var(...) usages first, then match `--x:`.
const DEF = /(^|[{;]|\s)(--[a-z0-9][a-z0-9-]*)\s*:/gi;

function walk(dir, ext, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, ext, out);
    else if (name.endsWith(ext)) out.push(p);
  }
  return out;
}

function stripVarUsages(line) {
  // Remove every var(--x[, fallback]) so its inner `--x` can't look like a def.
  return line.replace(/var\(\s*--[a-z0-9-]+[^)]*\)/gi, 'var()');
}

const files = walk(ROOT, '.css').filter((f) => {
  const rel = relative(ROOT, f);
  if (rel.split(sep).join('/').startsWith(CANONICAL.split(sep).join('/'))) return false;
  if (ALLOW.some((a) => rel.split(sep).join('/') === a)) return false;
  return true;
});

const violations = [];
for (const file of files) {
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((raw, i) => {
    const line = stripVarUsages(raw);
    let m;
    DEF.lastIndex = 0;
    while ((m = DEF.exec(line))) {
      violations.push({ file, line: i + 1, name: m[2], src: raw.trim() });
    }
  });
}

/* ---- TSX guard (handoff-admin-1a §4) ---- */

// Undefined-token families that keep sneaking back via inline styles.
const BAD_VAR = /var\(--(ok|warn|danger)[,)\s]/g;
// A CSS hex colour: 3, 6, or 8 digits, not part of a longer hex/word run.
const HEX = /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?(?![0-9a-fA-F])/g;

/* Control glyphs: banned when standing alone as an affordance, allowed inside prose. */
const GLYPHS = '×✕▾▸⋯⋮★☆↑↓‹›';

/* Second bite (handoff-auth-opendata-4 §1.3). The ENTIRE-node rule below missed
   `← Open Data` twice over: `←` was not in GLYPHS at all, and even if it had been, the
   glyph sits BESIDE its label rather than alone. That shape — arrow then label, or label
   then arrow — is a control decoration every time, and it had already been fixed
   per-surface four times.
   EDGE_GLYPHS is the arrow family only. The rest of GLYPHS stays entire-node-only,
   because ×/★/▾ legitimately open or close prose (`24×24`, a rating line) in a way an
   arrow glued to a short label does not. The 40-char ceiling keeps it to labels: a
   sentence that happens to start with an arrow is not a button. */
const EDGE_GLYPHS = '←→';
const GLYPH_EDGE_NODE = new RegExp(`>\\s*([^<>{}]{1,40}?)\\s*<`, 'g');
const GLYPH_EDGE_LITERAL = new RegExp(`(['"\`])\\s*([^'"\`\\n]{1,40}?)\\s*\\1`, 'g');
const isEdgeGlyph = (t) => t.length > 0 && (EDGE_GLYPHS.includes(t[0]) || EDGE_GLYPHS.includes(t[t.length - 1]));
// `>×<` — a JSX text node that is nothing but the glyph.
const GLYPH_NODE = new RegExp(`>\\s*[${GLYPHS}]\\s*<`, 'g');
// `'×'` / `"×"` / `` `×` `` — a string literal that is nothing but the glyph.
const GLYPH_LITERAL = new RegExp(`(['"\`])\\s*[${GLYPHS}]\\s*\\1`, 'g');
// A whole line that is just the glyph — the multi-line <button>\n  ×\n</button> shape.
const GLYPH_LINE = new RegExp(`^[${GLYPHS}]$`);

const tsxFiles = walk(ROOT, '.tsx');
for (const file of tsxFiles) {
  const lines = readFileSync(file, 'utf8').split('\n');
  // Depth of unclosed braces opened at/after a `style={{` — >0 means we are inside an
  // inline style object (multi-line ones included; the SystemView bugs were multi-line).
  let styleDepth = 0;
  lines.forEach((raw, i) => {
    let m;
    // Glyph guard. Comments are prose by definition; so is a glyph with text beside it.
    const trimmed = raw.trim();
    if (!trimmed.startsWith('*') && !trimmed.startsWith('//') && !trimmed.startsWith('/*')) {
      GLYPH_NODE.lastIndex = 0;
      GLYPH_LITERAL.lastIndex = 0;
      const glyph =
        GLYPH_NODE.exec(raw)?.[0] ??
        GLYPH_LITERAL.exec(raw)?.[0] ??
        (GLYPH_LINE.test(trimmed) ? trimmed : null);
      if (glyph) {
        violations.push({ file, line: i + 1, name: `control glyph ${glyph.trim()}`, src: trimmed });
      }
      // Arrow at the edge of a short label — `← Open Data`, `Next →`.
      for (const re of [GLYPH_EDGE_NODE, GLYPH_EDGE_LITERAL]) {
        re.lastIndex = 0;
        let e;
        while ((e = re.exec(raw))) {
          const text = (e[2] ?? e[1] ?? '').trim();
          if (isEdgeGlyph(text)) {
            violations.push({ file, line: i + 1, name: `edge glyph ${text}`, src: trimmed });
          }
        }
      }
    }
    BAD_VAR.lastIndex = 0;
    while ((m = BAD_VAR.exec(raw))) {
      violations.push({ file, line: i + 1, name: `var(--${m[1]}…)`, src: raw.trim() });
    }
    // Walk the line character-wise, collecting the exact spans that sit inside a
    // style={{…}} object — so `style={{` on one line and its hex three lines later are
    // connected, and hex AFTER the object closes on the same line is not flagged.
    const spans = [];
    let idx = 0;
    let spanStart = styleDepth > 0 ? 0 : -1;
    while (idx < raw.length) {
      if (styleDepth === 0) {
        const open = raw.indexOf('style={{', idx);
        if (open === -1) break;
        styleDepth = 2; // the `{{`
        idx = open + 'style={{'.length;
        spanStart = idx;
      } else {
        const ch = raw[idx];
        if (ch === '{') styleDepth += 1;
        else if (ch === '}') styleDepth -= 1;
        idx += 1;
        if (styleDepth === 0) {
          spans.push([spanStart, idx]);
          spanStart = -1;
        }
      }
    }
    if (styleDepth > 0 && spanStart !== -1) spans.push([spanStart, raw.length]);
    if (spans.length > 0) {
      HEX.lastIndex = 0;
      while ((m = HEX.exec(raw))) {
        const at = m.index;
        if (spans.some(([a, b]) => at >= a && at < b)) {
          violations.push({ file, line: i + 1, name: m[0], src: raw.trim() });
        }
      }
    }
  });
}

/* ---- COMPAT guard (handoff-auth-opendata-4 §6.3) ---- */

// The only `--color-*` aliases compat.css still mints. Everything else is a
// retired alias or one that never existed; both fail. Shrink this list when an
// alias is retired — never grow it.
const COMPAT_SURVIVORS = new Set(['--color-muted', '--color-text']);
const COMPAT_VAR = /var\(\s*(--color-[a-z0-9-]+)/g;

for (const file of [...files, ...tsxFiles]) {
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((raw, i) => {
    const trimmed = raw.trim();
    // Comments name these aliases on purpose (compat.css's own ledger, global.css's header).
    if (trimmed.startsWith('*') || trimmed.startsWith('//') || trimmed.startsWith('/*')) return;
    let m;
    COMPAT_VAR.lastIndex = 0;
    while ((m = COMPAT_VAR.exec(raw))) {
      if (!COMPAT_SURVIVORS.has(m[1])) {
        violations.push({ file, line: i + 1, name: `compat alias ${m[1]}`, src: trimmed });
      }
    }
  });
}

if (violations.length === 0) {
  console.log(
    `✓ token guard: ${files.length} CSS + ${tsxFiles.length} TSX files scanned — no token definitions outside ${CANONICAL}/, no undefined var(--ok|warn|danger…), no hex inside style={{…}}, no control or edge glyphs and no new --color-* compat alias`,
  );
  process.exit(0);
}

console.error(`✗ token guard: ${violations.length} violation(s)\n`);
for (const v of violations) {
  console.error(`  ${relative(process.cwd(), v.file)}:${v.line}  ${v.name}`);
  console.error(`      ${v.src}`);
}
console.error(`\nDefine tokens under ${CANONICAL}/ and reference them with var(--…) elsewhere.`);
console.error(`In TSX: use the real tokens (--success-600 / --warning-600 / --danger-600, --warning-50…)`);
console.error(`instead of var(--ok|warn|danger…) or literal hex inside style={{…}}.`);
console.error(`For a control glyph: use lucide-react — components/CloseButton.tsx for dismiss,`);
console.error(`components/Caret.tsx for disclosure. Prose keeps its glyph.`);
console.error(`For a compat alias: reference the real token — see the RETIRED ledger in`);
console.error(`styles/talus-ds/compat.css for what each retired --color-* actually meant.`);
console.error(`If this is an intentional local scoped var, add its path to ALLOW in check-tokens.mjs.`);
process.exit(1);
