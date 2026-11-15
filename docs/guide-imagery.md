# Imagery, rasters, and basemaps

This guide covers uploading aerial imagery and other rasters, managing and sharing them, using them in maps and
desktop tools, downloading a clip, and the organization basemap every map draws on. It applies to Talus Community and
Talus+ alike.

## Who can do what

| Who | Can |
|---|---|
| Viewer, editor | Add rasters shared with them to maps, read pixel values, export when downloads are allowed, use **Use in…** |
| Publisher | All of that, plus upload rasters and manage the ones they own |
| Administrator | All of that, plus manage any raster and run **Admin** → **Basemap** |
| Open Data members, administrators | Publish a raster to the public Open Data catalog |

Only a raster's owner or an administrator can manage or delete it. Anyone else sees a notice naming the owner.

## Upload an aerial image

1. Go to **Catalog** and choose **Rasters**.
2. Click **+ Upload raster**. Only publishers and administrators see it.
3. Under **File**, choose the file. The picker offers .tif, .tiff, .cog, .jp2 and .img files.
4. Under **Details**, check **Title \*** (filled from the file name, up to 200 characters). **Description** is optional.
5. Click **Upload**. The button reads **Uploading…** until the server finishes.

The server checks that the file is a readable raster whose coordinate system has an EPSG code. A Cloud-Optimized
GeoTIFF is stored as it is. Any other raster is converted to one with lossless compression, and its coordinate system
is kept. Maps then draw the raster as tiles made on demand.

Uploading and converting happen in one step, with no background job or status page. Keep the page open until the
dialog closes. The default limit is 5,120 MB per file.

## Manage a raster

Click the gear (**Manage raster**) on the raster's row. The dialog has four tabs.

- **Sharing**: choose **Owner Only**, **Organization Only** or **Public Access**, tick any **Groups**, and click
  **Save**. **Public Access** works only when an administrator has turned public access on. A non-administrator also
  needs the organization to let non-admins share publicly.
- **Metadata**: the descriptive record. It must be complete before the raster can go to Open Data.
- **Open Data**: see below.
- **Settings**:
  - **Allow download / export**: when off, the raster can still be viewed and streamed as WMTS, but not downloaded or
    read over WCS. Use it for licensed imagery.
  - **Protect this raster from deletion**: while on, nobody can delete the raster, administrators included.
  - **Used in**: the maps that use this raster.
  - **Transfer ownership**: shown to administrators, and to owners when the organization allows transfers.

A raster's title cannot be changed after upload.

To delete a raster, click the trash icon on its row and confirm with **Delete raster**. "The raster and its files are
removed from the server. This can’t be undone." A protected raster shows a padlock instead of the trash icon.

## Put a raster on a map

- In the catalog, click the raster's name. A new map opens with the raster added.
- In any map, click **Add layers**, find it under **Rasters**, and click **+ Add**.

The raster goes to the bottom of the **Layers** list. Its row has a checkbox, an opacity slider, **Zoom to**, **Move
up**, **Move down**, **Remove from map**, and, when downloads are allowed, an **Export / download** icon. A
single-band raster also gets a color-ramp list starting at **Grayscale (default)**.

Click the map over a visible raster to see its band values at that point.

**Your own imagery as the backdrop:** the basemap switcher cannot use your rasters. Instead, add your aerial as a layer
and keep it at the bottom of **Layers**.

## Export a clip of a raster

1. In **Layers**, click the raster's download icon.
2. Drag a box on the map, or click **Whole raster**. **Cancel** stops.
3. Choose a **Format**. The list shows only what the server can write: **Cloud-Optimized GeoTIFF**, **GeoTIFF**, and
   where available **ERDAS IMAGINE (.img)** and **JPEG 2000 (.jp2)**.
4. If you want, fill in **Downsample longest side to (pixels) — optional**. Leave it blank for full resolution.
5. Click **Download**.

The file keeps the raster's coordinate system and all its bands. By default a full-resolution export is capped at 64
megapixels (about 8,000 × 8,000). For a larger area, downsample to between 1 and 20,000 pixels.

## Use a raster in QGIS, ArcGIS Pro or a script

Click the plug icon (**Use in…**) on the raster's catalog row. Under **I want to…**, choose **Stream in ArcGIS Pro**,
**Stream in QGIS** (WMTS imagery), **Query live values in ArcGIS Pro**, **Query live values in QGIS** (WCS pixel
values), or **Download a copy**. The wizard gives the address, the steps, and a connection test. Live values and
downloads need **Allow download / export** turned on. See `connect-desktop.md` for the details.

## Publish a raster to Open Data

You must be in the Open Data group or be an administrator, and public access must be on in **Admin** → **Sharing &
access**.

1. In **Manage raster**, complete the **Metadata** tab.
2. On the **Open Data** tab, click **Curate into Open Data…**, then **Yes, publish publicly**.

This makes the raster public. Its Open Data page shows a preview and a WMTS address, plus GeoTIFF and Cloud-Optimized
GeoTIFF downloads and a WCS address when downloads are allowed. **Remove from Open Data** does not make it private;
change that under **Sharing**.

## The organization basemap

The **Basemap** button on the map's right-hand rail lists what your portal offers, under up to two headings:

- **Downloaded**: **Light**, **Dark** and **Gray**, one OpenStreetMap-derived street map stored on your server, with
  its fonts and symbols. These work with no internet.
- **Streamed**: **OpenStreetMap**, **OpenStreetMap Gray** and **OpenStreetMap Dark** (worldwide), and **USGS Topo**
  and **Imagery** (United States; detail stops at zoom 16). Nothing is stored on your server. Each viewer's browser
  fetches these from the publisher over the internet, and the publisher sees the viewer's internet address and the
  area being viewed.

An administrator chooses which kinds are listed. Your layers stay when you switch, and a saved map remembers its
basemap.

### Choose what the portal lists (administrators)

**Admin** → **Basemap** → **What this portal lists** has three choices: **Streamed**, **Downloaded**, or **Streamed
and downloaded**. A new installation starts on **Streamed**, with new maps opening on OpenStreetMap, so there is a
street map from the first sign-in. An installation upgraded from an earlier release keeps listing what it listed
before.

- Under **Streamed maps this portal offers**, untick a publisher to stop offering its maps.
- Choose **Downloaded** to send no viewer's browser to the internet for a map background. Download an area first
  (below); until one is downloaded, maps have no background.
- **New maps open on** sets the basemap a new map starts with. Saved maps keep the basemap they were saved with. A
  saved map on a streamed map the portal stops offering opens on that default instead.

### Download or update the street basemap (administrators)

You need a downloaded area for the Light, Dark and Gray looks, for maps that work with no internet, for Talus Field
packs, and for the map on report sheets.

1. Pan a map to your area, then open **Admin** → **Basemap**.
2. Enter **West**, **South**, **East** and **North** in degrees, or use **Use last map view** or **Set on map**.
3. Keep **Max zoom** at 15 (1 to 15). Leave **Build date** blank for the latest build, or enter YYYYMMDD.
4. Click **Fetch basemap**, or **Update basemap** if one is installed. The page shows **Working: …** while it runs,
   which can take a few minutes. Open maps load the new basemap when you return to their tab.

Areas over 1 square degree get a size warning; the server refuses areas over 64. The page marks a basemap "over 6
months old".

When a map on a downloaded look is moved outside the area you downloaded, it says "This area is outside the
downloaded basemap".

**With no outbound network**, Fetch cannot work. Extract a `.pmtiles` file on a connected machine, carry it in, and use
**Upload .pmtiles…** (up to 4 GiB, PMTiles version 3); `basemap-provisioning-runbook.md` has the extract command. Then choose **Downloaded** under **What this portal
lists**, because streamed maps cannot load there. Everything else here works offline.

**Default extent** sets where new maps open. See `guide-administration.md`.

## Limits, and what it deliberately does not do

- No analysis: no hillshade, band math, classification or raster functions. Read the values over WCS and analyze them
  in your desktop GIS.
- No mosaics. Each upload is one raster, and each export clips one raster.
- No reprojection on export.
- MrSID (.sid) and ECW (.ecw) files are not in the upload picker.
- Display settings are opacity, plus a color ramp for single-band rasters. Rasters with three or more bands show their
  first three bands as red, green and blue.
- No 3D, point clouds or time series.

## If something goes wrong

| You see | What to do |
|---|---|
| "could not read this file as a raster. Supported formats are GeoTIFF/COG, JPEG 2000, and ERDAS IMG." | Save the file as GeoTIFF in your desktop GIS and upload again. |
| "could not read this raster: the file is damaged or incomplete." | Export or copy the file again from where it came from, then upload again. |
| "raster has no CRS" / "raster CRS has no EPSG code (reproject to a standard CRS before upload)" | Assign or reproject to a standard coordinate system, then upload again. |
| "raster exceeds the … MB upload limit" | Split the file, or ask your administrator about the limit. |
| "could not convert this raster to a COG." | Convert to Cloud-Optimized GeoTIFF in QGIS or GDAL, then upload that. |
| "selected area is … px at native resolution, over the … MP export cap …" | Draw a smaller box, or fill in the downsample field. |
| "the selected area does not overlap this raster" | Draw the box over the raster. |
| "this raster is marked view-only (not downloadable)" | Ask the owner to turn on **Allow download / export**. |
| "raster is delete-protected — turn off delete protection before deleting" | Untick **Protect this raster from deletion** in **Settings**. |
| "could not reach the Protomaps builds index — no egress? …" | The server has no internet access. Use **Upload .pmtiles…**. |
| "a provisioning operation is already running" | Wait for the current fetch or upload to finish. |

## Related guides

- `guide-maps.md`: building and saving maps
- `guide-portal.md`: the catalog, sharing, metadata and Open Data
- `guide-administration.md`: users, public access and organization defaults
- `connect-desktop.md`: WMTS and WCS in QGIS and ArcGIS Pro
