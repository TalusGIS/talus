import { previewImportFields } from '@/lib/api/client'
import type { FeatureImportPayload } from '@/lib/api/client'
import type { CustomFieldCreate, PreviewImportResult } from '@/lib/api/types'

/**
 * Add-from-file plumbing (decision-add-from-file.md). One picked file drives BOTH halves of the flow:
 * `prepareFile` detects the format, runs the schema preview (a client-cut SAMPLE for the formats the
 * browser can parse — so a 235 MB GeoJSON previews instantly), and RETAINS what the post-Finish ingest
 * needs; `buildIngestPayloads` then turns that into one-or-many /features/import requests (GeoJSON/CSV
 * chunk client-side below the per-request cap; a zipped shapefile/gpkg can't be split, so it goes whole
 * and the panel rejects it honestly when it exceeds the cap).
 */

/**
 * A refusal whose way forward is Catalog → Import data — the streamed, background door that takes
 * files up to 2 GB. The Add layers panel shows these with an **Open Import data** button instead of
 * leaving the user to find the other door from a sentence.
 */
export class UseImportDataError extends Error {}

export type AddFileFormat = 'shapefile' | 'geopackage' | 'geojson' | 'csv-data'

export const ADD_FILE_ACCEPT = '.zip,.gpkg,.geojson,.json,.csv'
export const MAX_FILE_MB = 50 // mirrors the api's file_ingest.MAX_FILE_BYTES (per REQUEST — chunked formats are unbounded per file)
const MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
const PREVIEW_SAMPLE = 1000 // features/rows sent to the schema preview for the client-parseable formats
const INGEST_CHUNK = 10000 // features per /features/import request when chunking (geojson/csv)

/** What the wizard needs to open pre-filled (built from the preview + the file itself). */
export interface WizardFileSeed {
  fileName: string
  format: AddFileFormat
  geometry: 'point' | 'line' | 'polygon' | null
  detectedSrid: number | null
  fields: CustomFieldCreate[]
  featureCount: number | null
  columns?: string[]
  xColumn?: string | null
  yColumn?: string | null
}

/** What the wizard's Finish hands back so the ingest matches the user's choices. */
export interface SeedIngestContext {
  sourceSrid?: number
  xColumn?: string
  yColumn?: string
}

/** A picked file, previewed and held for ingest. Exactly one of the retained fields is set, by format. */
export interface PreparedFile {
  format: AddFileFormat
  fileName: string
  featureCount: number | null
  featureCap: number | null
  preview: PreviewImportResult
  payloadBase64?: string // shapefile | geopackage — the whole file, sent once
  geojsonFeatures?: unknown[] // geojson — parsed once, chunked at ingest
  csvHeader?: string // csv-data — header + rows kept split (quote-aware) for chunking
  csvRows?: string[]
}

export function detectFileFormat(name: string): AddFileFormat | null {
  const n = name.toLowerCase()
  if (n.endsWith('.zip')) return 'shapefile'
  if (n.endsWith('.gpkg')) return 'geopackage'
  if (n.endsWith('.geojson') || n.endsWith('.json')) return 'geojson'
  if (n.endsWith('.csv')) return 'csv-data'
  return null
}

/** Blob/string → raw base64 (no data: prefix). Chunk-safe for big files (no arg-spread btoa). */
function toBase64(data: Blob | string): Promise<string> {
  const blob = typeof data === 'string' ? new Blob([data]) : data
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const res = reader.result as string
      resolve(res.includes(',') ? res.slice(res.indexOf(',') + 1) : res)
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

/** Split CSV text into rows on newlines OUTSIDE double quotes (a quoted cell may contain \n).
 *  Escaped quotes ("") toggle the state twice — net unchanged. Trailing \r is trimmed (CRLF files). */
export function splitCsvRows(text: string): string[] {
  const rows: string[] = []
  let start = 0
  let inQuotes = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (ch === '"') inQuotes = !inQuotes
    else if (ch === '\n' && !inQuotes) {
      let end = i
      if (end > start && text[end - 1] === '\r') end--
      rows.push(text.slice(start, end))
      start = i + 1
    }
  }
  if (start < text.length) {
    let end = text.length
    if (text[end - 1] === '\r') end--
    if (end > start) rows.push(text.slice(start, end))
  }
  return rows
}

/** Detect + preview a picked file; retain what the ingest needs. Throws Error with a user-facing message. */
export async function prepareFile(file: File, accessToken: string): Promise<PreparedFile> {
  const format = detectFileFormat(file.name)
  if (format === null) {
    throw new Error('Unsupported file type — use a zipped Shapefile (.zip), GeoPackage (.gpkg), GeoJSON, or CSV')
  }

  if (format === 'shapefile' || format === 'geopackage') {
    // Sent whole (the browser can't split these) — the size cap is per file.
    if (file.size > MAX_FILE_BYTES) {
      // MiB, honestly labeled (the backlog's MB-vs-MiB rider): the math is binary, so is the unit.
      throw new UseImportDataError(`This file is ${Math.round(file.size / (1024 * 1024))} MiB. Add from file takes a Shapefile or GeoPackage up to ${MAX_FILE_MB} MiB, because it is read in one piece while you wait. Import data in the Catalog takes files up to 2 GB.`)
    }
    const payloadBase64 = await toBase64(file)
    const preview = await previewImportFields({ format, payload_base64: payloadBase64 }, accessToken)
    return { format, fileName: file.name, featureCount: preview.featureCount ?? null, featureCap: preview.featureCap ?? null, preview, payloadBase64 }
  }

  if (format === 'geojson') {
    let doc: unknown
    try {
      doc = JSON.parse(await file.text())
    } catch {
      // The old message ('Not valid JSON') lied for county-scale files: the browser's ~512 MiB
      // string ceiling — OUR limit, not the file's validity — is the usual culprit (backlog 9d
      // fence 3). Say which failure this actually is.
      if (file.size > 256 * 1024 * 1024) {
        throw new UseImportDataError(
          `This GeoJSON (${Math.round(file.size / (1024 * 1024))} MiB) is beyond what the browser can parse — ` +
          'the file may be perfectly valid. Import data in the Catalog reads it on the server.',
        )
      }
      throw new Error('The file could not be parsed as JSON — check that it is valid GeoJSON')
    }
    const d = doc as { type?: string; features?: unknown[] }
    const features = d?.type === 'FeatureCollection' && Array.isArray(d.features) ? d.features : d?.type === 'Feature' ? [doc] : null
    if (features === null) throw new Error('Expected a GeoJSON FeatureCollection or Feature')
    // Preview from a sample — the wizard only needs the schema + geometry family; the REAL count is ours.
    const sample = JSON.stringify({ type: 'FeatureCollection', features: features.slice(0, PREVIEW_SAMPLE) })
    const preview = await previewImportFields({ format, payload_base64: await toBase64(sample) }, accessToken)
    return { format, fileName: file.name, featureCount: features.length, featureCap: preview.featureCap ?? null, preview, geojsonFeatures: features }
  }

  // csv-data — rows split once (quote-aware) and kept for chunking; preview from a sample.
  const rows = splitCsvRows(await file.text())
  if (rows.length < 2) throw new Error('The CSV has no data rows')
  const header = rows[0]
  const dataRows = rows.slice(1)
  const sampleText = [header, ...dataRows.slice(0, PREVIEW_SAMPLE)].join('\n')
  const preview = await previewImportFields({ format: 'csv-data', csv_text: sampleText }, accessToken)
  return { format, fileName: file.name, featureCount: dataRows.length, featureCap: preview.featureCap ?? null, preview, csvHeader: header, csvRows: dataRows }
}

/** The wizard seed for a prepared file (real client-side count wins over the sample-based preview count). */
export function seedFromPrepared(prep: PreparedFile): WizardFileSeed {
  return {
    fileName: prep.fileName,
    format: prep.format,
    geometry: prep.format === 'csv-data' ? 'point' : (prep.preview.geometry ?? null),
    detectedSrid: prep.preview.detectedSrid ?? null,
    fields: prep.preview.fields,
    featureCount: prep.featureCount,
    columns: prep.preview.columns,
    xColumn: prep.preview.xColumn,
    yColumn: prep.preview.yColumn,
  }
}

/** One-or-many /features/import payloads: whole-file for shapefile/gpkg, 10k-feature chunks for geojson/csv.
 *  CSV chunks keep every column — the server maps the coordinate pair via csv_x_column/csv_y_column.
 *  sourceSrid rides on the formats that may need a user-declared CRS (shapefile w/ codeless .prj, csv). */
export function buildIngestPayloads(prep: PreparedFile, ctx?: SeedIngestContext): FeatureImportPayload[] {
  if (prep.format === 'shapefile') {
    return [{ format: 'shapefile', shp_base64: prep.payloadBase64 ?? '', source_srid: ctx?.sourceSrid }]
  }
  if (prep.format === 'geopackage') {
    // source_srid only matters for a gpkg whose srs_id is unusable (0/-1) — the user then declared one.
    return [{ format: 'geopackage', gpkg_base64: prep.payloadBase64 ?? '', source_srid: ctx?.sourceSrid }]
  }
  if (prep.format === 'geojson') {
    const feats = prep.geojsonFeatures ?? []
    const out: FeatureImportPayload[] = []
    for (let i = 0; i < feats.length; i += INGEST_CHUNK) {
      out.push({ format: 'geojson', geojson_text: JSON.stringify({ type: 'FeatureCollection', features: feats.slice(i, i + INGEST_CHUNK) }) })
    }
    return out
  }
  const rows = prep.csvRows ?? []
  const out: FeatureImportPayload[] = []
  for (let i = 0; i < rows.length; i += INGEST_CHUNK) {
    out.push({
      format: 'csv',
      csv_text: [prep.csvHeader ?? '', ...rows.slice(i, i + INGEST_CHUNK)].join('\n'),
      source_srid: ctx?.sourceSrid,
      csv_x_column: ctx?.xColumn,
      csv_y_column: ctx?.yColumn,
    })
  }
  return out
}
