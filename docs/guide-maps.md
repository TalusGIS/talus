# Web maps

This guide covers building and using web maps in the Talus portal: creating a map, adding and arranging
layers, styling, labels, pop-ups and filters, finding, selecting and editing features, and exporting,
saving, sharing and embedding a map. Creating layers and importing data are in `guide-layers.md`.

## Who can do what

| Task | Who |
|---|---|
| Open a new map, add layers, try labels, filters and pop-ups | Any signed-in user (kept only if the map is saved) |
| Save a new map | Publishers and administrators |
| Change and re-save a saved map, its bookmarks and layer names | The map's owner or an administrator |
| Change a layer's style | People who can manage that layer |
| Add, move, reshape or delete features | Editors, publishers and administrators with edit permission on the layer |
| Open a shared map, read pop-ups, use bookmarks, export | Anyone the map is shared with |

## Create a map

1. In the top bar, choose **New map**. If your portal shows a **New** menu instead, open it and choose
   **Map**.
2. The map opens as "Untitled map" with an **Unsaved** chip, on your organization's default basemap and
   starting view.

A visitor who is not signed in sees the **New map** link, but it leads to sign-in: only signed-in users
can build a map.

## Add layers

1. Choose **Add layers** in the header bar.
2. Under **Browse catalog**, search with **Search layers…** or use **All**, **Owned by me**, **Shared with
   me** or **Favorites**. You see only layers you may view.
3. Choose **+ Add** on an item; it changes to **✓ Added**. Rasters, references and standalone tables
   are listed in their own groups (**Rasters**, **References**, **Tables**).
4. Choose **Done**.

**Add from URL** takes an ArcGIS feature or image service, a GeoJSON URL, or an XYZ tile template:
paste it, choose **Check**, then **+ Add to map**. The data stays on the remote server. **Add from file**
(publishers) creates a new layer; see `guide-layers.md`. **Sketch layer** opens the sketch toolbar.

A map holds up to 200 layers and 20 tables. Tables draw nothing: they are listed under **Tables** in the
Layers panel, and choosing one opens its rows.

## Arrange layers in the Layers panel

The **Layers** panel is on the left; the top of the list draws on top.

- The checkbox shows or hides a layer. **Move up**, **Move down** and **Remove from map** (×) change
  order and remove it.
- Right-click a row, or choose its **Layer options** (⋯) button, for **Attribute Table**, **Zoom to**,
  **Zoom to selected**, **Clear selection** and **Rename**. **Rename** changes the name on this map only;
  **Reset to original name** undoes it.
- A raster row has an opacity slider, a colormap list for single-band rasters, and **Zoom to**.
- The pin keeps the panel open; unpinned, it folds to a **Layers** tab.

A funnel on a row means the layer is filtered; a link mark means it is referenced from elsewhere.

## Use the layer inspector

Click a layer's name in the Layers panel. The inspector opens on the right with **Style**, **Labels**,
**Filter**, **Popup** and **Form** tabs; a dot marks a tab whose setting is in use. Click the name again,
or the inspector's ×, to clear the selection. With nothing selected the inspector folds to an
**Inspector** tab; its pin holds it open for the session.

The inspector appears only on maps you can change (a new map, or a saved map you own or administer).
Viewers still see the map's labels, filters and pop-ups. **Style** and **Form** need manage rights on
the layer; **Form** applies to hosted layers only.

## Style a layer

1. Select the layer and open **Style** (or click its color swatch in the Layers panel).
2. Choose **Single symbol** and a **Color**, or **Types (unique values)**, an **Attribute**, and a color
   per value. **Color ramp** and **Flip** recolor every value; empty values use the **«No value»** color.
3. Set the geometry options:
   - Points: **Symbol** (a plain circle or a bundled icon, searchable by name or category), **Size** in
     pixels or **Scale with zoom**, and for icons **Icons from zoom** (further out, the layer draws as
     circles; default 14).
   - Lines: **Line width** and **Line style** (**Solid**, **Dashed**, **Dotted**).
   - Polygons: **Fill opacity**, **Outline color**, **Outline width**.
4. Choose **Save**. **Reset to default** returns the layer to a single symbol.

Style is saved on the layer immediately, not on the map, so every map showing the layer changes. Unique
values are capped at 200, with a warning above 50.

## Label a layer

1. Select the layer and open **Labels**.
2. Pick a field in **Label by**, then **Size** (8 to 32), **Text color**, **Halo color**, **Bold**, and
   optionally **From zoom** to hide labels when zoomed further out.
3. Choose **Apply**. **Remove labels** turns them off.

Labels, filters and pop-up settings belong to the map: save the map to keep them.

## Configure pop-ups

1. Select the layer and open **Popup**.
2. Keep **Show pop-ups for this layer** checked, or uncheck it so the layer ignores clicks.
3. Pick a **Title** field, or keep the layer name.
4. Under **Fields shown, top to bottom**, check fields (**All** / **None** set every box) and reorder
   them with the arrows. Up to 30 can be shown.
5. Choose **Apply**. **Reset to defaults** clears your settings.

Click the map to open a pop-up. Where features overlap, the pop-up pages through them (**1 of N**) and
highlights each one. A raster shows its band values. If a feature has attachments, the first four show as
thumbnails or file chips; "+N more — view in details" and the **Details** icon open the full record. A date or time in a pop-up or in the attribute table is shown in your browser's date format, a time with a time zone in your own time zone; hover over it to see the stored value (`guide-layers.md`).

## Filter a layer

1. Select the layer and open **Filter**.
2. Choose **+ Add condition**, then a field, an operator (for example **is**, **is not**, **contains**,
   **is any of**, **is between**, **is at least**) and a value. Features show only where every condition
   is true.
3. Choose **Apply**. **Clear all** removes the filter.

Up to 10 conditions; **is between** counts as two. The attribute table's filter is the same filter.

## Save bookmarks

1. Choose **Bookmarks** in the right-hand rail.
2. Type a name in **Name this view…** and choose **+ Bookmark**.
3. Click a bookmark to fly to it. The pencil renames it or updates it to the current view; arrows
   reorder; the trash can deletes.

A bookmark keeps the view only, not which layers are on. Save the map to keep bookmarks (up to 50).

## Switch the basemap

Choose **Basemap** in the right-hand rail and pick from what your portal lists: **Light**, **Dark** and
**Gray** under **Downloaded**, and **OpenStreetMap**, **OpenStreetMap Gray**, **OpenStreetMap Dark**, **USGS
Topo** and **Imagery** under **Streamed**. A portal may list only one kind; an air-gapped installation lists
Downloaded. The choice saves with the map. The
organization's basemap and default extent are set up by an administrator; see `guide-imagery.md`.

## Find places and features

- **Address search**: choose the pin button at the lower left, type in **Find address or place**, and
  pick a suggestion or press Enter. **Search in** appears when several locators exist
  (`guide-geocoding.md`).
- **Feature search**: choose **Find features in your layers** (magnifier) and type two or more
  characters. It searches the attributes of visible layers; `*` and `?` are wildcards, and typos return
  **similar** matches. A result zooms to the feature and opens its pop-up.
- **What's here?**: right-click the map and choose **What's here?** for longitude and latitude (WGS 84),
  the nearest address if found, and the point in another system via **Show in another CRS**.
- **My location** zooms to your device's position if the browser allows it.

## Select features and use the attribute table

1. Click a layer's name in the Layers panel.
2. Choose **Select features on the map**. Drag a box or click a feature. Shift adds, Ctrl (⌘ on a Mac)
   removes, and a plain click on empty map clears. Esc stops.
3. Use **Zoom to selected** in the layer's options, or **Clear selection · N** on the map.

**Attribute Table** docks the layer's rows under the map. **Map extent** limits rows to the current view,
and selecting rows selects the same features on the map. Tables of layers from another server are
read-only.

## Edit features in the map

Choose **Edit mode** (pencil) in the right-hand rail; it appears only if you can edit a layer on the
map. The header shows **Editing on** and an editing toolbar opens at the top.

- **Pick the layer** in the toolbar's layer list (**Edit** column). The same list has **Enable
  snapping** and a **Snap** box per layer; snapping starts on. "zoom in to snap" means the layer is too
  dense to snap to at this zoom.
- **Add**: choose **+** and follow the hint (click to place a point; click vertices and double-click to
  finish a line or polygon). Polygons also offer **Rectangle**, **Circle** and **Freehand** under **More
  shapes**. Fill in the form, attach files with **+ Add** under **Attachments** if offered, and choose
  **Save**. Esc cancels.
- **Attributes**: click a feature and choose **Attributes** in its pop-up.
- **Move** (points): choose **Move**, drag, then **Save location**.
- **Reshape** (lines, polygons): choose **Edit shape**, then drag a vertex, drag a midpoint to add one,
  or right-click a vertex to remove it. Changes save automatically; choose **Done**.
- **Delete**: **Delete** in the pop-up, or select features and use the toolbar's trash button. Deleting
  removes attachments too and cannot be undone.

A layer's owner can turn off adding, shape editing, attribute editing or deleting; those buttons then
do not appear.

If someone else saved the feature first, Talus does not overwrite it. You see "This … changed while you
had it open" and choose **Discard mine & reload** or **Overwrite with mine** (for a delete, **Cancel** or
**Delete anyway**). A bulk delete skips features changed elsewhere and reports the count.

## Draw sketch notes

Choose **Sketch** in the header bar, pick **Point**, **Line** or **Polygon**, a color and an optional
label, then draw. Notes show as one **Sketch** row in the Layers panel and save with the map (up to
200). Click a note to recolor, relabel, **Move**, **Reshape** or **Delete** it.

## Export the map

Choose **Export** in the header bar.

- **Screenshot**: name the file and choose **Download PNG**: the current view, a legend of visible
  layers, and the map attribution. Tool panels are left out.
- **Print**: set **Title**, **Landscape** or **Portrait**, and **Resolution** (**Screen**, **150 dpi**,
  **300 dpi**, **600 dpi**), then **Download PDF**: one US Letter page with title, date, map, legend, a
  scale bar in feet or miles, a north arrow, and attribution. The PDF is **georeferenced**: open it in
  Avenza Maps, QGIS or Acrobat and it is a map, not a picture — your location shows on it in the
  field. A **tilted** view exports as seen but is not georeferenced; the Print tab says so. Reset the
  tilt first if you need the georeferencing.

## Save and share a map

1. Choose **Save map**. The first time, enter a **Title** and optional **Description**, then **Save**.
   Later saves update the map in place.
   Beside **Save map**, publishers also see **Field-ready**. It marks the map for Talus Field, the field app in Talus+ (`talus-field-runbook.md`), and is saved with the map. Without Talus Field it does nothing.
2. A save keeps the layers, their order and visibility, labels, filters, pop-ups, layer renames, tables,
   bookmarks, sketch notes, the basemap, and the view on screen.
3. To share, open the Catalog and choose **Manage map** on the map's row. On **Sharing**, pick **Owner
   Only**, **Organization Only** or **Public Access** (when offered) and any **Groups**, then **Save**.
   **Copy** copies the map's link.

Leaving with unsaved changes asks "Leave this map?". Changes kept on your device from an earlier visit
are offered back with **Restore** or **Discard**.

Sharing a map does not share its layers. A viewer who cannot see a layer gets the rest of the map and a
notice such as "1 layer in this map is unavailable to you and was skipped." Making a map public opens
**Review sharing**, which lists layers and rasters that are not public; choose **Update sharing** to
make them public too, or ask an administrator for those under **Needs an administrator**.

## Embed a map in another website

1. Share the map with **Public Access** and save.
2. Under **Embed on another website**, choose **Copy embed code** and paste it into your web page.

The embed shows the title, a **Legend** button, zoom buttons, a scale bar, basemap choices and **Open
full map ↗**. It always shows the public view, even to signed-in visitors, and only public layers appear.
Making the map private stops every embed.

A visitor who is not signed in can click a point, line or polygon to see its pop-up. The pop-up lists the layer's
active fields under the labels you gave them. A field that uses a picklist shows its stored code, and the pop-up has
no link to the full record, which needs a sign-in.

## Limits, and what the map deliberately does not do

- No 3D, no routing, and no analysis or geoprocessing tools.
- Styles are single symbol or unique values only: no graduated or class-break styles, and no opacity
  setting for vector layers other than polygon fill.
- Labels use one field; labels and filters take no expressions.
- Print is one US Letter page with a fixed layout.
- Layers from another server load at most 2,000 features in view; zoom in for more.
- Address search, feature search and the **What's here?** address require signing in.
- Editing needs a connection; offline, the map shows only areas already loaded.

## If something goes wrong

| What you see | What to do |
|---|---|
| "No basemap installed yet — an administrator can set one up in Admin ▸ Basemap." | Layers still display. Ask an administrator; the page is **Admin** → **Basemap** (`guide-imagery.md`). |
| "This area is outside the downloaded basemap — an administrator can download it, or offer a streamed map, in Admin ▸ Basemap. Data layers still display." | Nothing is wrong with your layers. Pan back, switch to a streamed basemap if your portal lists one, or ask an administrator to download a larger area. |
| "Your map is empty — choose “Add layers” at the top to put something on it." | Add a layer; **Save map** is disabled on an empty map. |
| "… unavailable to you and was skipped." | A layer is not shared with you; ask its owner. |
| "Could not open this map (HTTP 403)" or "(HTTP 404)" | The map is not shared with you or does not exist. |
| "Save failed: …" | Read the reason, check you are still signed in, and save again. |
| "The map has a layer from another site that blocks image export. Remove that layer, or use a same-origin source." | Remove that layer, then export. |
| "Could not build the PDF (…). Try a lower DPI, or remove the layer if it persists." | Choose a lower **Resolution**. |
| "Address search is unavailable — every locator failed; tell your administrator" | Tell your administrator. |
| "You’re offline — reconnect to edit. Your change wasn’t saved." | Reconnect and repeat the change. |
| "This is a multi-part shape — vertex editing isn’t supported yet." | Reshape it in a desktop GIS (`connect-desktop.md`). |

## Related guides

- `guide-portal.md` — signing in, the Catalog, groups and roles
- `guide-layers.md` — creating layers and tables, importing data
- `guide-geocoding.md` — locators and batch geocoding
- `guide-imagery.md` — rasters, the organization basemap and default extent
- `guide-dashboards.md` — dashboards
- `guide-story-maps.md` — story maps
- `connect-desktop.md` — using Talus layers in ArcGIS Pro and QGIS
