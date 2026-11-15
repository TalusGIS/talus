/**
 * Copy text to the clipboard, robust across deployment contexts.
 *
 * The async Clipboard API (`navigator.clipboard`) only exists in a SECURE context — HTTPS or
 * `localhost`. Talus dev/LAN deployments are frequently plain `http://` on an IP or hostname, where
 * `navigator.clipboard` is `undefined` — so a copy button relying only on it throws and silently
 * no-ops (the symptom: the button "works" but nothing lands on the clipboard). This falls back to the
 * legacy `document.execCommand('copy')` over an off-screen textarea, which works without HTTPS.
 *
 * Returns whether the copy succeeded, so callers can gate their "Copied" feedback on a real copy.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  // Preferred path: the async Clipboard API (secure contexts). May still reject (permissions
  // policy, document not focused) — fall through to the legacy path on any failure.
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // fall through
    }
  }
  // Fallback: select an off-screen textarea and execCommand('copy'). Deprecated but broadly
  // supported and, crucially, available in non-secure (http://) contexts.
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.top = '-9999px'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}
