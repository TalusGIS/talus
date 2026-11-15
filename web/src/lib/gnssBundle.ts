import type { CustomFieldCreate } from '@/lib/api'

/**
 * The GNSS metadata fields bundle (decision-gnss-metadata-fields.md) — Talus's answer to AGO/Pro
 * "Add GPS Metadata Fields" (the ESRIGNSS_* set). Twelve ordinary custom fields capturing per-feature
 * GNSS provenance/QA: accuracy, fix, DOPs, receiver. Added one-click from the create wizard or an
 * existing layer's Fields panel; populated by QField default-value expressions (connect-desktop.md
 * recipe) and, later, natively by the Talus Field PWA.
 *
 * ★ These exact fieldKeys ARE the Field-PWA contract — the marker is the naming convention (`gnss_`
 * prefix + these keys), not a schema tag. Never required, no defaults; units are meters/seconds
 * (receiver-native), NOT feet. `gnss_fix_type`/`gnss_fix_time` are TEXT by decision: fix type stores
 * what the source emits (no domain), fix time is full ISO-8601 UTC (the custom `date` type is
 * day-granularity only).
 */
export const GNSS_BUNDLE: readonly Omit<CustomFieldCreate, 'sortOrder'>[] = [
  { fieldKey: 'gnss_h_accuracy', label: 'Horizontal accuracy (m)', dataType: 'number', numericSubtype: 'double' },
  { fieldKey: 'gnss_v_accuracy', label: 'Vertical accuracy (m)', dataType: 'number', numericSubtype: 'double' },
  { fieldKey: 'gnss_altitude', label: 'Altitude (m)', dataType: 'number', numericSubtype: 'double' },
  { fieldKey: 'gnss_fix_type', label: 'Fix type', dataType: 'text' },
  // Label was 'Fix time (UTC)'. The portal now shows a timestamp in the viewer's local time, with the
  // stored UTC value as the tooltip, so the label no longer names a zone.
  // The STORED value is still ISO-8601 UTC.
  // Layers made before this keep their own label: a label is per-layer data.
  { fieldKey: 'gnss_fix_time', label: 'Fix time', dataType: 'text' },
  // 0–99 mirrors Esri's ESRI_NUM_SATS_DOMAIN range; no legitimate receiver value violates it.
  { fieldKey: 'gnss_num_sats', label: 'Satellites', dataType: 'number', numericSubtype: 'short_integer', constraints: { min: 0, max: 99 } },
  { fieldKey: 'gnss_pdop', label: 'PDOP', dataType: 'number', numericSubtype: 'double' },
  { fieldKey: 'gnss_hdop', label: 'HDOP', dataType: 'number', numericSubtype: 'double' },
  { fieldKey: 'gnss_vdop', label: 'VDOP', dataType: 'number', numericSubtype: 'double' },
  { fieldKey: 'gnss_correction_age', label: 'Correction age (s)', dataType: 'number', numericSubtype: 'double' },
  { fieldKey: 'gnss_station_id', label: 'Station ID', dataType: 'text' },
  { fieldKey: 'gnss_receiver', label: 'Receiver', dataType: 'text' },
]

/** The bundle's keys — membership test for skip-existing and the wizard's uncheck-removal. */
export const GNSS_FIELD_KEYS: ReadonlySet<string> = new Set(GNSS_BUNDLE.map((f) => f.fieldKey))

/** The bundle as create payloads with sortOrder assigned from `start`, stepping by 10 (house spacing). */
export function gnssBundleDrafts(start: number): CustomFieldCreate[] {
  return GNSS_BUNDLE.map((f, i) => ({ ...f, sortOrder: start + i * 10 }))
}
