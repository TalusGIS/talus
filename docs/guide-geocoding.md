# Finding addresses and features

This guide covers address search on the map, feature search in your own layers, and batch geocoding a file or a
table of addresses into a point layer. It also covers how an administrator sets up the geocoders (locators) these use.

## Kinds of geocoder

| Kind (the **Type** in the form) | What it is | Outbound network? | Key? |
|---|---|---|---|
| Shipped **OpenStreetMap** locator | The public OpenStreetMap Nominatim service, added to a new install | Yes, public internet | No |
| **Nominatim (OSS)** | Any Nominatim service, including one you host | Yes, to wherever it runs | No |
| **ArcGIS GeocodeServer** | An ArcGIS REST geocode service, such as your county's, or Esri's ArcGIS World Geocoding Service with a token | Yes | Only if secured (a token) |
| **Your address layer (this portal)** | Your own address-point layer, searched inside Talus | No | No |

Any signed-in user can search addresses and use Locate on layers they can view. Publishers can also batch geocode.
Only administrators manage geocoders.

## Add a geocoder

1. Go to **Admin**, then **Geocoders**, and click **+ Add geocoder**.
2. Enter a **Name** (users see it in the map's **Search in** picker) and choose the **Type**.
3. For ArcGIS or Nominatim, enter the **Service URL** (starting `http://` or `https://`). Leave **Out SR (EPSG)** at 4326.
4. Optionally fill **Limit to extent (optional)** with `xmin, ymin, xmax, ymax` in longitude and latitude. With an
   extent, results stay inside it; without one, results are limited to the area on screen.
5. For a secured ArcGIS service, paste the token into **Token (optional — for a secured service)**.
6. Leave **Enabled** checked, click **Test**, then **Add**.

**Test** runs one sample search: "✓" and a result count means the service answered (zero results still counts);
"✕ no response" means it did not, and hovering shows why. A failed test does not block saving.

## Order, disable, edit or remove a geocoder

- **Order:** use the up and down arrows. With **Search in** set to **All**, Talus asks the top geocoder first and uses
  the first that returns results.
- **Disable:** click the **Enabled** badge. A **Disabled** geocoder is not used and is hidden from **Search in**.
- **Edit:** click **Edit**, then **Save changes**.
- **Remove:** click **Remove** and confirm; this affects everyone's map search. The shipped OpenStreetMap geocoder is
  not added back once removed.

## How a stored token is handled

You enter a token once. It is "Stored encrypted on the server and never shown again — only that a token is set (and its
last 4 characters)." Editing later shows **Token set (…abcd)** with **Replace** and **Clear**. Tokens apply only to the
ArcGIS type.

A **credential unavailable** badge means the server can no longer read the stored token (for example, its encryption key
changed). Talus skips that geocoder until you re-enter the token. Testing a saved geocoder with a token uses its saved
settings, so save URL changes first.

## Build a geocoder from your own address points

Load your address points as a point layer first (see `guide-layers.md`). View layers cannot be used.

1. Click **+ Add geocoder** and choose **Your address layer (this portal)**.
2. Pick the **Address layer**.
3. Under **Address fields (in display order)**, use **+ Add field…** to add the street-address fields, house number
   first: one full-address field, or a number field then a street field. Up to six.
4. Optionally, under **Locality fields (city, ZIP — optional)**, add city or ZIP fields. They appear in results and break
   ties between the same street address in two towns, but never count toward the score, so keep them out of the address
   fields.
5. Note the form's warning: every signed-in member can search and see these addresses, whatever the layer's sharing.
6. Click **Test**, then **Add**.

How it matches:

- Case, accents and extra spaces are ignored.
- Matching is fuzzy, so small typos still match. Abbreviations are not expanded: "St" and "Street" are different words,
  and a spelling that differs from your data scores lower.
- Suggestions start at the third character; addresses beginning with what you typed come first.
- The score (0 to 100) measures how similar the text is. Picking a suggestion is an exact match (100).
- **What’s here?** returns the nearest address point within 100 meters.

It needs no outside connection. A layer in use by this geocoder cannot be deleted.

## Search for an address on the map

1. Click the pin icon (**Search address**) at the lower left of the map.
2. Type in **Find address or place**. Suggestions appear after three characters.
3. Pick a suggestion, or press Enter. The map moves to the best result and drops a marker.
4. With more than one geocoder, use **Search in** to choose one, or **All**.

Address search needs you to be signed in and is not shown in embedded maps. The shipped OpenStreetMap geocoder gives no
suggestions as you type (its usage policy forbids it): press Enter.

To get the address at a point, right-click the map and choose **What’s here?**. It shows the coordinates and the
nearest address, or "No address found", using the geocoder chosen in **Search in**.

## Find features in your layers

1. Click the magnifier (**Find features in your layers**) in the map's right-hand rail.
2. Type at least two characters in **Find features by attribute**. Talus searches the attributes of the visible layers
   you can view.
3. Click a result to zoom to it. Results are grouped by layer, at most 50 per layer.

- Plain words match whole words. Use quotes for a phrase, `OR` for either term, `-word` to exclude.
- `*` matches any characters and `?` one character, anywhere in a value; you need three characters in a row without a
  wildcard. These results are badged **partial**.
- With fewer than five exact matches, Talus adds typo-tolerant matches badged **similar**, with a percentage (not for
  searches using quotes, `OR` or `-word`).

Each result is named by the layer's display field, or the first text field if none is set. The layer's owner or an
administrator sets it in the layer's attribute table (see `guide-layers.md`): click the star beside a column heading
(**Set as the display field — names each feature in popups and search**) or in the **Columns** list; click again to
clear. It applies for everyone.

## Batch geocode a file or a table of addresses

1. In the Catalog, on the **Layers** tab, click **Geocode addresses** (publishers and administrators).
2. **Locator:** choose a geocoder. The dialog says "Free — no credits." or "Consumes credits (estimate shown at the
   Output step)."
3. **Input:** answer *Where are the addresses?*
   - **A file — a CSV, or a KML/KMZ of addresses.** Choose an **Address CSV or KML/KMZ**. A CSV needs a header row; a
     KML/KMZ may carry addresses instead of coordinates (a Google My Maps export).
   - **A table or layer already in Talus.** Choose it under **Table or layer**. The list holds the tables and layers
     you can see; tables are listed first. A view layer is not in the list: use the table or layer it is built on.
4. **Fields:** pick the address column, or several to join in the order you pick them. For a table, the fields are
   shown under their labels.
5. **Output:** name the layer and set the **Match threshold** (30 to 100, default 85). For a credit-consuming service,
   tick the box accepting the estimated credits.
6. **Country** (ArcGIS and Nominatim only): optionally limit results to countries.
7. Click **Geocode**.

Geocoding a table makes a **new point layer, copied at that moment**. The table itself is not changed, and the new
layer does not update when the table changes or when a database pull refreshes it. Run it again to get a fresh copy.

The run happens in the background, one run at a time. The output layer fills in as rows finish; **Cancel run** keeps
rows already done.

### Read the results

The output point layer holds **Geocode input**, your original columns (as text), **Match status**, **Match score**,
**Matched address**, **Matched locator** and **Match type** (ArcGIS only).

| Match status | Meaning | Placed on the map? |
|---|---|---|
| `matched` | Best score at or above your threshold | Yes |
| `low_score` | Best score below your threshold | Yes, at the best guess |
| `tie` | Top two candidates within 5 points | Yes, at the best guess |
| `no_match` | No candidate | No |
| `skipped` | Blank address | No |
| `canceled` | Run canceled first | No |

Check **Match status** before trusting a point. Unplaced rows are in the attribute table, not on the map. When a run
completes with unmatched rows, **Re-run misses** retries the `no_match`, `low_score`, `tie` and `canceled` rows with the
same geocoder and threshold. It appears only in the dialog, right after the run.

## Air-gapped deployments

The shipped OpenStreetMap geocoder cannot be reached without internet access. Disable or remove it, and use your own
address layer or a Nominatim or ArcGIS service on your network. `security-overview.md` lists the outbound connections a
deployment can make.

## Limits, and what it deliberately does not do

- Batch row limits: 50,000 for your own address layer, 5,000 for ArcGIS, 1,000 for Nominatim; files up to 50 MB.
- The public OpenStreetMap service cannot batch geocode. An ArcGIS service must support batch requests.
- Your own address layer finds only points that exist; it does not estimate locations between addresses.
- Address search goes to the single best result. There is no routing.
- External providers' terms of use and charges are your organization's responsibility.

## If something goes wrong

- **"Address search is unavailable — every locator failed; tell your administrator"**, or a message beginning
  **"address search is unavailable: every configured locator failed to answer"**: an administrator should **Test** each
  geocoder.
- **"No match found"**: try fewer words, or another geocoder in **Search in**.
- **"✕ no response"** on **Test**: check the URL and token.
- **"This locator needs a stored token. Add one in Admin → Geocoders, then try again."**: an administrator adds it.
- **"OpenStreetMap's public geocoder prohibits bulk use…"** or **"This locator only does single-address lookups…"**:
  choose another geocoder.
- A message that your rows exceed the locator's row limit: split the file, or for a table use your own address layer as
  the geocoder.
- **"Locator(s) … search this layer's addresses. Remove them in Admin → Geocoders first, then delete this layer."**:
  remove the geocoder first.

## Related guides

- `guide-maps.md`
- `guide-layers.md`
- `guide-administration.md`
- `guide-portal.md`
- `security-overview.md`
