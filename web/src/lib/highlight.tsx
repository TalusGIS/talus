import type { ReactNode } from 'react'

/**
 * Wrap every case-insensitive occurrence of `query` in `text` with an amber highlight mark (`.hl`),
 * so a search hit reads at a glance. Returns the plain text when there's no active query. Shared by
 * the Domains (name / value) and Groups (name) searches — 06-main-pages.md.
 */
export function Highlight({ text, query }: { text: string; query: string }): ReactNode {
  const needle = query.trim().toLowerCase()
  if (needle === '') return text
  const haystack = text.toLowerCase()
  const parts: ReactNode[] = []
  let from = 0
  let key = 0
  for (let at = haystack.indexOf(needle, from); at !== -1; at = haystack.indexOf(needle, from)) {
    if (at > from) parts.push(text.slice(from, at))
    parts.push(
      <mark key={key++} className="hl">
        {text.slice(at, at + needle.length)}
      </mark>,
    )
    from = at + needle.length
  }
  if (from < text.length) parts.push(text.slice(from))
  return <>{parts}</>
}
