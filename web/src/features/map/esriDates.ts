/** ArcGIS date values, as a feature service writes them in GeoJSON, to Talus date values.
 *
 *  An ArcGIS `esriFieldTypeDate` field arrives from `/query?f=geojson` as milliseconds since 1970
 *  (UTC), a bare number. A Talus date field holds a calendar day, `YYYY-MM-DD`. The day is cut in
 *  UTC, the same cut the feature-service door makes for the same wire value, so one layer gets the
 *  same day whichever door it arrives through; a date entered with no time of day is stored at
 *  midnight and keeps its day. The time of day is not kept.
 *
 *  No imports: this file is also loaded directly by `node --test`. */

/** Names of the fields a layer's metadata declares as dates. `fields` is the remote service's own
 *  JSON — untrusted, so anything that is not a named date field is ignored. */
export function esriDateFieldNames(fields: unknown): string[] {
  if (!Array.isArray(fields)) return []
  const out: string[] = []
  for (const f of fields as Array<{ name?: unknown; type?: unknown } | null>) {
    if (f && f.type === 'esriFieldTypeDate' && typeof f.name === 'string' && f.name !== '') out.push(f.name)
  }
  return out
}

/** Epoch milliseconds → `YYYY-MM-DD` (UTC). Anything that is not a finite number inside years
 *  0001–9999 is returned untouched: null stays null, a string stays a string, and a value no date
 *  can hold is left for the import to refuse by name instead of being silently dropped. */
export function esriDateToIsoDay(v: unknown): unknown {
  if (typeof v !== 'number' || !Number.isFinite(v)) return v
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  const year = d.getUTCFullYear()
  if (year < 1 || year > 9999) return v
  return d.toISOString().slice(0, 10)
}

/** Rewrite the named date properties of each feature, in place. */
export function convertEsriDateProps(
  features: Array<{ properties?: Record<string, unknown> | null }>,
  dateFields: readonly string[],
): void {
  if (dateFields.length === 0) return
  for (const f of features) {
    const p = f?.properties
    if (!p) continue
    for (const k of dateFields) {
      if (Object.prototype.hasOwnProperty.call(p, k)) p[k] = esriDateToIsoDay(p[k])
    }
  }
}
