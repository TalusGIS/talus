# The Administration pages

This guide walks through the portal's Administration area page by page, in the order its navigation shows them. Only administrators see this area. The **Admin** tab appears in the top bar for administrators only, and anyone else who opens an Administration address is sent to the home page.

Administration opens on **Health**. In the navigation, a count beside **Users** and **Service accounts** shows how many exist. An amber dot means that page has an open issue.

| Page | Purpose |
|---|---|
| Health | Is the portal healthy right now? |
| Backups & recovery | Backup schedule, retention, destination, encryption, the backup list |
| Maintenance | What the automatic clean-up jobs did |
| Usage | Content, sharing, member and activity reports, with CSV download |
| Users | Create, edit, disable, transfer from and delete accounts |
| Service accounts | Credentials for scripts and integrations |
| Notices | The banner shown to everyone signed in |
| Sharing & access | The public-access switch and non-admin policy |
| Home page | The public landing page's text and image |
| Connections | The address desktop clients and scripts use |
| Basemap | The street basemap and the map defaults |
| Geocoders | Address-search locators |
| Database sources | Every database connection and pull |
| License, Delivery targets, AI, AI usage | Talus+ only |

## Health

A status page, not a control panel. It refreshes every 30 seconds, and each issue links to the page that fixes it. The callout reads **All systems operational** only when no service is down and there are no issues; otherwise it lists each issue in a sentence.

The **Services** table shows each check as **Up**, **Slow** (answered, but took over a second) or **Down**:

- **Database (PostGIS)**, **Media store (filesystem)**, **Sign-in (Keycloak)**, **Permissions (OpenFGA)**, **Tile server**: the parts of Talus.
- **Portal certificate**: shown only when the server knows its hostname. It turns amber within 14 days of expiry and red once expired. With the built-in certificate authority, only a renewal that failed to happen counts.
- **Clock**: this server's clock compared with the database's. Past 60 seconds apart, sign-ins and certificates fail.
- **Portal API**: up whenever the page loads.

**Operational state** shows the last successful backup, whether scheduled backups are on, the basemap's age, the **Last restore drill**, storage used, and the version running.

**When something is not OK:**

1. Follow the issue's link (**Open Backups & recovery** or **Open Basemap**) where there is one.
2. For a service that is **Down**, run `scripts/talus-doctor.sh` as `install-runbook.md` §8 describes. It changes nothing and says what to do next.
3. For a failing maintenance job, read its **Detail** on **Maintenance**.
4. For **recovery unproven**, run a restore drill (`dr-restore-runbook.md`, "Automated restore drill"). The issue "No successful restore drill has been recorded." appears from 14 days after installation.

## Backups & recovery

The page, headed **Backups**, covers scheduled disaster-recovery backups: all of Talus's databases (data, sign-in accounts, sharing and permissions) plus attachments.

**Reading it:** a warning shows while backups sit on the same host as your live data, which protects you from mistakes but **not** from losing the machine. **In effect now:** states the cadence, retention, destination, contents, whether encryption is on, and whether the `.env` "lifeboat" is included. It ends with **scheduled backups PAUSED** if they are off. The table lists each backup with **Taken**, **Kind**, **Status**, **DB size**, **Media added**, **Target** and **Detail**. A lock marks an encrypted backup. A lifebuoy marks one that includes the `.env` file. On a successful backup's row, **Download** saves that backup's database dumps (data, sign-in accounts, permissions) as separate files; attachments and other media are not part of the download. Where the lifeboat is on, **🛟 .env (encrypted)** saves that backup's encrypted `.env` on its own. With encryption on, what you download is ciphertext, to be decrypted with your `age` private key (`dr-restore-runbook.md`).

**Back up now** runs a backup in the background. Use **Refresh** to see the new row.

**Change schedule, retention or encryption:**

1. Click **Edit settings**.
2. For each setting, keep the deployment default or set your own: **Scheduled backups**, **Schedule cadence**, **Retention — keep backups for (days)**, **Retention — always keep at least (most-recent backups)**, **Encryption recipients (age public keys)**, **Include the .env file (lifeboat) — requires encryption**.
3. Click **Save settings**. Changes apply from the next backup, and nothing needs restarting.

**Send backups off-host:**

1. Under **Destination**, click **Change destination**.
2. Enter the **Endpoint URL** (server only, no bucket or path), **Bucket**, **Access key ID** and **Secret key** of any S3-compatible store.
3. Click **Test connection** and wait for **✓ Connection test passed**.
4. Click **Save destination**, then **Back up now**.

Existing backups stay where they are. **Use deployment default** switches back and deletes nothing.

**Watch for:** the destination form warns when encryption is off, because backups then arrive as plaintext. There is no restore button. Restores and drills follow `dr-restore-runbook.md`; keys and hardening are in `dr-hardening.md`.

## Maintenance

Read-only: each job's **Last run**, **Status**, **Items**, **Freed** and **Detail**, plus **Recent runs**.

- **Prune idempotency keys**: removes old records that let phones and scripts retry a save safely.
- **Clean orphaned media**: deletes stored attachment files (and Talus+ report files) that nothing refers to any more, once they are older than a grace period.
- **media_usage**: measures the media store's size daily. It deletes nothing.
- **scheduled_backup**, **prune_old_backups**: the backup and the removal of backups past retention.
- **restore_drill**: records successful restore drills, and shows "never" until one runs.

These jobs never delete your layers, features or audit log. How often they run is set per deployment.

## Usage

Tabs: **Content inventory**, **Members**, **Public content**, **Cross-references**, **Activity**, **Storage**, **Groups**, **Service accounts**. Choose a tab, then click **Download CSV** for that tab's report.

**Content inventory** shows the sharing breakdown and flags items not accessed in 90 days. **Public content** lists everything public; review items not in Open Data for unintended exposure. **Cross-references** shows where each item is used; one with zero references can be deleted without breaking anything. **Activity** records who changed sharing, ownership and roles.

## Users

| Role | Allows |
|---|---|
| **Viewer** | read shared content |
| **Editor** | edit features on layers they can reach |
| **Publisher** | create & own layers and maps |
| **Administrator** | full administration |

**Groups** decide which shared content someone reaches. Groups are created with the publisher tools, not here. The summary counts **Total members**, **Pending (never signed in)** and **Disabled**.

**Create a user:**

1. Click **New user**. Enter **Full name**, **Username** and **Email**, then choose a **Role** and any **Groups**.
2. For someone who signs in through your organization's identity provider, tick **SSO user** under **Sign-in method**. The email must match what the provider sends. No local password is created. Connecting the identity provider itself is done in the sign-in service, outside these pages; `sso-runbook.md` has the steps.
3. Click **Create**.
4. For a local user, copy the **Temporary password**: it is shown once. The person must change it at first sign-in.

**Bulk import:**

1. Click **Bulk import**. **Download template** gives a starting file with columns `full_name, username, email, role, groups`. The role is Viewer, Editor, Publisher or Administrator (blank means Viewer). Groups are existing group names separated by `;`. One import takes up to 500 rows.
2. Choose the file and click **Validate**. This is a dry run; nothing is created.
3. Review each row: **will create**, **skip — exists** (username or email taken) or **error**.
4. Click **Create** (labeled with the number of users).
5. Click **Download credentials CSV**. It is offered only once and holds each temporary password, so keep it secure and delete it after handing passwords out.

Bulk import creates local accounts only.

**Edit** changes a role or groups. **Reset password** shows a new temporary password once; for your own password, use Profile → Password instead.

**Offboard someone who has left:**

1. Click **Disable** and confirm. They are signed out and cannot sign in. Their content and attribution are kept, and their scheduled database pulls stop. **Re-enable** reverses this.
2. To remove them for good, click **Transfer…**, choose a **Successor** (another active user) and click **Transfer**. All their maps and layers move, keeping sharing and creator attribution. If an item fails, run it again. **Transfer…** moves maps and layers only. A raster or a field project stays with the departed account until you transfer it yourself, in the item's **Manage** dialog under **Transfer ownership** (`guide-portal.md`). On Talus+, a dashboard, a story or a report cannot be given a new owner in this release: it stays under the departed person's name, and their reports stop running once the account is disabled (`guide-reports.md`).
3. Click **Delete** (shown only on disabled accounts). It cannot be undone. If they edited data, their name stays on that history. Their database connections are removed.

**Last-administrator guard:** the last enabled Administrator cannot be disabled, demoted or deleted.

A row marked **cannot sign in** is a record kept so imported edits have an author; it has no password.

## Service accounts

Credentials for scripts and integrations, scoped to groups you choose, read-only unless you grant read-write, never an administrator.

1. Click **New service account** and enter a **Name**.
2. Choose **Read-only — can read its granted layers** or **Read-write — can also edit features (data-editor)**, and pick **Groups**.
3. Click **Create**. Copy the **Client ID** and **Client secret**; the secret is shown once.

**Rotate secret** issues a new secret, shown once. **Disable** stops new tokens; existing ones stop working within minutes. There is no re-enable. Scripting is in `api-guide.md`.

## Notices

A notice is a banner across the top of the portal for everyone signed in. Visitors who are not signed in do not see it.

1. Turn on **Show a banner at the top of the portal for everyone signed in**.
2. Enter a **Message** and choose a **Level**: **Info**, **Warning** or **Critical**.
3. Click **Save notice**. People see it on their next page load.

## Sharing & access

**Offer public access from this organization** is off by default. It governs sharing to **Public Access**, embedding public maps in other websites, and the **Open Data** catalog and its DCAT-US feed.

- **On** takes effect immediately after you confirm. Nothing becomes public until someone shares it.
- **Off** stops serving all public items, embeds and the Open Data feed immediately. The confirmation says how many items are affected. Items keep their sharing setting, so turning it back on restores them.

Under **Non-admin policy**, choose whether non-admin owners may share to Public Access (off is recommended) and whether members may transfer ownership of their own items, then click **Save policy**.

## Home page

1. Edit **Heading**, **Intro** and **Hero image URL** (must start with `http://` or `https://`). A preview updates as you type. A blank field uses the built-in default.
2. Click **Save home page**.

Maps and layers shared to the **Home (Featured)** group appear in the featured gallery.

## Connections

The **Advertised base URL** is the address that desktop clients, the Field app and scripts use to reach this server. The **Use in…** assistant's recipes show it.

1. Enter an address other machines can reach, such as `https://gis.example.org`.
2. Click **Test connection**. **Reachable** means it answered from your browser.
3. Click **Save**. Blank means the portal's own API address.

Field projects cannot be generated until this is set. Desktop setup is in `connect-desktop.md`.

## Basemap

This page sets which basemaps the portal lists (**Streamed**, **Downloaded**, or both) and downloads the street basemap, fetched for your area or uploaded as a `.pmtiles` file on air-gapped networks; see `guide-imagery.md`. It also holds the organization's map defaults:

- **New maps open on**: the basemap a new map starts with. It saves when chosen.
- **Default extent**: where a new map opens. Use **Use last map view**, **Use basemap coverage** or **Set on map**, then **Save extent**. **Clear** unsets it.
- **Default units**: **Metric** or **US Standard**. It sets the scale bar on embedded maps and, on Talus+, the paper size of reports (A4 for metric, US Letter otherwise) and the units Talus Field starts with on each phone. It saves when chosen.

Saved maps keep their own basemap and view.

## Geocoders

The address-search locators everyone uses on the map. Order sets the fallback (top first), and disabled locators are hidden from the map's "Search in" picker. See `guide-geocoding.md`.

## Database sources

An overview of every database connection and pull in the organization, with owners. Passwords are never shown. You can remove a connection or detach a pull; publishers manage their own on the **Databases** tab of the Catalog. See `guide-databases.md`.

## Talus+ additions

On a Talus+ deployment a **Talus+** group appears in the navigation. Each entry appears only when its module is licensed, except **License**, which appears even before a license is installed. Talus Community has no Talus+ group.

### License

Shows **Status** (**Licensed**, **Term ended** or **No licence**), **Licensed to**, **Term ends** and **Modules**. The license is verified on this server; nothing contacts an outside service.

1. Click **Upload license or activation file…**.
2. Choose the `.json` file Talus GIS LLC sent, unchanged. A modified or unsigned file is refused and nothing changes.
3. Talus+ entries appear on your next page load. An activation file also turns on the AI assists.

If the license is a mounted file, there is no upload button; replace the file (`talus-plus-runbook.md`).

**Nothing is disabled when a term ends.** Administrators see a banner on the Stories, Dashboards and Reports pages saying installed features continue to work and that new releases and support require renewal, with **Upload renewal license…** and **License details**. Uploading the renewed file clears it.

### Delivery targets

Webhook and email destinations where generated reports are announced. Only administrators add or change them. See `guide-reports.md`.

### AI and AI usage

**AI** shows the AI assists' status and the credential. **AI usage** shows Helper usage against the allowance included with Talus+. See `guide-helper.md`.

## What Administration deliberately does not do

- No restore button; restoring is a documented procedure.
- No group editor and no per-permission editor: access is one role plus groups.
- No Maintenance controls and no alerting from Health. Check Health yourself.
- No organization-name setting; the public heading is set on **Home page**.
- No identity-provider setup. That is done in the sign-in service; see `sso-runbook.md`.
- No re-enable for a disabled service account; create a new one.

## If something goes wrong

| Message | What to do |
|---|---|
| "cannot disable or demote the last remaining administrator" | Make another user an Administrator first. |
| "disable this account first, then delete it" | **Disable**, then **Delete**. |
| "… still owns … Transfer their content to another user first, then delete the account." | Use **Transfer…**, then delete. |
| "successor is disabled" | Choose an active user. |
| "unknown group(s): …" | Groups must already exist; check spelling. |
| "CSV is missing required column(s): …" | Fix the header row. |
| "too many rows: …" | Split into imports of 500 rows or fewer. |
| Temporary password lost | **Reset password**. |
| "Must start with http:// or https://." | Correct the address. |
| "✗ Connection test failed — …" | Check endpoint, bucket and keys before saving. |
| "No successful backup has ever been recorded." | **Back up now**, then read the new row's **Detail**. |
| "Maintenance job '…' is failing …" | Read that job's **Detail** on **Maintenance**. |
| "The portal certificate has expired — browsers will refuse the site." | Run `scripts/talus-doctor.sh` (`install-runbook.md` §8). |
| "The portal clock disagrees with its neighbours by over 60 s …" | Fix the server's time synchronization. |
| "not a valid signed license (schema or signature check failed)" | Upload the file as delivered, or ask Talus GIS LLC for a new copy. |
| "this install's license is file-managed …" | Replace the mounted file. |

Still stuck: `install-runbook.md` §8 says where to get help.

## Related guides

`install-runbook.md`, `upgrade-runbook.md`, `dr-restore-runbook.md`, `dr-hardening.md`, `security-overview.md`, `sso-runbook.md`, `basemap-provisioning-runbook.md`, `api-guide.md`, `connect-desktop.md`, `talus-plus-runbook.md`, `guide-imagery.md`, `guide-geocoding.md`, `guide-databases.md`, `guide-reports.md`, `guide-helper.md`.
