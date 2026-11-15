# Dashboards (Talus+)

A dashboard is a single page of live widgets (numbers, charts, gauges, tables, filters and maps) built over the layers you already keep in Talus. Every widget reads its data at the moment someone opens the dashboard, with that person's own permissions. Dashboards is a Talus+ module: it appears only on a Talus+ deployment whose license includes dashboards.

## Before you start

- **The data must be a Talus layer**: an upload, a database pull or a view. A **Reference** to an outside service never passes through Talus, so it is not offered. To chart one, import a copy (**Catalog → Import data → ArcGIS feature layer URL**).
- **Share the layers first.** A dashboard grants no access of its own. Each person sees only the layers and web maps already shared with them.
- **Creating needs the publisher or admin role.** Viewers and editors can open dashboards shared with them.
- **Save a web map first if you want a map.** Once a dashboard has a map widget, every other widget's layer picker starts with that map's layers.

## Widget types

| Widget | What it shows | What it needs |
|---|---|---|
| Map | A saved web map you can pan and click | A web map you can open |
| Indicator | One number, shown large | A layer and an aggregation |
| Chart | A bar, line or pie chart of a value per category | A layer, an aggregation and a **Group by** field |
| Gauge | One number on a dial, with an optional target mark | A layer, an aggregation and a scale |
| List | A table of features, one page at a time | A layer and at least one column |
| Details | The fields of the feature selected on a map or list | A layer |
| Filter | Filter rows and preset switches | A layer and at least one field |
| Text | Headings, paragraphs, lists and links | Nothing |
| Legend | The live legend of one map widget | A map widget on the dashboard |

## Find your dashboards

Open **Catalog** and choose the **Dashboards** item type. The list shows **My dashboards** and **Shared with me**, each row with its sharing level and owner. On your own dashboards, publishers and administrators get **Edit**, **Share** and **Delete**. Administrators also see everyone else's dashboards under **Shared with me**, and can open one and click **Edit**.

## Create a dashboard

1. In the top navigation, open **New** and choose **Dashboard**, or open **Catalog**, then **Dashboards**, and click **+ New Dashboard**.
2. Type a name in **Dashboard title**. You can also fill in **Short description (optional)**.
3. Add widgets with the toolbar buttons: **+ Map**, **+ Indicator**, **+ Chart**, **+ Gauge**, **+ List**, **+ Details**, **+ Filter**, **+ Text**, **+ Legend**.
4. Click **Create dashboard**. The button stays disabled until the dashboard has a title and at least one widget. After the first save the button reads **Save**, and a **View** link opens the finished page.

Talus does not save automatically. Changes are kept only when you click **Create dashboard** or **Save**.

## Arrange widgets

- **Move** a widget by dragging it. **Resize** it from its right edge, bottom edge or corner. The grid is 12 columns wide. While you resize, a label shows columns × rows and the share of the width, for example `6 × 4 · 50% wide`.
- **Lock** a widget with the padlock button (**Lock size and position**).
- **Change** a widget's settings with **⚙**, then click **Apply**.
- **Remove** a widget with **✕**. There is no confirmation, but nothing is saved until you click **Save**.

## Choose a layer

Every widget except Map, Legend and Text starts with **Layer**. With a map on the dashboard, the list reads **Showing this dashboard's map layers.** and offers **Search all layers…**; without one, you search every layer you can see. Click **change** to pick a different layer.

## Add an indicator

1. Click **+ Indicator** and choose the **Layer**.
2. Choose the **Aggregation**: `count` (features), `sum` or `avg` (number fields only), `min` or `max` (also dates and text).
3. For anything but `count`, choose the **Field**.
4. Optionally type a **Label**. It becomes the widget's title.
5. Click **Add widget**.

An indicator shows `—` when there is no value.

## Add a chart

1. Click **+ Chart**, choose the **Layer**, then the **Chart type**: `bar`, `line` or `pie`.
2. Choose the **Aggregation**, and the **Field** if it asks for one.
3. Choose **Group by**: the field whose values become the bars, points or slices.
4. For bar and pie charts, you can click **Load categories…** under **Colors** and set a color for any category. Only the categories you change are saved. The rest follow the theme palette. **Reset to palette** undoes your changes.
5. Optionally add a **Label**, then click **Add widget**.

A chart shows at most the 50 largest groups (**top 50 groups shown**). Groups run from largest value to smallest, line charts included.

## Add a gauge

1. Click **+ Gauge** and choose the **Layer**, **Aggregation** and, if asked, the **Field**.
2. Set the **Scale** from a minimum to a maximum. The maximum must be larger than the minimum.
3. Optionally set a **Target**. It must lie within the scale, and it is drawn as a second, thinner needle.
4. Optionally add a **Label**, then click **Add widget**.

## Add a list

1. Click **+ List** and choose the **Layer**.
2. Under **Columns**, tick the fields to show. You can choose up to 30.
3. Optionally choose **Sort by** and a **Direction** (**ascending** or **descending**).
4. Set **Rows**, the page size, from 1 to 5,000 (the default is 25). Above 1,000 the dialog warns that large lists can slow the dashboard, because every filter change fetches the list again.
5. Optionally add a **Label**, then click **Add widget**.

Below the table, a count such as `1–25 of 113` appears, with **‹** and **›** to page. Clicking a row selects that feature; clicking it again clears the selection.

## Add a details card

1. Click **+ Details** and choose the **Layer**. This must be the same layer the map or list shows.
2. Optionally tick **Fields**. If you tick none, the card shows every field.
3. Optionally add a **Label**, then click **Add widget**.

Until something is selected, the card reads **Select a feature on the map or list**.

## Add a map

1. Click **+ Map** and type in **Search your maps by title…**.
2. Pick the web map, then click **Add widget**.

To change the map's layers or styling, edit the web map itself.

## Add a legend

**+ Legend** is available once the dashboard has a map. Choose which map it describes under **Map widget** (**Map 1**, **Map 2**, and so on). The legend updates as the map changes.

## Add text

Click **+ Text** and type in the box. The toolbar inserts **Bold**, **Italic**, **Heading**, **List item** and **Link** markup, or type it yourself: `**bold**`, `*italic*`, `# Heading`, `## Subheading`, `- list item`, `[text](https://…)`. Only `http` and `https` links work. There are no images, tables, fonts or colors. The limit is 8,000 characters.

## Add a filter

1. Click **+ Filter** and choose the **Layer**.
2. Under **Fields**, tick up to 10 fields. Each one becomes a filter row: a choice list for text fields, or a min/max range for number and date fields.
3. Optionally click **+ preset** to build a named switch: a name, then conditions of a field, an operator (`eq`, `ne`, `ilike`, `gte`, `lte`, `lt`) and a value. **+ condition** adds more; all must be true together. A date value can be `$today` or `$today-30d`. Click **Add preset**. Up to 10 presets, 10 conditions each.
4. Optionally add a **Label**, and tick **Start expanded** to open every choice list on load.
5. Click **Add widget**.

## Make widgets filter each other

- **A filter narrows widgets on the same layer**: indicators, charts, gauges, lists and that layer on any map. Widgets on other layers are not affected.
- **Choice lists:** tick values. The row shows **All** or the number selected; **Clear** resets it. At most 50 values are offered (**top 50 values listed**).
- **Ranges:** fill in either end or both. Empty both boxes to clear.
- **Presets** turn on and off with their switch.
- **Selection links the map, list and details card.** Clicking a feature on the map highlights and scrolls to its row in a list on that layer, and a details card on that layer shows it. Clicking a list row does the same and flies the map there. One feature is selected at a time; clicking an empty spot on the map clears it.
- Charts, indicators and gauges respond to filters but are not filter sources. Clicking a bar or slice does nothing.

Viewers can change filters while they look at a dashboard, but their choices are not saved. When you click **Save** in the editor, the filter values set at that moment become the dashboard's starting filters. Preset switches always start off.

## Share a dashboard

1. In **Catalog → Dashboards**, click **Share** on the dashboard's row.
2. Choose a **Sharing level**: **Owner Only**, **Organization Only** (everyone signed in to your organization) or **Public Access** (anyone, no sign-in). You can also tick any **Groups** you belong to.
3. Click **Save**.

**Public Access** works only after an administrator turns on public sharing on the **Sharing & access** page of **Admin**, and some organizations reserve it for administrators. When you make a dashboard public, **Review sharing** lists any map or layer on it that is not yet public. Nothing is published automatically; share each from its own Manage dialog.

**What a viewer sees.** Each widget fetches its data with the viewer's own permissions. If a widget's layer is not shared with the viewer, that widget shows **Not available** and the rest of the dashboard still works. A map widget leaves out layers the viewer cannot see. Anonymous visitors to a public dashboard see only public layers, and coded values appear as codes rather than labels.

## Refresh

Widgets read their data when the dashboard opens, and again whenever a filter changes. They do not refresh on a timer. To see edits made since you opened the page, reload it.

## When the Talus+ term ends

Nothing stops: dashboards keep working, and what lapses is new releases and support. Administrators see a banner on the dashboards list, *Talus+ entitlement expired … installed features continue to work; new releases and support require renewal.*, with **Upload renewal license…** and **License details**. If no valid license is present at all, Dashboards disappears from the menus and the Catalog.

## Limits, and what dashboards deliberately do not do

- The aggregations are fixed. There is nowhere to type an expression, a calculated field or SQL.
- Indicators have no number formatting, prefixes, suffixes or color rules. Gauges have no colored thresholds.
- Widgets have no filter of their own. Narrowing happens through a filter widget.
- A dashboard holds up to 30 widgets and up to 20 saved filter conditions.
- No timed refresh, no emailing a dashboard, no click-through to other systems, and no embed code. A public dashboard is opened by its link.

## If something goes wrong

- **The New menu has Dashboard, but you see "This editor isn’t available."** Your account lacks the publisher or admin role. Ask an administrator.
- **"Dashboards aren’t available here."** The module is not licensed on this deployment, or you are not signed in.
- **"This dashboard isn’t available."** It was deleted, or it is not shared with you (or, signed out, it is not public).
- **A widget says "Not available".** Its layer is not shared with you, or was removed. Ask the layer's owner.
- **The details card says "Feature not found".** The feature no longer exists or you cannot read it.
- **Saving shows "Request failed with status 422".** A setting was refused. A common cause is a gauge **Target** outside its scale.
- **"unknown map: …" or "unknown layer(s): …" on save.** A widget points to a map you can no longer open, or to a layer that was deleted. Reconfigure or remove that widget.
- **Delete is grayed out with "Delete protection is on".** The dashboard is protected from deletion. A new dashboard is not protected, and this release has no control in the portal that turns the protection on or off for a dashboard: it can only have been set, and can only be cleared, through the API, by the owner or an administrator.

## Related guides

- [guide-layers.md](guide-layers.md)
- [guide-maps.md](guide-maps.md)
- [guide-portal.md](guide-portal.md)
- [guide-reports.md](guide-reports.md)
- [guide-story-maps.md](guide-story-maps.md)
- [guide-administration.md](guide-administration.md)
- [talus-plus-runbook.md](talus-plus-runbook.md)
