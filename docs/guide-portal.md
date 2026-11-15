# Using the portal

This guide covers the everyday parts of the Talus portal: signing in, the home page, the catalog, sharing, groups, Open Data and your profile. Building layers, authoring maps and the Administration pages have their own guides, listed at the end.

## Who can do what

Your role appears beside your name at the top right.

| Role | What it adds |
|---|---|
| **Viewer** | Open, favorite and download the items shared with you; connect desktop GIS. |
| **Editor** | Viewer, plus editing features in layers shared to your organization or a group, where editing is on. |
| **Publisher** | Editor, plus creating layers, tables, maps, rasters, references and groups. |
| **Admin** | Everything, on every item, plus the **Admin** pages. |

Only an item's owner or an administrator can manage it (sharing, metadata, settings, transfer, delete).

| Sharing level | Who can view the item |
|---|---|
| **Owner Only** | The owner (administrators can always see every item). |
| **Groups** (added to any level) | Members of each group you tick. |
| **Organization Only** | Everyone signed in to your organization. |
| **Public Access** | Anyone on the internet, with no sign-in. Public access never lets anyone edit. |

## Sign in and out

1. Open your portal's address (for example `https://gis.example.org`) and choose **Sign in**. The sign-in page opens; enter your username and password, or choose your organization's sign-in button if the page shows one.
2. To sign out, click your name at the top right and choose **Sign out**. Talus first clears its cached data from that device.

The same menu holds **Profile**. Where public access is on, visitors who are not signed in see **Home** and **Open Data**, a **New map** link that leads to sign-in, and a **Licenses** link beside **Sign in**.

## Use the home page

**Home** shows the **Featured** gallery: maps and layers an administrator has shared to the **Home (Featured)** group, filtered to what you may view. The **Open Data** card opens the public catalog.

## Find items in the catalog

1. Choose **Catalog**.
2. Pick an item type: **Layers**, **Tables** (data with no geometry), **Maps**, **Rasters**, **References** (services another organization hosts), **Field projects**, and, for publishers, **Databases** (see `guide-databases.md`). Where those modules are licensed, **Dashboards**, **Stories** and **Reports** (Talus+) also appear.
3. Narrow it with **All**, **Owned by me** or **Shared with me**, **Favorites** (star a row to add it), the **Sharing** filter and **Search by name…**. **Modified** flips the sort order.

Each row's sharing icon names its level on hover: a person is **Owner**, several people **Groups**, a building **Organization**, an amber globe **Public**. The broadest level wins. Also:

- An orange layer icon means "Empty layer — no features".
- A **View** tag marks a view layer (a filtered copy of another layer).
- A padlock in place of the trash can means the item is delete-protected.

Click a row to open it: a layer or table opens its data view, a map the map viewer, a raster a new map.

## Manage an item

Click the gear (**Manage layer**, **Manage map**, …) on a row. If you can't manage the item, a notice names its owner instead.

Layers, maps and rasters all have **Sharing**, **Metadata** and **Settings** tabs. Layers and rasters add **Open Data**; layers add **Use in…** and, depending on the layer, **Fields**, **Relates** and **Views** (see `guide-layers.md`). A layer's or map's **Sharing** tab starts with a link and a **Copy** button; the link opens only for people who can already view the item.

## Share an item

1. Open **Manage** and stay on the **Sharing** tab.
2. Under **Sharing level**, choose **Owner Only**, **Organization Only** or **Public Access**.
3. Under **Groups**, tick any groups that should also see it. You can share only to groups you belong to; administrators can share to any group.
4. Choose **Save**. The row's sharing icon updates.

**Public Access** stays unavailable until an administrator turns on **Offer public access from this organization** under **Admin → Sharing & access** (off by default). Even then, only administrators can make items public unless **Allow non-admin owners to share their layers and maps to Public Access** is also ticked.

When you make a map public and some of its layers or rasters are not, **Review sharing** lists them under **Make these public too**; otherwise the public sees a blank map. Choose **Update sharing** or **Don’t update sharing**. Items you can't manage appear under **Needs an administrator**. A public map also offers **Copy embed code** for another website.

## Create a group and share to it

1. Choose **Groups**. **My groups** lists yours; **My organization’s groups** lists the rest.
2. Publishers and administrators choose **+ Create group**, enter a **Name**, and choose **Create group**. You become owner and first member.
3. Open the group, go to **Members**, choose **+ Add members**, and choose **+ Add** next to each person. The owner or an administrator can also **Make owner** or **Remove** a member.
4. To share, go to **Content**, choose **+ Add items**, and choose **+ Add** next to a layer, map or raster you own. Any member may add their own items. Or tick the group on an item's **Sharing** tab.

Only members, the owner and administrators can open a group's page. On **Overview**, the owner can **Edit details** (including **Protect from deletion**) or **Delete group**. Deleting a group ends access through it; the items stay.

## Fill in metadata

On the **Metadata** tab, fill in **Title**, **Abstract**, **Keywords (comma-separated)**, **Topic category (ISO)**, **License / use constraints**, **Point of contact — name** and **Language**, then choose **Save metadata**. The spatial extent fills in from the geometry when you save, and the banner lists what is missing. Metadata is required only for Open Data, never for ordinary sharing. Export it with **QGIS .qmd** or **ISO 19139**.

## Publish a layer or raster to Open Data

1. Check that you may publish: public access is on (see "Share an item"), you manage the item, and you are an administrator, or a member of the **Open Data** group in an organization that lets non-admin owners share publicly.
2. Complete the metadata. When it is complete and you are allowed to publish, the **Metadata** tab offers **Publish to Open Data**.
3. On the **Open Data** tab, choose **Curate into Open Data…**, read the warning, and choose **Yes, publish publicly**. This makes the item public and lists it; the action is audited.
4. To take it back out, choose **Remove from Open Data**. This does **not** make it private. Change that on **Sharing**.

The **Open Data** page lists published layers, tables and rasters for anyone, with no sign-in. Its search covers titles, abstracts and keywords. A visitor can preview a dataset and download it:

- **Layers:** CSV, GeoJSON, Shapefile and GeoPackage.
- **Tables:** CSV only.
- **Rasters:** GeoTIFF and Cloud-Optimized GeoTIFF, but only while **Allow download / export** is on in the raster's **Settings** tab.

Each dataset page also shows its OGC API address and metadata downloads. The **DCAT feed** link serves the catalog to open-data aggregators.

## Protect an item from deletion

On **Settings**, tick **Protect this layer from deletion** (or map, or raster). Until it is turned off, nobody can delete the item, administrators included.

## See where an item is used

On a layer's or raster's **Settings** tab, **Used in** lists the maps, views and geocoders that use it. It also says if the item is **Listed in Open Data** or **Featured on Home**. Deleting an item used by maps triggers a warning first. Both lists show only items you can view; an administrator can give the complete picture.

## Transfer ownership

1. On **Settings**, under **Transfer ownership**, type part of a person's name in **Search members by name…** and pick them.
2. Choose **Transfer**, then confirm.

The new owner manages the item from then on; its sharing does not change. Administrators can always transfer. Owners can transfer only when an administrator has ticked **Allow members to transfer ownership of their own layers and maps**. Items with no owner can't be transferred.

## Connect desktop GIS or a script

The plug icon on a layer or raster row, or a layer's **Use in…** tab, opens a wizard. Choose **Next**, then a goal such as **View / analyze in QGIS**, **Edit in ArcGIS Pro** or **Download a copy** (the only one for tables). Details: `connect-desktop.md`.

## Use your profile

Choose your name, then **Profile**.

- **Change your password:** fill in **Current password**, **New password** and **Confirm new password**, then choose **Change password**. Accounts that sign in through another identity provider change their password there.
- **Create an access token:** choose **+ Create token**, give it a **Name**, set an optional expiry in days, and choose **Read-only** or **Read & edit**. Copy it at once; it is shown only once. Desktop apps sign in with username `talus` and the token as password. **Revoke** stops it immediately. A token sees what your account sees, never with administrator rights.
- **Download a connection file:** under **Vector layers**, **Imagery** or **Raster values**, choose **QGIS** or **ArcGIS Pro**. Import the file once. Every layer you can view then appears, including ones shared with you later. (For raster values in ArcGIS Pro, choose **Copy URL for Pro** instead.)

## Limits, and what the portal deliberately does not do

- Groups have no join requests or invitations. A group's owner or an administrator adds members directly.
- The catalog search matches item names only.
- Maps can't be published to Open Data. Only layers, tables and rasters can.
- Public sharing never grants editing, and copies taken while an item was public can't be recalled.
- Delete protection has no administrator override, by design.
- Group pages are closed to non-members, even though every group's name is listed.

## If something goes wrong

- **"You don’t have permission to manage this layer — only its owner … or an administrator can."** Ask the named owner, or an administrator.
- **"Public sharing is turned off for this organization …"** An administrator must turn on public access on **Admin → Sharing & access**.
- **"Only administrators can enable this in your organization."** Ask an administrator to publish it.
- **"You aren’t a member of any groups to share with."** Ask a group's owner to add you.
- **"This group doesn’t exist, or you don’t have access to it."** Only members can open a group's page.
- **"This map is protected from deletion …"** (layers say much the same; rasters say "raster is delete-protected"). Untick delete protection on the **Settings** tab, then delete.
- **"Only Open Data members or administrators can publish datasets."** Ask an administrator to add you to the Open Data group.
- **"Complete this layer’s metadata before publishing."** Choose **Complete metadata…** and fill in the fields it lists.
- **"That current password is not correct."** Try again. If you no longer know it, ask an administrator to reset it.
- **"Your session ended"**: choose **Sign in** again. **"Couldn’t reach the sign-in service"**: choose **Try again**, and tell your administrator if it keeps happening.
- **"Too many downloads — please wait a minute and try again."** Wait, then retry.

## Related guides

`guide-layers.md` · `guide-maps.md` · `guide-geocoding.md` · `guide-imagery.md` · `guide-administration.md` · `guide-databases.md` · `guide-dashboards.md` · `guide-reports.md` · `guide-story-maps.md` · `guide-helper.md` · `connect-desktop.md` · `api-guide.md`
