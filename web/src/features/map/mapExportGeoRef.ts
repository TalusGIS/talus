// Georeferencing for the Print (PDF) export — decision-geospatial-pdf-export.md.
//
// Makes the one-page PDF an ISO 32000-2 §12.9.3 geospatial PDF (the QGIS / GDAL / Avenza Maps /
// Acrobat flavor, not TerraGo): a /VP viewport over the map image's page rectangle carrying a
// /Measure /GEO dictionary — four ground corners (/GPTS, latitude then longitude), their unit-square
// positions (/LPTS), and the render CRS (/GCS, EPSG:3857 as WKT). Metadata only: the page draws the
// same. The shape mirrors GDAL's writer (frmts/pdf/pdfcreatecopy.cpp, WriteSRS_ISO32000).
//
// Written through jsPDF's own plugin surface — the per-document `putPage` event plus
// `internal.write` / `internal.newAdditionalObject`, the trio its annotations plugin uses for
// /Annots — so no second PDF library is needed. `internal.*` is jsPDF's plugin API (stable v1–v4),
// not its public document API; mapExportGeoRef.test.ts reads the bytes back and is the guard on a
// jsPDF bump. Dependency-free on purpose (no maplibre import) so the test runs under plain node.

import type { jsPDF } from 'jspdf'

/** A ground position, degrees. */
export interface LngLatLike {
  lng: number
  lat: number
}

/** The map image's four ground corners. */
export interface GroundCorners {
  ul: LngLatLike
  ll: LngLatLike
  lr: LngLatLike
  ur: LngLatLike
}

/** The map image's rectangle on the page in the document's units (jsPDF 'in' here), top-left origin —
 *  exactly the arguments handed to `doc.addImage`. */
export interface PageRect {
  x: number
  y: number
  w: number
  h: number
}

/** EPSG:3857 as the ESRI-flavor WKT GDAL writes into /GCS (OSRMorphToESRI). Readers that honour it fit
 *  the corners exactly in Web Mercator; readers that ignore it fit lat/lon linearly, which is within a
 *  metre at municipal scale. Contains no parentheses or backslashes, so it is a plain PDF literal. */
export const WEB_MERCATOR_WKT =
  'PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",' +
  'SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],' +
  'PROJECTION["Mercator_Auxiliary_Sphere"],PARAMETER["False_Easting",0.0],PARAMETER["False_Northing",0.0],' +
  'PARAMETER["Central_Meridian",0.0],PARAMETER["Standard_Parallel_1",0.0],PARAMETER["Auxiliary_Sphere_Type",0.0],' +
  'UNIT["Meter",1.0]]'

/** The unit square's corners in the order the ground corners are written: UL, LL, LR, UR. */
const UNIT_SQUARE = '0 1 0 0 1 0 1 1'

// The parts of jsPDF's plugin surface this module uses; `internal` is only partially typed upstream.
interface PluginInternals {
  scaleFactor: number
  pageSize: { getHeight(): number }
  events: { subscribe(topic: string, fn: () => void): string }
  write(s: string): void
  newAdditionalObject(): { objId: number; content: string }
  pdfEscape(s: string): string
}

/** Ground corners from a map's unproject function and its canvas size in CSS pixels. Order UL, LL, LR,
 *  UR — the order /GPTS is written in. Exact under any bearing (the registration is four points, not a
 *  north-up box). */
export function groundCorners(
  unproject: (x: number, y: number) => LngLatLike,
  width: number,
  height: number,
): GroundCorners {
  return {
    ul: unproject(0, 0),
    ll: unproject(0, height),
    lr: unproject(width, height),
    ur: unproject(width, 0),
  }
}

/** Register `rect` (the map image's page rectangle) as a geospatial viewport whose corners are
 *  `corners`. Call once per document, before `doc.save`/`doc.output`; the dictionaries are written when
 *  jsPDF serialises the page. */
export function registerGeospatialViewport(doc: jsPDF, rect: PageRect, corners: GroundCorners): void {
  const internal = doc.internal as unknown as PluginInternals
  const k = internal.scaleFactor
  const pageH = internal.pageSize.getHeight()
  // PDF user space: points, origin bottom-left. jsPDF's units are top-left origin.
  const x0 = pt(rect.x * k)
  const x1 = pt((rect.x + rect.w) * k)
  const y0 = pt((pageH - rect.y - rect.h) * k)
  const y1 = pt((pageH - rect.y) * k)
  const gpts = [corners.ul, corners.ll, corners.lr, corners.ur].map((c) => `${deg(c.lat)} ${deg(c.lng)}`).join(' ')

  internal.events.subscribe('putPage', () => {
    const viewport = internal.newAdditionalObject()
    const measure = internal.newAdditionalObject()
    const gcs = internal.newAdditionalObject()
    gcs.content = `<< /Type /PROJCS /EPSG 3857 /WKT (${internal.pdfEscape(WEB_MERCATOR_WKT)}) >>`
    measure.content =
      `<< /Type /Measure /Subtype /GEO /Bounds [${UNIT_SQUARE}] /GPTS [${gpts}] /LPTS [${UNIT_SQUARE}] ` +
      `/GCS ${gcs.objId} 0 R >>`
    viewport.content = `<< /Type /Viewport /Name (Map) /BBox [${x0} ${y0} ${x1} ${y1}] /Measure ${measure.objId} 0 R >>`
    internal.write(`/VP [${viewport.objId} 0 R]`)
  })
}

/** Points to 1/10,000 pt (≈ 3.5 µm) — exact enough, and no exponent notation. */
function pt(v: number): string {
  return trim(v.toFixed(4))
}

/** Degrees to 1e-8 (≈ 1 mm). */
function deg(v: number): string {
  return trim(v.toFixed(8))
}

function trim(s: string): string {
  return s.includes('.') ? s.replace(/0+$/, '').replace(/\.$/, '') : s
}
