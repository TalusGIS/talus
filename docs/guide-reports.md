# Reports (Talus+)

A report in Talus is a dated document built from your own layers: either one summary document (counts, totals, tables, lists) or one sheet per feature of a single layer, such as inspection sheets or permits. Each document is stamped with its generation time and filters and kept in the portal for download. Reports are a Talus+ module.

## Before you start

- **Reports appear only on a Talus+ deployment licensed for the reports module.** Otherwise there is no Report entry and the pages say "Reports aren’t available here."
- **Where to find it:** saved reports are on the **Reports** tab of **Catalog**; new ones start from **New** → **Report**.
- **Who can do what:**
  - **Create:** Publisher or Admin. Others see "This editor isn’t available."
  - **Edit, share, attach delivery targets, delete:** the report's owner, or an Admin.
  - **Open, run and download:** anyone the report is shared with.
  - **Define delivery targets** (email relay, webhooks): Admins only.
- **A report shows what its owner can see.** Every run (scheduled, manual or a single sheet) reads data as the owner, with the owner's own permissions; an administrator's extra access does not count. The report page says so under **Renders as**. Anyone you share the report with sees the owner's view of the data, so share accordingly.

## Section types

A summary document and a per-feature report offer different sections. There is no chart section.

| Section | Report kind | What it shows | What it needs |
|---|---|---|---|
| **Heading** | both | A heading line | Heading text (up to 200 characters) |
| **Text** | both | Plain text, printed exactly as written | Up to 4,000 characters; no formatting |
| **Indicators** | summary | Up to 8 single numbers, for example "Open work orders" | For each: a layer, an aggregate (count, sum, avg, min, max), a field (except for count), optional filters, a label |
| **Summary table** | summary | One aggregate grouped by a field, one row per value | A layer, aggregate, field, **Group by** field, up to 50 rows, optional filters and label |
| **Feature list** | summary | A table of features with the columns you choose | A layer, up to 30 columns, optional sort, up to 200 rows, optional filters and label |
| **Fields** | per feature | The feature's chosen fields as a label/value table. Coded values print as their descriptions | Up to 30 fields of the report's layer |
| **Photos** | per feature | The feature's image attachments, oldest first | Up to 6 photos; **Small** or **Medium** |
| **Map** | per feature | A map of the feature (see *The locator map*) | Nothing to configure |

## Create a report

1. Open **New** → **Report**, or **+ New Report** on the Reports list.
2. Type a **Report title**. Optionally add a short description and a **Subtitle printed under the title**.
3. Under **What this report is**, choose **Summary document** or **One sheet per feature**.
4. For **One sheet per feature**, choose the **Layer**, add any filters under **Which features**, and set **Per run** (at most 200). Features are rendered in object-ID order. The layer must have an object ID column.
5. Under **Sections**, add sections with the **+** buttons. Reorder them with **↑** and **↓**, change one with **Configure**, and remove one with **×**.
6. Set the schedule and **Keep last** (see *Schedule a report*).
7. Select **Create report**.

Every page carries the organization name (the portal's home page heading) and title at the top, and the generation time, filters and page number at the bottom. Paper is A4 when the organization's default units are metric, US Letter otherwise.

## Add a table section

1. In a summary document, select **+ Summary table**.
2. Under **Layer**, search with **Search your layers by name…** and pick a layer.
3. Choose the **Aggregate**. For **count**, the **Field (optional)** can stay at **All rows**. For **sum** and **avg**, only numeric fields are offered.
4. Choose the **Group by** field and the number of **Rows (≤ 50)**.
5. Optionally use **+ Add filter**. Each filter is a field, an operator (**is**, **is not**, **>**, **≥**, **<**, **≤**, **is one of (a,b,c)**, **is not one of**, **contains**, **matches (LIKE)**) and a value. You can add up to 10.
6. Optionally add a **Label**, then select **Add section**.

A **Feature list** works the same way but asks for **Columns**, a **Sort** (or **Default order**), and **Rows (≤ 200)**. When rows are cut off, the document says "Showing the first N rows."

## Run a report and download documents

1. On the report page, select **Run…**.
2. For a summary document, select **Run**. It uses the current data.
3. For a per-feature report, choose one option:
   - **From the start**: the first features, up to the per-run limit.
   - **Run next batch**: continues after the last object ID of the latest document. This option appears only when features remain.
   - **Object ID range**: no more than the per-run limit.
   - **One feature**: a single object ID.
4. The button shows **Generating…** until the document appears under **Documents**. Select **Download**.

Documents are PDFs. A per-feature report can instead use your own Word template: after creating the report, open **Edit** and use **Upload .docx template…** under **Word template (optional)** (the page lists the allowed placeholders; macro-enabled files are refused; 5 MB limit). Runs then produce one Word document per feature, zipped when there is more than one. **Remove** returns to PDFs. **Recent runs** lists each run's status (pending, running, completed, failed) and trigger (manual or scheduled).

## Schedule a report

1. Open **Edit**. Under **Schedule and retention**, set **Run** to **Daily**, **Weekly** or **Monthly**. The default is **On demand only**.
2. For weekly runs, choose the day under **On**. For monthly runs, choose a day from 1 to 28 under **On day**.
3. Set **At** and **Time zone** (it starts as your browser's; any standard zone name, such as America/Chicago). Documents are stamped in this zone; a report with no schedule stamps UTC.
4. Choose **Keep last** (1 to 24 documents, default 12). Scheduled documents and the ones people request (**Generate sheet**, **Run now**) are counted separately; older documents of each kind are deleted as new ones arrive.
5. Select **Save**.

Saving never runs a slot that has already passed. A scheduled per-feature run continues after the last object ID the previous scheduled run covered, so it picks up newly added features. If a scheduled run fails, the schedule is **paused** with the reason shown; it resumes when the report is saved again or a run succeeds.

## Send a report by email

A delivery carries the document's facts and a sign-in link, never the document itself. An administrator sets up email once:

1. Go to **Admin** → **Delivery targets** (in the **Talus+** group) and select **+ Add mail relay** (one per deployment).
2. Enter **Name (what report owners see)**, **SMTP host**, **Port**, **TLS**, **Relay username**, **Relay password** and **From address**. TLS and relay sign-in are required.
3. Select **Test** (a pass reads "relay accepted sign-in"; nothing is sent), then **Add**.
4. Under **Delivery policy**, review **Allowed recipient domains**. The restriction is on by default, seeded from your users' email domains.

The report owner then attaches it:

1. On the report page, under **Deliveries**, select **Add targets** (or **Edit targets**).
2. Choose the relay from **+ Add a delivery target…** and type **Recipients (comma-separated)**, then select **Save**.

Each new document then sends one plain-text message with the subject "[organization] Report ready: title".

**If email is not configured**, there is no email target to choose and nothing is sent; documents stay in the portal. The panel says "No delivery targets exist yet — an admin adds them under Administration → Delivery targets."

## Send a report to a webhook

1. An administrator selects **+ Add webhook** on **Delivery targets** and enters **Name (what report owners see)** and **HTTPS URL**, such as `https://hooks.example.org/talus`.
2. Select **Generate secret** and copy it to the receiver now (it is shown only while the form is open), then **Test** and **Add**.
3. The report owner attaches the webhook in the report's **Deliveries** panel, as for email.

**Where a webhook may point:** https only, to a public address; private and internal addresses are refused. An administrator may also turn on **Only allow webhooks to these hostnames** (exact names). With that list on, **Let these hostnames resolve to private (on-premises) addresses** allows a receiver on your own network; loopback, link-local and cloud-metadata addresses stay refused.

**What the receiver gets:** one signed JSON POST per document (event `report.artifact_generated`, Standard Webhooks headers `webhook-id`, `webhook-timestamp`, `webhook-signature`) with the report ID and title, the document's format, size, SHA-256, generation time, feature count and object-ID span, a sign-in link to the report, and the organization name. A 2xx answer counts as delivered; redirects are not followed. Teams and Slack expect their own card format, so point the webhook at an automation tool or relay.

**Deliveries panel:** everyone who can open the report sees recent deliveries with **Status** (pending, sent, failed), **Outcome** and **Detail**. Each delivery is tried up to three times (now, after 1 minute, after 5 more), showing "retrying (attempt N of 3)" between tries. There is no manual retry, and a failed delivery never affects the document.

## Print a record sheet for one feature

Once a per-feature report exists for a layer, anyone who can open that report can make a sheet for one feature:

- In the record drawer, select **Generate sheet**. If several reports apply, choose one first.
- In the map's identify popup, select the sheet icon beside the title.

When the sheet is queued, select **Sheet queued — open report** to download it from the report page.

### The locator map

A **Map** section uses the map you are looking at when you make a sheet this way. In every other run, Talus draws the feature on the downloaded street basemap (one fixed style, scale bar and attribution, no labels or legend). Without geometry or a downloaded basemap it prints "Map not available (no geometry or no basemap)." A portal that only streams its maps prints that until an area is downloaded.

## Share a report

1. Select **Share** on the report page, or on its row in the list.
2. Choose **Owner Only** or **Organization Only**, and tick any **Groups**. Non-admins can choose only groups they belong to.
3. Select **Save**. Reports can never be shared publicly.

## Limits, and what reports deliberately do not do

- The layout is fixed. There is no page designer, and no chart, logo or custom header section.
- Filters are chosen from lists. You cannot type an expression or SQL.
- At most 200 features per run and 20 sections per report; deliveries never attach the document.
- A run that hits a problem in any section fails as a whole. Talus does not produce a partial document.
- Delete is permanent and removes all of the report's generated documents.
- When the Talus+ term ends, reports keep working, schedules and deliveries included; administrators see a "Talus+ entitlement expired" banner, and new releases and support require renewal ([talus-plus-runbook.md](talus-plus-runbook.md)).

The content and member reports under **Admin** are a separate core feature, covered in [guide-administration.md](guide-administration.md).

## If something goes wrong

- **"the report owner cannot view layer(s) … share the layer to the owner or a group first"** when saving: share the layer to the owner; an admin role alone is not enough.
- **"not authorized to create reports"**: you need the Publisher or Admin role.
- **A paused badge reading "last scheduled run failed: …"**: fix the cause named, then save or run the report.
- **"owner_unavailable: the report owner is disabled or removed"**: runs fail while the owner's account is disabled or removed. Ask an administrator.
- **"A range may span at most N object IDs per run."**: use a narrower range.
- **"unknown placeholder(s): …"** or **"macro-enabled templates are not accepted"**: fix the Word template and upload it again.
- **"'name@example.org' is outside this organisation's allowed recipient domains …"**: ask an admin to add the domain.
- **"no recipients allowed by the org's recipient policy"** or **"target disabled"** in Deliveries: ask an admin to review the policy or the target.
- **"destination is not on the admin hostname allow-list"** or **"destination resolves to a private or reserved address"**: the webhook address breaks the rule above; ask an admin.
- **"N photo(s) could not be included."**: some attachments were missing or too large; the rest of the sheet is complete.

## Related guides

- [guide-layers.md](guide-layers.md)
- [guide-maps.md](guide-maps.md)
- [guide-portal.md](guide-portal.md)
- [guide-dashboards.md](guide-dashboards.md)
- [guide-administration.md](guide-administration.md)
- [talus-plus-runbook.md](talus-plus-runbook.md)
- [security-overview.md](security-overview.md)
