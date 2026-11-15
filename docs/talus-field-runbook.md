# Talus Field — the crew lead's runbook

> Talus Field is part of **Talus+**.

This is the order a crew lead follows, once, to put a crew on Talus Field — and the two things to do
when someone leaves or loses a phone. Every step is in the portal or on the phone; nothing here needs
a terminal or a Keycloak console.

---


## 1. Give each crew member an account

**Admin → Users.** One person: **New user** (name, username, email, role, groups — the temporary
password is shown once; hand it over). A whole crew: **Bulk import** — upload a CSV with a
`username`, `full_name` (or `name`), `email` and optional `role` and `groups` columns (group display
names, `;`-separated; a blank role is Viewer), **Validate** (a dry run that shows you every row), then
**Create** the valid rows and **Download credentials CSV** — the one-time passwords, shown once. Each
person changes their password at first sign-in.

Crew members are **Editors** unless they also author maps. A contractor gets an account like anyone
else — time-boxed by you: **Disable** it when the contract ends (§7). There is no "share a link and
skip the login" path, on purpose: every edit is attributed to the person who made it, and every
person can be switched off individually.

## 2. Put the crew in a group

**Groups → + Create group** (for example *Signs crew*), then **+ Add members**. A group is what you
share maps and layers *to*; a crew member sees a map in Talus Field only when it — and the layers
in it — are shared with a group they are in (or with the whole organization).

## 3. Share the map and its layers, and switch it to Field-ready

In the **Catalog**, on each layer the crew will collect on: **Manage ▸ Sharing** — *Organization Only*, or
add the crew's group under *Groups*. The layer must be **editable** for the crew to add or change
features; a layer shared read-only shows on the map but cannot be edited. Under **Manage ▸ Settings ▸
Editing**, four checkboxes say *what* the crew may do — **Add features · Edit attributes · Move or reshape ·
Delete features** — and the phone shows only those buttons (a crew that updates a sign's condition but must
never move or delete a surveyed asset gets *Edit attributes* alone). Everyone with edit access gets the same
set, administrators included.

Open the web map, share it the same way, and press **Field-ready** in the map header, then **Save map**.
Field-ready is a property of the map: it tells Talus Field this map is meant for collection. The map's
title, its layers, their forms (**Manage ▸ Fields** and the form layout) and their symbols are what the
crew sees on the phone.

## 4. Build a pack

A **pack** is the basemap tiles and the map's collectable features for one work area, built once and
downloaded by every crew member who can see the map. In the map header, **Packs → New pack**: name it
(*Ward 3*), set the extent (the current view, or draw one), pick the basemap detail (*streets · lots ·
detail* — the estimate updates), set how many days it is good for (1 to 30), and **Build pack**. A pack is ready in
seconds for a neighborhood and a few minutes for a town. **Rebuild** it when the layers change
substantially; crews are offered the update.

A pack's basemap is cut from the street map your organization has downloaded to its own server, so a
pack needs a downloaded area. If your portal has none yet (a new portal starts with streamed maps
only), download one first in **Admin → Basemap**. A pack cannot carry a streamed map: streamed maps
are fetched live from their publisher, and OpenStreetMap does not allow an area to be copied from its
map servers for offline use. The downloaded map is made from the same OpenStreetMap data, and it is
the one a pack carries, as Light, Dark and Gray.

Packs expire (the days you chose). An expired pack still opens on the phone for a week — a crew in
the middle of a job does not lose its map at midnight — then removes itself, unless it holds edits that have
not synced yet: it stays until they do. Build a fresh one before that.

## 5. The phone

Open the portal on a desktop, go to **Field** — the card at the top shows the app's address as a QR
code. The crew member:

1. scans it with the phone camera (or types the address), and **signs in once**;
2. follows the card on the phone: **Share → Add to Home Screen → Add** on an iPhone, or **Install**
   on Android (where the card shows steps and no button: the browser's menu, then **Install app** or
   **Add to Home screen**);
3. opens the app **from its icon** from then on, and taps **Download** under the map's pack.

That is the whole install. The one sign-in is what ties the phone to the person: the app then holds
its own credential for that pack (30 days, renewed in its last week whenever the app is open online and the
person is signed in) and does not ask the crew to sign in again to add or edit in the field. Deleting is the
exception: that credential cannot delete, so a delete is sent only while the person is signed in (§6).
Downloads and the map list need a connection; everything after that works with or without one.

If the crew uses an external GNSS receiver (Eos Arrow, SparkPNT TX2, Trimble, Bad Elf…): on Android, set
**Settings ▸ Location ▸ Position from** to *Bluetooth receiver* and tap the map's GPS strip to pick it — Talus
Field reads the receiver itself and every point carries its fix type, satellites and accuracy. On iPhone, only
a receiver that provides location to all apps (Eos Arrow, Bad Elf) works, paired in Settings → Bluetooth; name
it under **GNSS receiver** so points record it. Which receivers work on which phone, antenna height and the fix
datum: [talus-field-devices.md](talus-field-devices.md) §5.

### 5.1 Settings

The name at the top right of the maps list opens **Settings** — the same page a Field Maps crew knows as
its profile. Everything on it is per phone and works without a connection:

- **Account** — who is signed in, which server, **Sign out**.
- **Collection ▸ Required accuracy** — off, or a distance (3 ft to 100 ft, 1 m to 30 m). With one set,
  the GPS strip on the map reads *"GPS accuracy 26 ft · 20 ft required"* and turns red when the fix is
  worse than the requirement. The crew can still add the point (a blocked crew under a tree canopy writes
  it on paper); the accuracy is recorded on the feature when the layer carries the GNSS fields, so a
  reviewer can see it.
- **Location ▸ Position from / GNSS receiver / Antenna height / Fix datum** — where the position comes from (Android can read a Bluetooth receiver directly), the paired receiver's name when the phone's location is used, the antenna height taken off altitudes, and the frame a network-RTK fix arrives in.
- **Units** — feet or metres for the GPS strip, distances and the measure tool (the organization's
  default unless the crew member picks).
- **Sync ▸ Auto-sync** — on (the default): edits made without signal are sent as soon as the app is open
  with a connection. Off: they wait for **Sync now** on the pending list.
- **Directions** — which maps app opens from a feature's **Directions** (ask each time, Apple Maps,
  Google Maps, Waze).
- **Troubleshooting ▸ Storage probe** — what the phone holds and whether it can download and open a
  pack; for the crew lead.

## 6. The day

Open the map from the icon. The map, its features and the basemap come from the pack, so it opens the
same with full signal, one bar, or airplane mode. The bar at the top is the one a Field Maps crew
expects: the map's name (back), **Layers** (switch a layer off on this phone), **Search** (a feature by
any of its values, an address through the organisation's geocoder whenever the phone has a connection, or
typed coordinates — features search inside the pack with no signal; an address or coordinate row's **+**
starts a feature there), and **More** (**Basemap** — Light, Dark and Gray draw from the pack with no signal; where your organisation offers streamed maps, those are listed too and need a connection; **Map extent** and the
map's bookmarks; **Legend**; **Measure** — tap points for a distance, close the shape for an area;
**Share map** — the map's link, for a crew member who signs in once). Under the bar, the GPS strip
reads the fix's accuracy at all times.

**+** adds a feature at the blue dot (or where the crew taps); the accuracy circle around the fix shows
how far the point could be off, in red when it is worse than the phone's requirement. Tapping a feature
opens its sheet — its coordinates and how far away it is, then **Edit**, **Copy attributes** (a new
feature on the same layer with these values filled in — twenty identical signs a day) and **More**
(**Move**, **Add a photo**, **Collect here** — a new feature at this one's location, **Directions** —
the phone's maps app, and **Delete** where the layer's owner allows deleting). Every edit is saved on the
phone the moment it is made and shown with a pending ring until it lands on the server.

Landing is automatic: whenever the app is open with a connection, queued edits are sent in the order
they were made, through the same doors the portal's editor uses, under the crew member's name. The
pill in the corner counts what is waiting; tapping it lists the edits, with **Sync now**. A delete is
sent only while the crew member is signed in. One that cannot be sent waits on that list with
*"Deleting needs you to be signed in. Sign in, then select Sync now."*; the edits queued after it are
still sent, and **Discard** on its line puts the feature back. If someone
edited the same feature in the portal meanwhile, the phone asks — **Keep mine** or **Keep theirs** —
and never overwrites silently.

**N changes · Refresh** on the banner means the server has newer rows than the pack; tap it to pull
them in without rebuilding the pack. Queued edits are never overwritten by a refresh.

## 7. Offboarding — one action

**Admin → Users → the person → Disable.** They are signed out, cannot sign in again, and every device
credential they hold stops working the next time it is used. The next time any of their phones opens
Talus Field with a connection, its packs — **including any edits that had not yet synced** — are wiped
and the phone says so. Their name stays on the edits they made.

Before disabling someone whose phone may hold unsynced work, ask them to open the app with a
connection once: the pill shows what is waiting, and it lands in seconds.

## 8. A lost or stolen phone — the person stays

**Admin → Users → the person → Devices.** Each pack a phone downloaded is a row, so a phone with two packs
is two rows, and the rows do not name the phone. **Revoke every row for the packs that phone held**, or, if you
cannot tell the rows apart, **Disable** the person and enable them again: that ends every device credential
they hold. That phone's packs and unsynced edits are wiped the next time it opens the app with a connection.
If it never reconnects, a pack without unsynced edits stops opening a week after it expires; a pack with them
stays on the phone. The person downloads the
pack again on the replacement phone — that mints a new credential — and carries on. The crew member
can do the same themselves under **Profile → Access tokens** (the token is named *Talus Field · <pack>*).

For a phone in hostile hands that stays offline, the answer is the device's own management (remote
wipe through your MDM, or the platform's Find My / Find My Device), not Talus — see
[talus-field-devices.md](talus-field-devices.md) §3.

## 9. Shared phones

Two crew members can share a phone. Each sees only their own packs; signing in as a different person
removes the previous person's packs from that phone. Sync a shared phone before handing it over.

---

*Related:* [talus-field-devices.md](talus-field-devices.md) (what lives on the phone, MDM, receivers) ·
`connect-desktop.md` Part 2 (the QField path, which stays available in Talus Community).
