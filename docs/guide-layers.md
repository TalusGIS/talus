# Layers, tables, and your data

This guide covers the data you keep in Talus: creating hosted layers and geometry-less tables, designing their fields
and picklists, importing and exporting data, working in the data table, and what each record's detail shows. It applies
to Talus Community and Talus+ alike.

> **Before you import anything, keep a backup of the source data in its original form.** An import makes a copy in
> Talus. The original file or service is your way back if a column, a coordinate system or a row does not come through
> the way you expected.

## Who can do what

| Role | Can |
|---|---|
| Viewer | Open layers and tables shared with them: the data table, filters, export, record detail and edit history |
| Editor | Everything a viewer can, plus add, change and delete features and rows, import features into an existing layer, run **Calculate**, and add attachments, on layers shared with them for editing and within the owner's editing settings |
| Publisher | Everything an editor can, plus create layers, tables, references and picklist domains, and use **Import data**. You own what you create and manage it from its **Manage** dialog |
| Administrator | Manage any layer, and edit a layer even when its owner has turned editing off |

Anyone signed in can reuse any picklist domain; creating a new one needs the Publisher or Administrator role. Sharing
is covered in `guide-portal.md`.

## Create a layer or a table

1. In **Catalog**, open the **Layers** tab and click **+ New layer**, or the **Tables** tab and click **+ New table**.
2. **Define:** enter a **Title** and pick a **Geometry**: **Point**, **Line**, **Polygon**, or **Table — no geometry**.
   Under **Schema**, choose **Define fields manually** or **Import a schema**.
3. **Spatial Reference** (layers only): search by name or EPSG code and click the coordinate system your data uses.
   If you pick anything other than WGS 84, you can tick **Advanced — keep the layer in this coordinate system**. Normally Talus
   stores geometry in WGS 84 and converts to and from your system. Native storage keeps coordinates exactly as entered
   in your projected system, which matters for survey-grade data. The map looks the same either way, and this choice is
   only offered here. Pick the system your data is really on, datum included: Talus converts a layer as what it
   says it is. Data on a plain NAD83 or a NAD27 system is treated as that era's coordinates and is corrected so
   that it lines up with newer data and with GPS positions (by up to a few feet for plain NAD83, far more for
   NAD27). If your data is on NAD83(HARN) or NAD83(2011), choose that system, not the plain NAD83 one of the same
   name. Layers on NAD83(HARN) and NAD83(2011) are drawn where their coordinates put them; the difference between
   those two adjustments (a few centimetres) is not modelled.
4. **Fields:** add fields (next section). If you chose **Import a schema**, click **Import schema file** and pick a CSV,
   GeoPackage, or Esri XML Workspace file.
5. **Sharing:** choose who can see it, then click **Finish**. Nothing is saved until you click **Finish**.

You can also start from a file on a map: **Add layers** → **Add from file** opens this wizard already filled in from a
zipped Shapefile, GeoPackage, GeoJSON or CSV file, and imports the features after **Finish**.

## Design fields

Every layer has built-in fields: an ID, **Notes**, and the created-by, created-at, updated-by and updated-at fields that
Talus fills in. The fields you add are owner-defined. Add them in the wizard, or later under **Manage** (the gear on the Catalog
row) → **Fields**.

In **Add a field**:

- **Field Name**: lowercase letters, digits and underscores, starting with a letter. It can't change later.
- **Field Alias**: the label people see.
- **Type**: **Short Integer**, **Long Integer**, **Float**, **Double**, **Text** or **Date**. The type can't change later.
- **Format** (optional): for text, a preset (phone, ZIP, email, URL) or **Custom pattern (advanced)…**; for text and
  numbers, **Picklist (coded values)** or **Yes / No**.
- **Required**, a **Default Value**, and **Rules**: a **Minimum**/**Maximum** for numbers and dates, **Min length**/**Max
  length** for text. Date fields have **Default new features to today’s date**, ticked by default.

Click **+ Field** to add it. Values are checked whenever a record is saved.

On an existing field, use the arrows to reorder, **Rules** to change its rules (**Check impact** first shows how many
rows already break the new rule; those rows keep their values until next edited), **Retire** to hide it from forms
while keeping its values, and **Delete** to remove a field that holds no data. There is no rename control.

To reuse a schema, use **Export CSV** or **Export GeoPackage**, and **Import from CSV**, which also accepts a single-table
GeoPackage or an Esri XML Workspace. You review a preview before **Import … fields**. Picklist fields in an imported
schema are flagged; add them yourself and bind a domain.

## Create and reuse picklists (domains)

A domain is a reusable list of codes and labels. A publisher or an administrator creates one from the **Domains** page
(**+ New domain**) or inside **Add a field** (**Picklist values** → **Create new**). Anyone can reuse one: choose **Use
existing**. Editing a domain updates every field that uses it. On the **Domains** page, a domain's owner or an
administrator can add values (**+ value**), reorder, and **Retire**/**Restore** them.

**Used by** lists the fields that use a domain on the layers you can view; an administrator sees all of them. A domain
that is used anywhere can't be deleted, even where the layers using it are ones you can't see, and a value in use must
be retired instead of deleted.

On a layer with two picklist fields, **Depends on** narrows one list by the other's value.

## Relate a layer to a table

A relate shows a table's matching rows on a feature's record detail. In **Manage** → **Relates** (point, line and
polygon layers only), choose the **Table**, **This layer’s key**, **Table’s key**, a **Display name** and
**Cardinality**, then **Add relate**. A relate is navigation only: no constraint is created, nothing cascades, and
deleting it changes no data.

## Turn on attachments and set editing rules

Both live in **Manage** → **Settings**.

- **Allow attachments on this layer** is off by default. While on, editors can attach images, PDFs, Office documents,
  plain text and CSV files, up to 25 MB each and 50 per feature. Turning it off blocks new attachments for everyone
  and keeps existing ones.
- **Enable editing on this layer**: while off, the layer is read-only for everyone except administrators. Under it,
  **Add features**, **Edit attributes**, **Move or reshape** and **Delete features** set what anyone with edit access
  may do, administrators included.
- **Protect this layer from deletion**: while on, nobody can delete it, administrators included.

## Import data

There are two import paths:

- **Catalog → Layers → Import data** (publishers): built for large files, in three steps. **Source**: choose **File**
  or **ArcGIS feature layer URL**. **Target**: **New layer**, or **Existing layer** to add the features to a layer of
  the same geometry type. **Options**: read the summary and click **Import**. The upload runs as a background job,
  and the dialog shows rows processed, inserted and refused. Closing the dialog does not stop the job, but progress
  is only shown in the dialog. **Cancel import** stops it at the next batch, and rows already imported stay. Rows that
  fail are refused with a reason, a sample is listed (`row …: reason`), and the rest are imported. New layers from this
  path are stored in WGS 84.
- **Import** in a layer's data table (editors): for smaller files. Talus first runs a dry run showing how many features
  are ready, which rows will be skipped and why, and which columns match no field. Click **Import … features** to
  insert the valid rows. Geometry is reprojected to the layer's coordinate system, and columns are matched to fields by
  name.

| Format | Import data | Import (data table) | Needs |
|---|---|---|---|
| Shapefile | Yes | Yes | All parts in one .zip. Choose the coordinate system if the .prj has no EPSG code |
| GeoPackage (.gpkg) | Yes | Yes | |
| GeoJSON (.geojson, .json) | Yes | Yes | |
| CSV | Yes | Yes, point layers only | A lon/lat pair (WGS 84), or an x/y or easting/northing pair plus a chosen coordinate system |
| KML / KMZ | Yes | No | Coordinates. Overlays and network links are skipped |
| GeoParquet | No | Yes | WKB geometry and an EPSG coordinate system |
| ArcGIS feature layer or GeoJSON URL | Yes | No | A public service that allows browser access. A one-time copy, not a link. ArcGIS date fields are copied as dates (the day in UTC); the time of day is not kept |

A geometry-less table can't take a file import. Add rows with **+ Add row**.

## Work in the data table

Click a layer's name in **Catalog** to open it, with **Table** and **Fields** views.

- **Columns** shows or hides columns. **Filters** builds conditions; click **Apply**, or **Clear all**. Click a column
  header to sort ascending, again for descending, and a third time to clear. Set **Rows** per page (up to 5,000).
- A value that is a date, or a date and time, is shown in your browser's date format and without seconds: *Sep 26, 2024*, or *Sep 25, 2024, 8:52 PM*. A time stored with a time zone is shown in your own time zone, so the day can differ from the stored one. A time stored without a zone is shown as written, and a date on its own never shifts. Hover over the value to see it exactly as stored. Filters, sorting, **Export** and the edit forms use the stored value. A record's detail and a map's pop-ups show dates the same way.
- The **Export** menu downloads **CSV**, **GeoJSON** (always WGS 84) or **GeoPackage** (layers only). Under **Output
  CRS** you can reproject CSV and GeoPackage. With rows selected, only those export. Otherwise the export follows your
  filters.
- **Calculate** writes into one owner-defined field: **X coordinate**/**Y coordinate** for points, **Length** for
  lines, **Area** or **Perimeter** for polygons (geodesic, in the unit you pick), or **Set value** for any layer. It
  applies to the selected rows, or to all filtered rows. Click **Preview count**, then **Apply**.
- For a table, **+ Add row** opens the row form. Open a row and use **Edit this row** to change it, or **Delete row**.
  Features on a layer are edited on a map (`guide-maps.md`).

## Read a record's detail

Clicking a row opens its detail: a location line (the point, or the center of the extent, in WGS 84), every attribute,
**Attachments** with enlarge and download, one section per relate listing the related rows, and **History**.
**History** shows each change: when, who, from which client, and each changed field's old and new values. It is shown
to signed-in users who can view the layer, never to anonymous visitors.

## When someone else saved first

Every record carries a version, and a save based on an outdated version is refused rather than overwriting the other
person's work (see `faq.md`). On a map, you see *"This … changed while you had it open"*, with the choice of
**Discard mine & reload** or **Overwrite with mine**. For a move the first choice reads **Cancel**; for a delete,
**Cancel** or **Delete anyway**. In a table's row
form you see *"Someone else changed this row — close and reopen it to edit the latest version."*

## Create a view

A view is a read-only, separately shared lens on one layer. In the source layer's **Manage** → **Views**, tick the
**Columns to expose**, optionally **Filter rows** (rows must match all conditions) and **Join a table**, enter a
**Title**, and click **Create view**. The view appears in the Catalog marked **View** and is shared on its own. Its rows
can't be edited, and the view itself can't be changed after creation: to change it, delete it and create a new one.

## Add a reference

A reference points at a service another organization hosts: an ArcGIS feature or image service, a GeoJSON URL, or XYZ
tiles. In **Catalog** → **References**, click **+ New reference**, paste the URL, click **Check**, then **Create
reference**. Nothing is copied. To copy the data into Talus instead, use **Import data**.

## Delete and recover

Use the delete button on the Catalog row. Deleting a layer warns *"The layer is released and its data is wiped. This
can’t be undone."* Deleting a view removes only the view. Talus has no recycle bin. Whoever runs the server can
bring a single deleted layer back from a backup taken before the deletion, with its rows, fields, settings and
sharing; its attachments and its change history do not come back (`dr-restore-runbook.md`).

## Limits, and what Talus deliberately does not do

- **Import data** limits on a standard install: a 2 GiB upload, 4 GiB unpacked, 2 million features per job. A URL
  copy is built in your browser and refused past 512 MB.
- **Import** in the data table: 50 MB per file, 50,000 features per upload. **Add from file** has the same limits for a
  Shapefile or GeoPackage. A zipped Shapefile may unpack to at most 256 MB, and a GeoPackage's layer must be an
  ordinary table, not a view or a table with computed columns. It sends GeoJSON and CSV in batches.
- Exports stop at 250,000 features, and an export too large to build at once is refused with a request to
  filter it. **Calculate** works on up to 50,000 rows and has no formulas or expressions.
- Geometry is two-dimensional point, line or polygon. Rasters are a separate item type (`guide-imagery.md`).
- No versioning (no branches, reconcile or post) and no topology rules.
- Field names and types are permanent. Relates create no database constraints.
- Views are read-only and can't be edited after creation.
- Deleting is permanent inside the portal.

## If something goes wrong

- *"This file doesn't say which coordinate system it uses. …"*: the **Options** step of **Import data** shows the
  coordinate-system search. Pick the system and click **Import** again.
- *"source geometry is …; the target layer is …"*: append to a layer of the same geometry type, or import as a new
  layer.
- *"these coordinates look like longitude/latitude degrees …"*: the chosen system is wrong. Choose WGS 84 (EPSG:4326).
- *"This service needs a sign-in. Talus holds no ArcGIS token …"*: only public services can be copied. Export a
  Shapefile or GeoPackage from the source and import the file.
- *"file too large (max 50 MB)"*: use **Import data** instead. On a map, **Add from file** shows an **Open Import
  data** button when a file is too large, or has too many features, for it.
- *"field '…' has data; retire it (set active=false) instead of deleting"*: click **Retire**.
- *"field '…' is used by view layer(s): …"* or *"… view layer(s) depend on this layer. Delete them first, then release
  this layer."*: delete those views first.
- *"This layer is protected from deletion. …"*: turn off delete protection in **Manage** → **Settings**.
- *"This layer does not allow adding features."* (or editing attributes, moving or reshaping, deleting): the owner's
  settings forbid it. Ask the owner.
- *"Export INCOMPLETE: …"*: narrow the export with a filter or selection and export the rest separately.
- *"attachment too large (max 25 MB)"* or *"feature already has the maximum 50 attachments"*: shrink or remove files.

## Related guides

- `guide-portal.md`: sharing, metadata and Open Data
- `guide-maps.md`: maps, styling and editing on the map
- `guide-geocoding.md`: turning addresses into points
- `guide-imagery.md`: rasters and imagery
- `guide-administration.md`: users, roles and backups
- `guide-databases.md`: pulling tables from a SQL database
- `connect-desktop.md`: QGIS, ArcGIS Pro and QField
- `api-guide.md`: scripting against the API
