/**
 * The unsaved-work baseline of a NEW map follows the basemap the portal puts it on.
 *
 * A new map's baseline ("what a Save would write for an untouched map") is taken when the page
 * mounts, on the built-in default look. A moment later the organization's default basemap arrives
 * and is applied — and, on a box with nothing downloaded, the cold-start fallback after it. Neither
 * is something the USER did, but both changed the map away from its baseline, so an untouched new
 * map said "Unsaved" and asked "Leave this map?" on every new install whose default is not Light —
 * which since streamed basemaps is every new install (once-over C1).
 *
 * The rule: when the portal (not the user) moves a new map from look `prev` to look `next`, and
 * the baseline is still exactly "a fresh map on `prev`", the baseline moves to "a fresh map on
 * `next`". If the user has already changed anything, or it is not a fresh-map baseline at all (a
 * saved map's, a draft's), it is left alone — a real change must still be reported.
 */
export function moveFreshBaseline<B>(
  baseline: string | null,
  freshSignature: (basemap: B) => string,
  prev: B,
  next: B,
): string | null {
  if (baseline === null || prev === next) return baseline
  return baseline === freshSignature(prev) ? freshSignature(next) : baseline
}
