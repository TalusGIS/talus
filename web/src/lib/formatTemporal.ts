/**
 * One display formatter for dates and times (a popup and the data
 * table showed `2024-09-26T00:52:27Z` and `2026-11-05T19:43:39.836279Z` as stored).
 *
 * DISPLAY ONLY. Exports, CSV, the API, edit forms, filters and sorting keep the stored value.
 *
 * It goes by the value's SHAPE, not the column's type, on purpose:
 *  - a timestamp imported from another system lands in a TEXT field (a Talus `date` field is
 *    date-only), so typing by column would leave exactly the reported case untouched;
 *  - a signed-out popup has no field types to consult.
 * Only a value that is, whole, one of three strict ISO-8601 shapes is formatted. Anything else —
 * `Inspected 2024-09-26`, `2024-9-26`, a part number — comes back untouched (null).
 *
 *  1. Date only       `2024-09-26`                  → `Sep 26, 2024`
 *     Built AND formatted in UTC: a date has no time zone, so it must never shift a day.
 *  2. With a zone     `2024-09-26T00:52:27Z`        → the viewer's local time, `Sep 25, 2024, 8:52 PM`
 *     An instant; shown where the viewer sits. The day can differ from the stored (UTC) day.
 *  3. Without a zone  `2024-09-26T00:52:27`         → as written, `Sep 26, 2024, 12:52 AM`
 *     Nothing to convert from, so nothing is converted.
 *
 * Seconds and fractions are dropped from the text; `raw` carries the stored value for a tooltip.
 */

export interface FormattedTemporal {
  /** What a person reads. */
  text: string
  /** The stored value, unchanged — for the hover tooltip. */
  raw: string
}

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/
// `T` or one space between date and time (Postgres writes a space); seconds and a fraction are
// optional; the zone is `Z`, `±hh`, `±hhmm` or `±hh:mm`.
const TIMESTAMP =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-]\d{2}(?::?\d{2})?)?$/i

// Built once: constructing an Intl formatter is the slow part, and a data table formats thousands.
// `undefined` locale = the browser's own (browser locale, medium + short).
const dateUtc = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeZone: 'UTC' })
const stampUtc = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' })
const stampLocal = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

/** A real calendar day? `Date.UTC` rolls 2024-02-31 over to March instead of refusing it. A year
 *  before 1000 is refused too: `0612-03-14` is a parcel or part number, not the year 612. */
function utcDay(y: number, m: number, d: number): Date | null {
  if (y < 1000) return null
  const t = new Date(Date.UTC(y, m - 1, d))
  return t.getUTCFullYear() === y && t.getUTCMonth() === m - 1 && t.getUTCDate() === d ? t : null
}

/**
 * Format `value` when it is exactly an ISO date or timestamp; otherwise null, and the caller
 * shows the value as it always has.
 */
export function formatTemporal(value: unknown): FormattedTemporal | null {
  if (typeof value !== 'string') return null
  // Cheap gate before any regex: both shapes start `dddd-` and run 10–35 characters.
  if (value.length < 10 || value.length > 35 || value.charCodeAt(4) !== 45) return null

  const d = DATE_ONLY.exec(value)
  if (d) {
    const day = utcDay(Number(d[1]), Number(d[2]), Number(d[3]))
    return day ? { text: dateUtc.format(day), raw: value } : null
  }

  const s = TIMESTAMP.exec(value)
  if (!s) return null
  const [, y, mo, da, h, mi, sec, zone] = s
  const hour = Number(h)
  const minute = Number(mi)
  const second = sec === undefined ? 0 : Number(sec)
  const day = utcDay(Number(y), Number(mo), Number(da))
  if (!day || hour > 23 || minute > 59 || second > 59) return null

  if (zone === undefined) {
    // No zone: lay the written wall-clock time on UTC and format in UTC, so it reads as written.
    const wall = new Date(Date.UTC(Number(y), Number(mo) - 1, Number(da), hour, minute, second))
    return { text: stampUtc.format(wall), raw: value }
  }

  // An instant. Normalise to the one spelling every engine parses: `T`, and `Z` or `±hh:mm`.
  let z = zone.toUpperCase()
  if (z !== 'Z') {
    const digits = z.slice(1).replace(':', '')
    z = `${z[0]}${digits.slice(0, 2)}:${digits.slice(2, 4) || '00'}`
  }
  const instant = new Date(`${y}-${mo}-${da}T${h}:${mi}:${sec ?? '00'}${z}`)
  if (Number.isNaN(instant.getTime())) return null
  return { text: stampLocal.format(instant), raw: value }
}

/** The display text alone, for call sites that have no place to hang a tooltip. */
export function temporalText(value: unknown): string | null {
  return formatTemporal(value)?.text ?? null
}
