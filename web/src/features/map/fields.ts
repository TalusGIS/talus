/**
 * Shared field-list helpers for the per-layer FIELD PICKERS (FilterPanel + LabelsPanel). Both panels
 * feed from the same /layers/{id}/columns source and hide the same internal columns — one module keeps
 * the two pickers in lockstep (and keeps the component files fast-refresh-clean).
 */

// Columns that are internal/opaque and not useful to filter or label on interactively (audit FKs as raw
// uuids, the search index, the optimistic-concurrency counter, the media ref, the jsonb custom-field
// blob). Named data columns (facility_id, condition_code, created_at, …) stay. `geom` never reaches
// here (/columns omits it).
export const HIDDEN_FIELDS = new Set([
  'id', 'geom', 'ai_provenance', 'search_document', 'cover_photo_id',
  'created_by', 'updated_by', 'row_version', 'props',
])

/** Pretty field label: strip a trailing _code, unslug, title-case. Presentation only. */
export function fieldLabel(name: string): string {
  return name
    .replace(/_code$/, '')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}
