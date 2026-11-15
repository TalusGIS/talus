# Connecting desktop GIS and field devices to Talus GIS

The support reference behind the portal's **"Use in…"** assistant (catalog row 🔌 / Manage →
Use in…) and the Profile → **Connect this computer** page. **Part 1** covers desktop GIS: every
recipe was proven with **QGIS 3.44** and **ArcGIS Pro 3.7**. **Part 2** covers field collection with
QField, proven with **QField 4.2** (iOS 4.2.4, Android 4.2.11) and **QFieldSync 4.22**. QField is
upstream software; re-check the field recipes when you move to a newer version.

---

# Part 1 — Desktop GIS (QGIS and ArcGIS Pro)

## The model in one paragraph

Every hosted layer you can see is automatically its own **FeatureServer** (Esri REST) service,
and every spatial layer you can see — hosted or view — is an **OGC API — Features** collection;
there is no publish step and no service registry. (Tables ride FeatureServer only; view layers ride
OGC only and are read-only today.) Access is governed
by the exact same sharing model as the portal: you connect with a **personal access token**
(Profile → Access tokens; shown once at mint) as the HTTP Basic **password** (username is
anything, e.g. `talus`), and see precisely the layers you can see in the portal. Viewing rides
either dialect; **editing rides the FeatureServer door** (below) with a
**Read & edit** token.

## The two doors — point the right client at the right URL

| Door | Path | Behaviour | Who it's for |
|---|---|---|---|
| **SIGN-IN** | `/api/v1/ogc-auth` | Challenges an unauthenticated request (`401 + WWW-Authenticate: Basic`) | **Every signed-in desktop client — ArcGIS Pro AND QGIS** |
| **PUBLIC** | `/api/v1/ogc` | Never challenges; anonymous callers get public layers | Open-data harvesters, anonymous scripts |

Why two: Pro never volunteers Basic credentials — it waits for a challenge, so on the PUBLIC
door it silently shows an **empty server**. QGIS volunteers credentials on every request, so it
works on either — but the SIGN-IN door is the one recipe that works for every signed-in client
and always shows full scope. The client stays on the door it dialed (links are built per-door).

The host must be reachable **from the desktop machine** — an admin sets the organization's
advertised base URL under Admin → Connections (e.g. `https://gis.example.gov`, or a LAN address on
a test rig).

## The fast path — the deployment connection file

Profile → **Connect this computer** offers a one-time connection file per deployment
(credential-free — it carries only the sign-in door URL and the proven client settings; scope is
computed per user at request time, so one file serves the whole organization):

- **QGIS**: download `talus-qgis-connection.xml` → Data Source Manager ▸ WFS / OGC API —
  Features ▸ **Load** ▸ Import. The connection arrives with the Version pinned and max-features
  blank (the two traps below pre-avoided); Connect and sign in with `talus` + your token.
- **ArcGIS Pro**: download the `.ogc` connection file → save it into your project's home folder
  (or any folder connection) → it appears in the Catalog pane; open it and sign in once with
  `talus` + your token (Pro stores it in Windows Credential Manager). The minted file is
  byte-compatible with what Pro itself writes, minus any credential.

**Credential hygiene:** never distribute a connection file with credentials saved into it. QGIS
stores inline connection username/password in **plaintext** settings (and includes them in its
own "Save connections" export!) — prefer a QGIS *authentication configuration* (encrypted) for
the token, and in Pro choose **Windows Credential Manager**, not "save in connection file".

## QGIS (3.44) — manual setup

Layer ▸ Data Source Manager ▸ **WFS / OGC API — Features** ▸ New:

1. **URL**: `<advertised base>/api/v1/ogc-auth`
2. **Version**: **"OGC API — Features"** — explicitly. The default ("Maximum") auto-probes
   classic WFS and dies with *"error occurred while parsing element"*.
3. **Authentication**: Basic — username `talus` (anything), password = your access token.
4. **Max. number of features**: **leave BLANK**. A value silently truncates the layer,
   sometimes with a "failed or partially failed" banner. Feature paging can stay default.
5. Connect → the collections list shows your layers → add.

## ArcGIS Pro (3.7)

Insert ▸ Connections ▸ Server ▸ **New OGC API Server**:

1. **Server URL**: `<advertised base>/api/v1/ogc-auth` — the SIGN-IN door. On the PUBLIC door
   Pro shows an empty server (it only authenticates after a challenge).
2. **Authentication**: Server Authentication — username `talus`, password = your access token.
3. Add the connection; add layers from the Catalog pane.

### Don't use "New ArcGIS Server Connection"

Pro's **New ArcGIS Server Connection** dialog does not work with Talus and never will — it is a
**documented limitation**, decided deliberately. The dialog
validates a server by Esri's proprietary **Admin API** (`admin/generateToken`,
`POST <site>/services`) *before* it ever reads the REST surface, so it fails with

> *We were unable to connect to: `<your URL>`.*

**even when your credentials are perfectly valid** — this is not an authentication problem
(wire-verified: the same credentials authenticate 401→200 on `/rest/services` mid-attempt, and the
dialog fails identically with no credentials at all). Talus declines to emulate the Admin API: it
would mean issuing Esri-style tokens beside the portal's own, for the sake of one browse dialog
whose jobs both have working routes:

| You want | Working route |
|---|---|
| **Browse / view** many layers | **New OGC API Server** (above), or the minted connection file (Profile → Connect this computer) |
| **Edit** a layer | Paste its FeatureServer link (layer's **Use in…** wizard) via **Add Data From Path** |
| **Imagery / live values** | **New WMTS Server** / **New WCS Server** with the sign-in URL — these dialogs work |

**The Service-type tell** (if you're diagnosing someone else's attempt): pasting the *services
root* into Add Data From Path leaves the Service type dropdown stuck on `Automatic` and fails
(*"Failed to create layer from …"*); pasting a **layer URL** resolves it to *"An ArcGIS Server Web
Service"* and adds silently. Layer URLs, not roots.

## Troubleshooting

- **Pro: changed the URL or credential, still seeing stale results?** Pro caches the
  collections listing and the session credential **in memory for the whole session** — fully
  **restart Pro**. Deleting and re-adding the connection is not enough.
- **Pro: wrong password seems to "work"?** Pro keys stored credentials to the **server URL**
  (Windows Credential Manager), not the connection item — a reconnect to the same URL silently
  reuses the previously stored good credential. The entry is a **Generic Credential** whose target
  is the door URL with a `mode` suffix, e.g. for the FeatureServer door:

  ```
  LegacyGeneric:target=https://<host>/api/v1/esri-auth&mode=0
  ```

  Find it with `cmdkey /list | findstr talus` (or Control Panel ▸ Credential Manager ▸ Windows
  Credentials). To re-authenticate cleanly you need **both steps, in this order**: exit Pro, delete
  the entry, then restart Pro. Deleting it while Pro is running does nothing — Pro holds the
  credential in process memory for the life of the session.
- **Pro: first connection attempt fails spuriously** — known Esri issue (BUG-000158228); just
  retry.
- **QGIS: "error occurred while parsing element"** — the Version dropdown is on "Maximum";
  set it to "OGC API — Features" (step 2).
- **QGIS: layer stops loading and stays broken until restart** — a once-interrupted download
  used to be replayed from Qt's disk cache; the server now sends `Cache-Control: no-store`
  specifically to prevent this. If you see it, update the server.
- **Token rejected (401)** — the token is revoked, expired, or mistyped. Tokens are shown once
  at mint; if it's lost, revoke it in Profile → Access tokens and mint a new one. Revocation is
  immediate — a live desktop connection dies on its next refresh (by design).
- **"This access token cannot see this layer" (re-prompt loop)** — you authenticated with a
  *valid* token, but its **account doesn't have access** to the layer you're opening (a common
  mix-up: a token minted under a different portal account than the one the layer is shared to —
  e.g. an admin's token, which by design never carries administrator rights). The server re-prompts (rather
  than dead-ending) so you can paste a token from an account that *can* see the layer — the fix
  is a different token, not a different password. Cancel the prompt to stop. (This is deliberate:
  a view-denied token gets an *invitation to re-authenticate* on the desktop doors, not a bare
  "forbidden" — the realm text names the reason.)
- **QGIS: connection shows no layers, no error, and refresh never re-prompts** — the credentials
  entered were wrong (most commonly the **portal password** — the password must be an *access
  token*, `tpat_…` — or a revoked/expired token), and after its retries Qt caches the failure
  for the whole session. **Restart QGIS** to get the prompt back, then sign in with a live token.
  (The credential dialog's realm text now says exactly this.)
- **Pro (WMTS): the connection is created, browses fine, and lists zero layers — with no error at
  all** — the token is wrong, truncated, revoked, or expired. WMTS Basic auth validates **only the
  password (the access token); the username is ignored entirely**, so any username with a live token
  works and any username with a dead one 401s — and Pro does not surface that 401. You get a server
  node that expands to nothing, which is indistinguishable from *"this server has no rasters."*
  Confirm from a shell before you touch the connection:

  ```
  curl -su talus:<token> '<base>/api/v1/wmts-auth/1.0.0/WMTSCapabilities.xml' | grep -c '<Layer>'
  ```

  A live token returns **1 or more**; a dead one returns **0**. The fix is a fresh token — deleting
  and re-adding the connection changes nothing. (Note the same zero-layer shape is *correct* and
  expected on the PUBLIC door with no token: anonymous callers see only publicly-shared rasters.)
- **Empty collections list in QGIS with no token** — anonymous callers on the PUBLIC door see
  only publicly shared layers; that's the sharing model working.

## Editing from desktop — the FeatureServer door

Every hosted layer is also a one-layer **GeoServices ("Esri REST") FeatureServer** — the ONLY
dialect both Pro and QGIS can EDIT through, and the one whose per-layer URL pastes anywhere
(the AGO copy-link habit):

| Door | Path | Behaviour |
|---|---|---|
| **SIGN-IN** | `/api/v1/esri-auth/rest/services/{layer_id}/FeatureServer` | Challenges (Pro's path); every signed-in client |
| **PUBLIC** | `/api/v1/esri/rest/services/{layer_id}/FeatureServer` | Never challenges; anonymous = public layers only |

**Two URL granularities, per client**: **Pro's Add Data From
Path takes the LAYER** (`…/FeatureServer/0`); **a QGIS ArcGIS-REST *connection* takes the
SERVICE** (`…/FeatureServer`, no `/0`) — QGIS's browser expands a service listing, and a `/0`
connection shows an empty tree with nothing addable. The wizard hands each client the right one.

The wizard's **Edit in Pro / Edit in QGIS** goals hand out the exact link + steps. What matters:

- **Token scope**: editing needs a **Read & edit** token (Profile → Access tokens, or minted
  inline in the wizard). Read-only tokens are refused for edits with a clear 403. A token is
  your *identity*, not a key to a layer — whether an edit lands is still decided by the layer's
  sharing (edit access), your role, and its editing toggle, exactly as in the portal, and every
  edit is attributed to you in the layer's audit history (client `geoservices-api`).
- **ArcGIS Pro**: Map ribbon ▸ Add Data ▸ **Data From Path** → paste the layer link (`…/0`) →
  sign in (`talus` + token) → edit with the Edit ribbon. Pro pages with `resultOffset` and saves
  through `applyEdits` — both served. **★ Do NOT use Insert ▸ Connections ▸ Server ▸ New OGC API
  Server**: that's the OGC-API read path — Pro can't edit OGC API Features
  and it can't parse a FeatureServer URL, so it fails with "Server landing page failed / Gateway
  Timeout." Add Data From Path is the editable path, one layer link at a time. (Pro's **New ArcGIS
  Server** dialog does not work with Talus: see "Don't use 'New ArcGIS Server Connection'" above.)
- **QGIS (3.44)**: Data Source Manager ▸ **ArcGIS REST Server** ▸ New → URL = the **service**
  link (no `/0`). **★ Authentication MUST be a saved *Configuration*, NOT the "Basic" tab.**
  This is the #1 editing trap: QGIS applies the Basic-tab
  username/password only to *read* requests; on an edit POST it sends **only** an auth
  Configuration. So the Basic tab lets the layer *draw* — then edits fail with a confusing
  "read-only" error (and if any stale token is cached for the host, QGIS may even replay THAT
  on the edit challenge). Fix: on the **Configurations** tab click ＋ ▸ Basic (`talus` + token)
  and select it — or, if you already typed into Basic, click the dialog's one-click **Convert
  to configuration**. A Basic Configuration is sent preemptively on every request (edits
  included), so it just works. Then: expand the connection → double-click the layer → Toggle
  Editing → add/move/edit/delete → Save (QGIS calls `addFeatures`/`updateFeatures`/
  `deleteFeatures` — all served).
- **Concurrency is last-write-wins between desktop editors** (as with ArcGIS Server without
  versioning). Every desktop write still bumps the row's version, so web-app editors keep their
  full stale-write protection; nothing is ever silently corrupted, and the audit trail records
  every write in order.
- **How fast edits show up for other viewers**: a desktop edit is durable in Talus the instant
  the save returns, and visible to any *new* fetch immediately — but Talus web maps are
  **pull-based, not live-push** (same as an ArcGIS feature layer in someone's open web map). An
  already-open map holds the tiles it already drew until it re-fetches, so another user sees the
  change on their **next refresh / pan / layer-toggle**, not instantly. For a **signed-in**
  viewer that's immediate on refresh (map tiles are served `no-cache`); for an **anonymous /
  public-layer** viewer there's an added **up-to-60-second** browser/CDN cache window (a
  deliberate cost/performance tradeoff for public tiles) before a refresh reflects the
  edit. There is no server push to open clients by design (a real-time-sync subsystem is against
  the operational-simplicity thesis and unneeded for the target users).
- **Editing OFF / read-only layers**: the service honestly advertises `Query`-only capabilities
  (desktop edit tools grey out) and refuses attempted edits with the reason. Hosted (`tal_…`)
  layers are the editable surface; a layer whose owner turned editing off is view-only through
  this door. View layers are not listed on this door today — read them through the OGC door and
  edit their source layer. Hosted **tables** (no geometry) are served as
  FeatureServer *tables* (view/query in v1).
- **Per-layer edit capabilities**: the owner's four flags —
  *Add features · Edit attributes · Move or reshape · Delete features* (`GET/PUT /layers/{id}/editing` →
  `editOps`) — shape the `capabilities` string token by token (`Create` ⇔ add, `Update` ⇔ attributes OR
  geometry, `Delete` ⇔ delete, `Editing` when any is on) and `allowGeometryUpdates` (⇔ move or reshape), so
  Pro and QGIS never draw a tool the layer refuses. A refused op inside `applyEdits` is a **per-result**
  `success:false` with code 403 and the policy sentence, never a top-level error; the native doors answer
  403 with the same sentence. No admin override — the flags are the owner's data policy.
- **Very large layers in QGIS**: QGIS loads the FULL object-id list at layer-open (its design;
  Pro is unaffected). Comfortable to ~1–2M features; a layer beyond that is better consumed in
  QGIS through the OGC door (which pages), or filtered server-side.
- **AGO web maps**: adding a Talus layer to an ArcGIS Online map requires an **https**
  deployment (AGO blocks plain http as mixed content) and a **publicly shared** layer through
  the PUBLIC door (the browser's Basic prompt is not usable in the AGO viewer). The façade
  already sends the CORS header AGO needs.

## Attachments — photos & documents on features (Pro + QGIS)

A hydrant photo, a permit PDF, an inspection shot on a parcel — attachments live in ONE place in
Talus (the `media.attachments` side table, bytes in the media store on the data volume) and every client holds a
reference. The **web app** (record panel) reaches them natively; **ArcGIS Pro** speaks the Esri
attachments protocol straight at the façade — the native **Attachments tab**, zero setup; **QGIS**
reaches them through its core **Attachment widget**, over a small feature-addressed upload door on
the same GeoServices façade you already edit through. Same token, same gates, same audit — an
attachment upload is authorized exactly like an edit (a **Read & edit** PAT; nothing new to grant).

**Turn it on first.** Attachments are **off by default per layer** (like ArcGIS). The owner enables
them in **Manage → Settings → Attachments**. Until then Pro's tab stays absent and the QGIS field
below simply won't appear.

### ArcGIS Pro — native, zero setup

Add the layer through the sign-in door with your **Read & edit** token (the same connection the
editing recipe mints) — Pro reads `hasAttachments` from the service and lights its native surfaces
up on its own:

1. Select a feature in an edit session — **Attributes pane ▸ Attachments** lists, opens, **adds**,
   updates, and removes attachments directly.
2. The **Add Attachments** geoprocessing tool covers batches (the same endpoints underneath).

Everything lands in the one Talus store the web app and QGIS also see, audited and attributed to
you. The same rules apply as everywhere: images, PDF, Office documents, plain text and CSV only, **25 MB** per file, up
to 50 per feature — and a read-only token, view-only sharing, editing off, or attachments off
refuse the write. On a layer you can *view* but not edit, the tab still shows and downloads
existing attachments (read and write are gated separately, exactly like features).

**Save sends updates and deletes.** *Adding* an attachment uploads the moment you pick the file —
but *updating or deleting* one is a staged edit like any other change in Pro: it reaches Talus
when you **save your edits** (Edit ribbon ▸ Save). Until you save, the tab shows your pending
local state, so don't judge by the list alone — save, then check.

### Bulk-loading with the Append tool — leave "Preserve Global IDs" UNCHECKED

Appending a whole feature class into a Talus layer works, **and it brings the attachments with it**:
tick **Environments ▸ Geodatabase ▸ Maintain Attachments** and Pro uploads each record's photos as
it goes. A File Geodatabase of roughly 2,800 features and 3,700 photos loaded this way in one operation.

**Do not tick "Preserve Global IDs"** (Environments ▸ Geodatabase Advanced). With it on, Append
fails before sending anything:

```
WARNING 000594: Input feature 1: Function not implemented.
ERROR 999999: Something unexpected caused the tool to fail.
Appended Row Count: 0
```

**This is not a Talus limitation.** The same source, same Pro, and same settings fail the same way
against a hosted **ArcGIS Online** feature layer — verified side by side with the same source and settings. The setting
requires the *source* to carry a genuine Esri **GlobalID** system field; a field merely *named*
`globalid` (Text or GUID) is not one, and Pro reports it per-feature in the way shown above rather
than saying so plainly. If your Field Map shows you mapping a source column onto the target's
`GlobalID`, that is the symptom — with a real GlobalID field Pro matches it itself and no mapping is
needed.

If you genuinely need the original GlobalIDs carried across (for example to keep a GlobalID-keyed
relationship class intact), run **Add Global IDs** on a *copy* of the source first, then Append. For
everything else, leave the box unchecked: Talus mints its own stable GlobalID for each feature, and
the attachments still follow.

> **Why the box is tempting:** "preserve the IDs" sounds like the safe choice, and the failure gives
> no hint that the setting is the cause. It is the single most common way to make a working Append
> look broken.

### QGIS — the Attachment widget

**The wizard sets it up for you.** In **Use in… → Edit in QGIS**, tick **"Need attachments?"** — it
hands you the exact widget click-path and the store-URL expression already filled in with this
layer's address. Manually, on a layer added through the ArcGIS-REST editing connection:

1. Right-click the layer ▸ **Properties ▸ Attributes Form** ▸ select the field
   **`_talus_attach`** (its alias reads *"QGIS attachment upload — ignore in Pro"*).
2. **Widget Type: Attachment.**
3. **Storage type: WebDAV Storage**; **Authentication = the same configuration** you created for
   editing (the encrypted Basic authcfg — not the Basic tab). *This is the desktop-only setup —
   for a project bound for QField/a phone, see the variant below.*
4. **Store URL**: click the **ε** (expression) button and paste — using this layer's service base:
   `'<advertised-base>/api/v1/esri-auth/layers/<layer_id>/attachments/' || "OBJECTID" || '/'`
   (the trailing slash is deliberate — QGIS appends the picked file's name itself).
5. Optional: **Integrated document viewer ▸ Image** for an inline photo preview in the form.

> **Make the fallback fail loudly.** The plain (non-ε) Store URL box is the *fallback* QGIS uses
> if the expression override is ever ignored — leave it as the base URL **without** an objectid.
> Then a dropped override 404s immediately instead of silently piling every upload onto one
> hardcoded feature. (This design caught a real silently-ignored-key bug: the expression
> override's property key is `storageUrl` — QGIS ignores unknown keys without a word.)

**Bound for QField / a phone? Different auth, same widget** (see Part 2). A QGIS
auth *Configuration* does not travel — the same lesson as the datasource below. For a project
that will be exported to a phone:

- **Authentication = No authentication** (leave the authcfg dropdown empty), and put the
  disposable PAT in the Store URL itself, in both the ε expression and the fallback base:
  `'https://talus:<PAT>@<host>/api/v1/esri-auth/layers/<layer_id>/attachments/' || "OBJECTID" || '/'`
  QGIS honours URL credentials with a normal 401→retry challenge; uploads land identically.
- The credential is **cleartext in the project file** — per-project, disposable, revoked when
  the project retires (and every project re-minted after any rotation).
- **QField itself will NOT upload through this widget.** On the phone, photo capture is
  local-only: QField stores the file in the project folder and there is no push step in any
  non-QFieldCloud workflow (verified against QField's source code and live, with server-side
  capture). The widget travels so the *desktop* side
  of the round-trip works; field photos come home with the project folder and are ingested
  through the same attachments door. See "Photos in the field" below.

**Using it.** Toggle editing on, open a feature's form, click the file button by `_talus_attach`,
pick a file — **it uploads the instant you pick it.** Allowed types are images, PDF, common
Office documents, plain text and CSV; **25 MB** per file; up to 50 per feature. The field always shows the *newest*
attachment (and previews it if you set the Image viewer); to see or delete any of a feature's
attachments, use the portal's record panel. Every upload appears in the web app and to other
editors immediately, and is recorded in the layer's audit history, attributed to you.

**Save the feature first.** A brand-new feature has no identity until you save it, and the upload
URL is addressed by that identity — so *save, then attach*. (Attaching then cancelling the form
still keeps the file; the upload is its own action, exactly as in ArcGIS.)

**Why the extra field shows in Pro too.** Pro and QGIS read the same service metadata, so Pro also
sees `_talus_attach`. **Ignore it in Pro** — it's the QGIS upload channel; edits to it are
discarded server-side (they change nothing). Pro's own attachments live in the native Attachments
tab above.

**It never leaks into data.** `_talus_attach` exists only in the FeatureServer metadata — there is
no such column. It is absent from the OGC API — Features door and from every export
(GeoPackage/GeoJSON/Shapefile/CSV): your attribute tables are untouched.

## Imagery — WMTS

Every raster you can view is served as a **WMTS 1.0.0** layer through the same two-door /
personal-access-token model as vector, on a separate path:

| Door | Path | For |
|---|---|---|
| **SIGN-IN** | `/api/v1/wmts-auth/1.0.0/WMTSCapabilities.xml` | ArcGIS Pro + QGIS |
| **PUBLIC** | `/api/v1/wmts/1.0.0/WMTSCapabilities.xml` | anonymous — serves **publicly-shared** rasters tokenlessly |

The single well-known **GoogleMapsCompatible** tile matrix set (EPSG:3857) is advertised; only
`GetCapabilities` + `GetTile` are served (no GetFeatureInfo, no WMS, no styles beyond `default`).

- **QGIS**: Layer ▸ Data Source Manager ▸ **WMS/WMTS** ▸ New ▸ paste the sign-in capabilities
  URL ▸ Authentication Basic (`talus` + your token) ▸ Connect ▸ add the raster. Or use the minted
  imagery connection file (Profile → Connect this computer → **Imagery**): same Load ▸ Import on
  the WMS/WMTS tab.
- **ArcGIS Pro**: Insert ▸ Connections ▸ Server ▸ **New WMTS Server** ▸ paste the sign-in
  capabilities URL ▸ Server Authentication (`talus` + your token). The same restart-after-change /
  credential-manager lore as the OGC leg applies. **If the connection succeeds but shows no
  layers, the token is dead** — see Troubleshooting; Pro will not tell you.

The per-raster **Use in…** assistant (the 🔌 on a raster catalog row) hands you the exact URL,
click-path, an inline token, and a Test-connection button.

### Revocation vs. the desktop tile cache (offboarding note)

Revoking a token **immediately** stops a desktop client from fetching anything new — the server
rejects the revoked token on the very next request (verified live: zero tiles served after
`revoked_at`). But a WMTS client caches the tiles it has already drawn (`Cache-Control:
private, max-age=3600`) and holds the capabilities document for the session, so **imagery already
on screen stays visible** until the user pans to an uncached area, the cache ages out (~1 hour),
or the app restarts. In ArcGIS Pro this is very visible: with the project still open after a
revoke, cached tiles keep displaying (and metadata still opens from the session cache) while any
*new* pan quietly 401s; **restarting Pro clears the session cache and it prompts for a fresh
token**. This is inherent to tiled-imagery protocols (QGIS, Pro, and Esri's own image services
all cache the same way) — not specific to Talus. **For a hard offboard, revoke the token AND have
the user close the app** (or wait out the cache); revocation guarantees no *new* access instantly,
not that already-rendered pixels vanish from an open session.

## Raster values — WCS

WMTS streams a *picture* of a raster; downloads hand you a *stale copy*. When the desktop needs
the live pixel **values** — "what is the elevation at this point, right now, from the
authoritative copy" — every **downloadable** raster is also served as a **WCS 1.0.0** coverage
through the same two-door / PAT model:

| Door | Path | For |
|---|---|---|
| **SIGN-IN** | `/api/v1/wcs-auth` | ArcGIS Pro + QGIS |
| **PUBLIC** | `/api/v1/wcs` | anonymous — serves **public + downloadable** rasters tokenlessly |

Why 1.0.0: QGIS's native WCS provider speaks 1.0.0/1.1 and *prefers* 1.0.0; Pro accepts a
1.0.0-only server (its version dropdown matches the server). Only `GetCapabilities` +
`DescribeCoverage` + `GetCoverage` are served — **no server-side analytics** (no hillshade, no
raster functions; the analysis happens in your GIS). Coverages are served in the raster's
**native CRS** only (your desktop reprojects for display), as **GeoTIFF** only, with request
grids bounded by the same megapixel cap as exports (an over-cap ask answers 413 — request a
smaller window or coarser resolution). A raster marked **view-only** (`downloadable` off) is
omitted from the coverage list and refuses values with 403 — streaming still works; values are
data.

- **QGIS**: Layer ▸ Data Source Manager ▸ **WCS** ▸ New ▸ paste the sign-in URL ▸ Authentication
  Basic (`talus` + your token) ▸ Connect ▸ add the coverage ▸ Identify reads live values. Or use
  the minted values connection file (Profile → Connect this computer → **Raster values**): Load ▸
  Import on the WCS tab.
- **ArcGIS Pro**: Insert ▸ Connections ▸ Server ▸ **New WCS Server** ▸ paste the sign-in URL ▸
  Server Authentication (`talus` + your token). No minted `.wcs` file yet — the "Copy URL for
  Pro" button on Profile hands you the URL. The same restart-after-change / credential-manager
  lore applies.
  - **★ Credential must live in the `.wcs`, not just Credential Manager** (verified with
    ArcGIS Pro 3.7). When you add the connection, **Save Login and tick "Connection
    file"** so the token is written into the `.wcs`. Credential-Manager-only auth carries
    `GetCapabilities`/`DescribeCoverage` (401→200) but the actual raster-open `GetCoverage`
    orphans at *"Failed to open raster dataset: HTTP 401"* — a Pro credential-scope quirk, not a
    server bug (the server is correct: 401 without Basic, 200 with it). Treat a token embedded in
    a `.wcs` as a **secret at rest** — sanitize before sharing the file.
  - **★ ArcGIS Pro does NOT render WCS coverages — use the Imagery (WMTS) door to display.** In Pro a WCS raster returns correct live **values on Identify** but
    **paints nothing** — confirmed against a valid tile with declared NoData + per-band statistics,
    a valid manual stretch (−0.725/109.393), *and* Dynamic Range Adjustment; it stays
    blank, silently. This is a limitation of Pro's WCS 1.0.0 client, **not a serving gap** — the
    coverage is standards-compliant and renderable, and QGIS draws it fine. **For a visible raster in
    Pro, add it through the Imagery / WMTS door** (Profile → Connect this computer → **Imagery**);
    use WCS for values and analysis. *(An earlier "Update Colorizer failed! / all-white" symptom was a served-metadata gap, since
    fixed; Pro's renderer still won't draw, so this is guidance, not a pending server fix.)*
- **GDAL/scripts**: `gdallocationinfo` / `gdal_translate` consume the same door:
  `WCS:https://…/api/v1/wcs-auth?COVERAGE=<raster-id>` with `talus:<token>` Basic auth.

The per-raster **Use in…** assistant (the 🔌 on a raster catalog row) has "Query live values"
recipes with the exact URL, click-path, an inline token, and a Test-connection button.

Revocation behaves like WMTS minus the tile cache: a revoked token is refused on the next
request, and coverage responses are `no-store` — nothing is replayed from disk.

## Scripts (GDAL/OGR, Python, anything HTTP)

Standard OGC API — Features Part 1 (Core + GeoJSON), CRS84 output, `limit` clamps at 1000:

```bash
# anonymous, public layers only
curl "<base>/api/v1/ogc/collections"
# signed-in, full scope
curl -u "talus:tpat_…" "<base>/api/v1/ogc-auth/collections/<layer_id>/items?limit=1000"
# GDAL/OGR
ogrinfo "OAPIF:<base>/api/v1/ogc-auth" -oo USERPWD="talus:tpat_…"
```

---

# Part 2 — Field collection (QField)

Everything below is core Talus: a phone running QField collects against the same hosted layers,
through the same FeatureServer door and the same access tokens, as the desktop recipes above.

## GNSS metadata fields — capture-time provenance

Talus's answer to AGO's "Add GNSS metadata fields": a one-click bundle of twelve per-feature
provenance/QA fields — accuracy, fix, DOPs, receiver (`gnss_*`).
Add it from the create wizard's Fields step (**Add GNSS metadata fields** checkbox) or on an
existing layer via **Manage ▸ Fields ▸ Add GNSS fields** (fields that already exist are skipped).
Nothing fills them at a desk — that's the point; they record *capture-time* GNSS state. **QField
fills them automatically** via default-value expressions in the QGIS project you take to the
field. **Talus Field**, the field app in Talus+, fills the same
fields natively; nothing in the QField path below depends on it.

**Wire it once, in QGIS, on the layer you added through the ArcGIS-REST editing connection**
(the same layer QField syncs): Layer **Properties ▸ Attributes Form** ▸ select each field ▸ under
**Defaults**, paste its expression — and **leave "Apply default value on update" UNCHECKED** on
every one, or later edits would overwrite the capture-time provenance:

| Bundle field | Default value expression | Note |
|---|---|---|
| `gnss_h_accuracy` | `@position_horizontal_accuracy` | meters |
| `gnss_v_accuracy` | `@position_vertical_accuracy` | meters |
| `gnss_altitude` | `z(@position_coordinate)` | WGS 84 ellipsoidal, meters |
| `gnss_fix_type` | `@position_quality_description` | the receiver-reported grade ("RTK fixed", "DGPS", …). On providers that report none, `@position_fix_status_description` (Fix2D/Fix3D) is the fallback |
| `gnss_fix_time` | `format_date(@position_timestamp, 'yyyy-MM-ddTHH:mm:ss') \|\| 'Z'` | `@position_timestamp` is UTC |
| `gnss_num_sats` | `@position_number_of_used_satellites` | |
| `gnss_pdop` | `@position_pdop` | |
| `gnss_hdop` | `@position_hdop` | |
| `gnss_vdop` | `@position_vdop` | |
| `gnss_correction_age` | — | no QField variable; leave blank or enter manually |
| `gnss_station_id` | — | no QField variable; enter the NTRIP mountpoint manually if you track it |
| `gnss_receiver` | `@position_source_name` | the positioning device's name |

**Getting the project (and its login) onto the phone** — a QGIS
auth *Configuration* does NOT travel — the `.qgz` stores only its `authcfg=` id, and QField
marks the layer invalid (current QFieldSync has no credential-export option either). The
working pattern: put a **disposable Read & edit PAT in the service URL itself** —
`https://talus:<PAT>@<host>/api/v1/esri-auth/rest/services/<layer_id>/FeatureServer` — with the
connection's Authentication set to *No authentication*. The credential rides inside the
project file in cleartext, so treat that PAT as per-project and **revoke it when the project
retires**. Verify before transferring: Layer Properties ▸ Information must show
`talus:…@` in the source and no `authcfg=`.

**Photos in the field** (proven end-to-end with QField 4.2 and QFieldSync 4.22):
QField never uploads attachments on a direct or packaged connection — capture is local-only, and
`_talus_attach` is an upload channel, not a column, so nothing a phone writes to it survives.
The working pattern rides the standard QFieldSync cable workflow plus one naming trick:

1. Give the attachment field a **naming expression that stamps the feature's identity** into the
   filename (Layer Properties ▸ QField ▸ Attachments Settings) — the proven form:
   `'DCIM/<layer>_' || regexp_replace(coalesce("gnss_fix_time",'NOFIX'), '[:]', '') || '.{extension}'`
   `gnss_fix_time` is populated by the GNSS bundle before the camera opens, is stored in the
   feature, and is effectively unique — so each returned photo names the exact feature it
   belongs to. (Generated field projects ship this wired, when the layer has attachments on.)
2. Package with QFieldSync (**cable action = Offline editing**; inspect the package — a
   nonzero-row `data.gpkg` and the layer repointed at it; QGIS reports success even when the
   copy silently failed), collect, photograph, and **Synchronize** on return. Features flow to
   the server; photos come back as files in the project folder (check the folder *root* as well
   as `DCIM/` — transfers can flatten it).
3. Ingest each photo through the attachments door, addressed by the feature the filename names:
   `PUT <base>/api/v1/esri-auth/layers/<layer_id>/attachments/<objectid>/<filename>` — the same
   gated, audited chute as every other client, matching `gnss_fix_time` → objectid.

What to expect in the field:

- **Creation only.** The expressions evaluate when a feature is **created** with positioning
  active and the crosshair snapped to the position; a feature digitized by hand (crosshair
  unlocked, or at a desk in QGIS) honestly gets blanks. That is correct behaviour — empty GNSS
  fields mean "not GNSS-captured," which is exactly the QA signal. Better: a hand-placed
  feature records `gnss_receiver = "manual"` — the provenance names itself.
- ★ **How you connect the receiver decides what the bundle records** — proven on both
  platforms with an external receiver:

  | Integration | Platform | `gnss_receiver` | Position | Provenance (fix/sats/DOPs) |
  |---|---|---|---|---|
  | Bluetooth NMEA → QField positioning device | Android | `nmea` | receiver-grade | **FULL** |
  | TCP/UDP NMEA → QField positioning device | iOS | `nmea` | receiver-grade | **FULL** |
  | Mock-location provider (receiver feeds the OS) | Android | `android` | receiver-grade | **stripped** (Unknown/-1, 0 sats) |
  | MFi receiver → Location Services (e.g. Eos Tools Pro) | iOS | `corelocation` | receiver-grade | **stripped** |
  | Phone internal | either | `android` / `corelocation` | ~5–15 m | stripped |

  **The rule: connect the receiver directly in QField** (Bluetooth NMEA on Android, TCP-NMEA on
  iOS — QField's Bluetooth path is Android-only, so iOS uses TCP whether or not the receiver is
  MFi certified) **whenever GNSS provenance matters.** OS location
  channels deliver the position but strip fix type/satellites/DOPs at the API boundary — the
  bundle then honestly records the stripped channel, which is itself the QA signal.
- **External receivers**: QField exposes the same data from a connected external GNSS under
  `@gnss_*` variants — swap the prefix if you want the raw external-sensor values rather than
  the position QField snapped to.
- **`gnss_fix_type` stores whatever text the source emits** (no forced vocabulary). For
  cross-walking to Esri's `ESRIGNSS_FIXTYPE` codes: 0 = fix not valid, 1 = GPS, 2 = differential
  GPS, 4 = RTK fixed, 5 = RTK float.
- Units are **meters/seconds** (receiver-native) — the field labels say so; don't enter feet.

## Field projects — the portal as the distribution point (QField Import URL)

A **Field project** is a catalog item type for the `.qgz` a crew's phone runs: a publisher
uploads it once, shares it like any other item, and every crew pulls it from **one stable
URL** with QField's Import URL — no AirDropped files, no hand-managed copies. Publishers
update server-side (Manage ▸ Replace project file); crews re-import. The portal serves the
artifact with conditional-GET support, so a re-import of an unchanged project costs headers,
not bytes.

**★ What Talus does NOT do — read this before you plan around it.** Talus **never opens or
edits a project**. A Field project is stored, versioned, shared, and served as an opaque
file; the catalog's title/layer count come from a read-only peek, nothing more. There is no
`.qgs` editor in the portal and none is coming — "edit" is always *download → QGIS →
re-upload*. If you want a web-authored map, build a Talus web map; the field-project door is
the distribution point for QGIS-authored field work, not a QGIS-compatibility surface.

### Publisher workflow A — generate from a web map (the one-button path)

The common case needs no QGIS at all: build (or open) the **web map** that shows what the
crews should see, then **Catalog ▸ your map ▸ Manage ▸ Settings ▸ Make field project**.
The server compiles the map into a ready-to-collect project — every visible hosted
point/line/polygon layer with its web-map color, the GNSS bundle's capture expressions
pre-wired per layer, the photo kit on every layer with attachments enabled, an OSM basemap,
and the map's opening view. It also **mints a dedicated access token** for the project
(labeled `field-project: <title>` in your Profile ▸ Access tokens list) and embeds it in
the artifact — never your personal token.

What to know about the generated item:

- **The web map stays the source of truth.** When the map changes (new layer, recolor),
  open the item's **Manage ▸ Regenerate** — it recompiles from the current map. The embedded
  token is reused, so already-imported copies keep reading live layers; crews still
  delete-old + re-import to get the new compile. Talus never edits a `.qgz` — regenerate is
  the ONLY update path for generated content.
- **What doesn't ride (the card warns loudly):** rasters, external references, tables,
  layers hidden in the map, and view layers (not servable to QField yet). A layer's web-map
  **filter does not apply** — the field project shows all features (QFieldSync's
  area-of-interest clip is the offline-packaging answer for big layers). Per-category
  colors flatten to the category default in v1.
- **Deleting the item revokes its token automatically** — retirement is one action. The
  generated item is NOT pre-shared; share it deliberately, like an upload.
- **What the embedded credential can do:** it reads what you can read, and it adds, edits and deletes
  only on the project's own layers, as you could. Anyone you share the project with can read it from the
  file, and edits made with it are recorded as yours. A project generated before 1.0.0 gains this limit
  when you regenerate it.
- Generation requires the **advertised base URL** (Admin ▸ Connections) — the project must
  carry a phone-reachable address, and the server never guesses its hostname.

### Publisher workflow B — author in QGIS and upload

1. Author the project in QGIS against this portal's layers (the Field-collection section
   above; the generated-project tooling wires the GNSS bundle + photo kit for you).
2. **Catalog ▸ Field projects ▸ + Upload field project.** The upload is checked at the lip
   (it must be a zip holding a `.qgs`, size-capped) and the card warns — without blocking —
   about the classic packaging hazards:
   - **more than one project file inside** (`.qgs` + `.qgs~`): QGIS's export folder ships a
     `.qgs~` *backup of the pre-conversion project* by default — on a phone it still edits
     the server. Re-export a clean archive (explicit file list, `zip -X`, no `__MACOSX/`).
   - **no layer pointing at this portal**: probably the wrong file.
3. Share it (Organization or specific groups). **There is deliberately no public tier**: the
   project embeds an access token, and a public door would hand that token to anyone.
4. To update: **Manage ▸ Replace project file**. Same URL, new bytes; crews re-import.
5. To retire: delete the item **and revoke its PAT** (next section). (Generated items do
   the revoke for you on delete.)

### The credential inside the artifact

A field project carries a **PAT in its datasource URLs** (the userinfo pattern
above) — treat the file as a credential:

- mint a **dedicated, labeled, per-project token**, never your personal one;
- scope it Read & edit only if the crews edit;
- **revoke it the day the project retires** (Profile ▸ Access tokens) — revocation cuts
  every copy on every phone at once, which is exactly the point;
- share the catalog item as narrowly as the crews require.

The Import URL itself is credentialed the same way: the crew's token rides the URL
(`https://talus:<ACCESS-TOKEN>@…/api/v1/field-projects/<id>/archive`). Anyone without a
valid token gets nothing — there is no anonymous path to a field project.

### Crew workflow (the phone side)

1. In QField: **⋮ next to "Imported projects" ▸ Import URL**, paste the project's Import URL
   (from the catalog item's dialog) with `<ACCESS-TOKEN>` replaced by your token.
2. Collect. In live mode the photo's capture preview is your only receipt — the attachment
   field reads blank on reopen until the office ingest runs; that's expected.
3. **When the office publishes an update: delete the old project on the phone first, then
   Import URL again.** Re-importing WITHOUT deleting creates a duplicate ("name_1") — with
   live layers that's clutter, not data loss, but the duplicate pile confuses everyone.

**★ Android import hygiene (this one bites):** always bring a project in via **⋮ ▸ Import**
— never open a `.qgz`/zip in place from Downloads. Opening in place makes *Downloads* the
project folder, and "Compress and send" then ships **everything sitting beside it** (whole
APKs have ridden along in live testing). The wrong-import signature: the legend shows
`log_*` tables and there's no camera button — delete the project and import it properly.

**★ Offline packages go stale on sync:** a QFieldSync offline package (the cable workflow
above) is a per-outing artifact. Once its edits are synchronized, **never collect on that
package again** — the phone's edit log is untouched by the desktop sync, so a re-sync pushes
the old edits a second time (duplicates). Fresh package per outing; the hosted Field project
with live layers doesn't have this problem.
