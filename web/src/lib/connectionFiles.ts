/**
 * Deployment-level desktop connection files (Interop I-4 lean,
 * docs/decision-interop-i4-connection-files.md).
 *
 * ONE credential-free file per deployment: it carries only the SIGN-IN door URL + the proven
 * client settings. Identity enters at first use (the client prompts for talus + the user's own
 * PAT and stores it locally); scope is computed per request server-side — so the same static
 * file serves the whole org, and two users importing it see different collections lists.
 * NEVER put a credential in these files (QGIS inline connection creds land in PLAINTEXT ini;
 * a Pro connection file can embed them too — both are exactly the .sde wart we're avoiding).
 */

/** XML attribute escaping (values are org-controlled — the advertised URL / org name — but
 *  escape anyway so a quote or ampersand can't break the document). */
function xmlAttr(v: string): string {
  return v
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * The QGIS connections-exchange XML (Data Source Manager ▸ WFS / OGC API — Features ▸ Load).
 * Schema verified against qgsmanageconnectionsdialog.cpp (root `qgsWFSConnections`, one `wfs`
 * element, import checks only the root tag); values mirror the settings of the connection that
 * passed the QGIS 3.44 rig acceptance: Version pinned to OGC API — Features (the "Maximum"
 * auto-probe trap), max features EMPTY (the silent-truncation trap), server paging at 1000.
 */
export function buildQgisConnectionsXml(connectionName: string, ogcAuthUrl: string): string {
  const name = xmlAttr(connectionName)
  const url = xmlAttr(ogcAuthUrl)
  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<qgsWFSConnections version="1.1">\n` +
    `  <wfs name="${name}" url="${url}" version="OGC_API_FEATURES" maxnumfeatures=""` +
    ` pagesize="1000" pagingenabled="enabled" ignoreAxisOrientation="false"` +
    ` invertAxisOrientation="false" username="" password="" httpMethod="Get"` +
    ` featureMode="default"/>\n` +
    `</qgsWFSConnections>\n`
  )
}

/**
 * The QGIS WMS/WMTS connections-exchange XML (Data Source Manager ▸ WMS/WMTS ▸ Load) — Interop
 * I-2. Schema verified against qgsmanageconnectionsdialog.cpp `saveOWSConnections` (root
 * `qgsWMSConnections` v1.0, one `<wms>` element — WMTS shares the WMS connection tree; import
 * checks only the root tag + reads `name`). Credential-free (username/password empty — QGIS stores
 * inline WMS creds in PLAINTEXT ini, so a distributed file must never carry them; the user enters
 * a PAT on first connect). `dpiMode="7"` is QGIS's "all" default; the boolean flags are off. The
 * WMTS layer + matrix set are chosen in the Add-Layer dialog after Connect, not in the connection.
 */
export function buildQgisWmsConnectionsXml(connectionName: string, capabilitiesUrl: string): string {
  const name = xmlAttr(connectionName)
  const url = xmlAttr(capabilitiesUrl)
  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<!DOCTYPE connections>\n` +
    `<qgsWMSConnections version="1.0">\n` +
    `  <wms name="${name}" url="${url}" ignoreGetMapURI="false"` +
    ` ignoreGetFeatureInfoURI="false" ignoreAxisOrientation="false"` +
    ` invertAxisOrientation="false" smoothPixmapTransform="false" dpiMode="7"` +
    ` username="" password=""/>\n` +
    `</qgsWMSConnections>\n`
  )
}

/**
 * The QGIS WCS connections-exchange XML (Data Source Manager ▸ WCS ▸ Load) — Interop I-6.
 * Schema verified against qgsmanageconnectionsdialog.cpp `saveOWSConnections` (root
 * `qgsWCSConnections`, one `<wcs>` element; import checks only the root tag; WCS connections
 * carry only name/url/username/password — the dpiMode/ignore* extras are WMS-only). Credential-
 * free like the others (the user enters a PAT on first connect). `url` is the WCS 1.0.0 KVP base
 * (the sign-in door) — the provider appends SERVICE/REQUEST itself.
 */
export function buildQgisWcsConnectionsXml(connectionName: string, wcsAuthUrl: string): string {
  const name = xmlAttr(connectionName)
  const url = xmlAttr(wcsAuthUrl)
  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<!DOCTYPE connections>\n` +
    `<qgsWCSConnections version="1.0">\n` +
    `  <wcs name="${name}" url="${url}" username="" password=""/>\n` +
    `</qgsWCSConnections>\n`
  )
}

// --- ArcGIS Pro `.ogc` server-connection file ---------------------------------------------
//
// The format was reverse-engineered from a REAL Pro-3.7-written file (captured off the rig's
// 2026-07-05 acceptance project) and the generator below reproduces that capture BYTE-IDENTICALLY
// given the same inputs — the format is understood, not guessed. Structure: an OLE Compound File
// (CFB v3) holding one stream, `OGCAPIConnectionProperties` = an ESRI-persisted PropertySet:
//   u16 1 · BSTR display-name · GUID(OGC connection class) · u16 2 · GUID(esriSystem.PropertySet)
//   · u16 1 · u32 count · count × [BSTR name · VARIANT value]
// BSTR = u32 byte-length (incl. the UTF-16 NUL) + UTF-16LE chars + NUL. VARIANTs used: VT_BSTR(8),
// VT_BOOL(11), VT_EMPTY(0), VT_UNKNOWN(13) carrying an empty nested PropertySet.
// Credential-free by construction: USER + AUTHENTICATION_INFO stay empty (Pro prompts on first
// use and stores in Windows Credential Manager, keyed to the server URL).

// The two Pro connection-class GUIDs, each byte-matched against a real Pro-written file:
//  - OGC API Server (.ogc, captured 2026-07-05) → OGCAPIConnectionProperties stream.
//  - WMTS Server    (.wmts, captured 2026-07-06) → WMTSConnectionProperties stream, VERSION "1.0.0".
// Otherwise the container + property layout are identical (same CFB, same PropertySet, same 12
// properties in the same order) — so ONE builder covers both, parameterized by class + stream name.
const _GUID_OGC_CONN = [0x83, 0x30, 0xf0, 0x1c, 0x9b, 0x31, 0xd7, 0x4d, 0x98, 0x8b, 0xcf, 0x12, 0xb8, 0x82, 0xf0, 0xd1]
const _GUID_WMTS_CONN = [0xbb, 0x61, 0xb0, 0x93, 0x92, 0xdb, 0x90, 0x4e, 0x99, 0x27, 0x48, 0xc0, 0x31, 0xb3, 0x3f, 0xe8]
const _GUID_PROPSET = [0x11, 0x5a, 0x8e, 0x58, 0x9b, 0xd0, 0xd1, 0x11, 0xaa, 0x7c, 0x00, 0xc0, 0x4f, 0xa3, 0x3a, 0x15]

class _Buf {
  bytes: number[] = []
  u16(v: number): void {
    this.bytes.push(v & 0xff, (v >> 8) & 0xff)
  }
  u32(v: number): void {
    this.bytes.push(v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff)
  }
  raw(b: number[]): void {
    this.bytes.push(...b)
  }
  /** BSTR: u32 byte-length (incl. NUL) + UTF-16LE + NUL. JS strings are UTF-16 natively. */
  bstr(s: string): void {
    this.u32((s.length + 1) * 2)
    for (let i = 0; i < s.length; i++) this.u16(s.charCodeAt(i))
    this.u16(0)
  }
}

function _buildConnStream(
  displayName: string, url: string, classGuid: number[], version: string,
): number[] {
  const b = new _Buf()
  b.u16(1)
  b.bstr(displayName)
  b.raw(classGuid)
  b.u16(2)
  b.raw(_GUID_PROPSET)
  b.u16(1)
  b.u32(12)
  const str = (name: string, val: string) => {
    b.bstr(name); b.u16(8); b.bstr(val)
  }
  const bool = (name: string, val: boolean) => {
    b.bstr(name); b.u16(11); b.u16(val ? 0xffff : 0)
  }
  str('URL', url)
  str('LAYERNAME', '')
  str('USER', '')
  bool('HIDEUSERPROPERTY', true)
  str('VERSION', version) // '' for OGC API; '1.0.0' for WMTS (both byte-proven)
  bool('SUPPORTS_CUSTOM_AUTHENTICATION', true)
  // Pro records the file's own save path here; a minted file has no meaningful path — empty,
  // like the other unset BSTRs (Pro is expected to manage it once the connection is used). Left
  // empty in both captures' generators; flagged in the WMTS capture report as the one value to
  // re-check if a minted file ever misbehaves (the local download path won't match the original).
  str('CONNECTIONPATH', '')
  b.bstr('CAPABILITIES_PARAMETERS'); b.u16(13); b.raw(_GUID_PROPSET); b.u16(1); b.u32(0)
  b.bstr('MAP_PARAMETERS'); b.u16(0)
  str('TEMPLATE_URL', '')
  bool('ANONYMOUSSOURCE', false)
  str('AUTHENTICATION_INFO', '')
  return b.bytes
}

/** Wrap the stream in a minimal CFB v3: header | FAT | directory | MiniFAT | ministream. The
 *  stream rides the ministream (64-byte mini sectors); timestamps stay zero (valid per spec,
 *  and deterministic). Layout mirrors the captured file exactly. */
function _buildCfb(stream: number[], streamName: string): Uint8Array {
  const nMini = Math.ceil(stream.length / 64)
  const miniBytes = nMini * 64
  const nSect = Math.max(1, Math.ceil(miniBytes / 512))
  const total = 512 * (4 + nSect)
  const out = new Uint8Array(total)
  const dv = new DataView(out.buffer)

  // header
  out.set([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1], 0)
  dv.setUint16(24, 0x003e, true) // minor version
  dv.setUint16(26, 0x0003, true) // major version 3
  dv.setUint16(28, 0xfffe, true) // little-endian
  dv.setUint16(30, 9, true) // 512-byte sectors
  dv.setUint16(32, 6, true) // 64-byte mini sectors
  dv.setUint32(44, 1, true) // FAT sector count
  dv.setUint32(48, 1, true) // first directory sector
  dv.setUint32(56, 0x1000, true) // mini-stream cutoff
  dv.setInt32(60, 2, true) // first MiniFAT sector
  dv.setUint32(64, 1, true) // MiniFAT sector count
  dv.setInt32(68, -2, true) // no DIFAT chain
  dv.setInt32(76, 0, true) // DIFAT[0] = FAT at sector 0
  for (let off = 80; off < 512; off += 4) dv.setInt32(off, -1, true)

  // FAT (sector 0 @512): s0=FATSECT, s1(dir)=END, s2(minifat)=END, ministream chain s3..
  const fatBase = 512
  const chain = [-3, -2, -2]
  for (let i = 0; i < nSect - 1; i++) chain.push(3 + i + 1)
  chain.push(-2)
  chain.forEach((v, i) => dv.setInt32(fatBase + i * 4, v, true))
  for (let off = fatBase + chain.length * 4; off < fatBase + 512; off += 4) dv.setInt32(off, -1, true)

  // directory (sector 1 @1024): Root + the stream + two unused entries
  const dirent = (
    idx: number, name: string, type: number, color: number,
    child: number, start: number, size: number,
  ) => {
    const base = 1024 + idx * 128
    if (name) {
      for (let i = 0; i < name.length; i++) dv.setUint16(base + i * 2, name.charCodeAt(i), true)
      dv.setUint16(base + 64, (name.length + 1) * 2, true)
    }
    dv.setUint8(base + 66, type)
    dv.setUint8(base + 67, color)
    dv.setInt32(base + 68, -1, true) // left sibling
    dv.setInt32(base + 72, -1, true) // right sibling
    dv.setInt32(base + 76, child, true)
    dv.setInt32(base + 116, start, true)
    dv.setUint32(base + 120, size, true)
  }
  dirent(0, 'Root Entry', 5, 0, 1, 3, miniBytes)
  dirent(1, streamName, 2, 1, -1, 0, stream.length)
  dirent(2, '', 0, 0, -1, 0, 0)
  dirent(3, '', 0, 0, -1, 0, 0)

  // MiniFAT (sector 2 @1536): the stream's mini-sector chain
  const mfBase = 1536
  for (let i = 0; i < nMini - 1; i++) dv.setInt32(mfBase + i * 4, i + 1, true)
  dv.setInt32(mfBase + (nMini - 1) * 4, -2, true)
  for (let off = mfBase + nMini * 4; off < mfBase + 512; off += 4) dv.setInt32(off, -1, true)

  // ministream (sector 3+ @2048)
  out.set(stream, 2048)
  return out
}

/** The ArcGIS Pro OGC API server-connection file (`.ogc`): double-click (or add via Catalog) →
 *  the connection appears; Pro prompts for credentials on first use and stores them in Credential
 *  Manager. Byte-matched to a real Pro 3.7 capture. */
export function buildProOgcFile(displayName: string, ogcAuthUrl: string): Uint8Array {
  return _buildCfb(
    _buildConnStream(displayName, ogcAuthUrl, _GUID_OGC_CONN, ''),
    'OGCAPIConnectionProperties',
  )
}

/** The ArcGIS Pro WMTS server-connection file (`.wmts`): same container as the `.ogc`, WMTS class
 *  GUID + VERSION "1.0.0". `capabilitiesUrl` is the WMTS 1.0.0 capabilities URL (sign-in door).
 *  Byte-matched to a real Pro 3.7 capture (2026-07-06). */
export function buildProWmtsFile(displayName: string, capabilitiesUrl: string): Uint8Array {
  return _buildCfb(
    _buildConnStream(displayName, capabilitiesUrl, _GUID_WMTS_CONN, '1.0.0'),
    'WMTSConnectionProperties',
  )
}

/** Save generated content as a download (the export-blob pattern from downloadLayerExport). */
export function downloadTextFile(
  filename: string,
  content: string | Uint8Array,
  mime = 'application/xml',
): void {
  const url = URL.createObjectURL(new Blob([content as BlobPart], { type: mime }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
