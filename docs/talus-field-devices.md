# Talus Field — devices, data on the phone, and what revoke really does

> Talus Field is part of **Talus+**.

For the IT person who has to answer "what is on that phone, and what happens when we take it away".
The crew lead's step-by-step is in [talus-field-runbook.md](talus-field-runbook.md).

---

## 1. What Talus Field is, technically

A web app installed to the home screen (a *progressive web app*), served from your own Talus server
at `https://<your host>/field/`. There is nothing in an app store, nothing to distribute, and nothing
that talks to anyone but your server. It runs in the phone's browser engine (Safari on iOS, Chrome on
Android) inside its own sandbox — the same one a website gets, with the same protections.

## 2. What lives on the phone, and where

Everything is inside the app's own private browser storage (the origin's storage, in browser terms).
No other app can read it, it is not in the camera roll, and it is not in iCloud or Google backups of
files. Clearing the site's data in the browser's settings removes all of it.

| On the phone | What it is | Until |
|---|---|---|
| **Packs** | The basemap tiles and the map's features for one work area, as built in the portal | Removed from the phone, revoked, or a week after the pack expires; **a pack with edits not yet synced stays until they sync** |
| **The device credential** | One per pack: a personal access token the app minted for itself after the crew member's one sign-in. It **reads what the person can read**, and **creates and edits only on the layers in that pack**; it can never delete, never administer, never read a layer the person cannot see. Deleting from the phone does not use it: a delete is sent with the person's own sign-in, so it needs them to be signed in (the runbook, §6) | 30 days. Renewed in its last week when the app is open online **and the person is signed in**; the credential it replaces stops working within a day. Dies at once with a revoke or a disable |
| **Queued edits and photos** | Edits made without a connection, in the order they were made, with the photos they carry | Landed on the server, or wiped with the pack |
| **Local changes** | Rows the phone knows are newer than its pack (its own landed edits, refreshes) | With the pack |
| **The last person who signed in** | Their user id — so the app opens *their* packs with no connection | Until a different person signs in |
| **Settings** | The GNSS receiver in use (§5), the units, the required accuracy, Auto-sync, the directions app, and per map which layers are switched off and which basemap draws | Until changed |
| **Recent searches** | The last eight things typed into the map's search box (browser storage, not the app's) | Until cleared from the search box |

**Two people, one phone:** each person's packs live under their own identity on the device; signing in
as a different person removes the previous person's packs. The app opens without a connection only
the packs of the person who signed in last.

## 3. Revoke, disable, wipe — and the honest limit

Two levers, both in the portal, both immediate on the server:

- **Admin → Users → Disable** ends every credential the person holds. Their next sign-in is refused,
  and every device token dies on its next use.
- **Admin → Users → Devices → Revoke** ends one credential (one pack on one phone) and leaves the person's
  account alone; revoke each row the lost phone held. The person can also do this under **Profile → Access tokens**.

The phone reacts the next time the app opens with a connection: it asks the server about each pack's
credential, and a revoked one **wipes that pack from the phone — data, credential, queued edits and
photos** — and tells the person. Nothing on the server is touched; the person's edits that had already
landed keep their name.

**The honest limit.** A phone that never comes back online never hears the revoke. What happens then:

- the credential is dead on the server anyway, so nothing that phone holds can reach your data —
  revoke is enforced at the door, not by the phone's good behavior;
- the pack stops opening a week after its expiry (each pack has a life you set at build time, typically
  days), and removes itself at the next launch, **unless it still holds edits that have not synced**: those
  keep it on the phone until they sync, so a phone lost offline with unsynced edits keeps its pack until it
  reconnects, and a revoke wipes it then;
- what remains is a snapshot of data the person was allowed to see, inside the browser sandbox of a
  phone you have lost. For a device in hostile hands the control is the device's, not Talus's: **remote
  wipe through your MDM**, or Apple's *Find My* / Google's *Find My Device* erase. A device passcode
  (which every managed phone should require) keeps the sandbox closed until then.

This is the same posture every offline field app has, Field Maps included. What Talus adds is that the
credential is per phone and per pack, dies at the door, and is visible and revocable by name.

## 4. Managed phones (MDM) and BYOD

**Managed (supervised) devices.** Deploy the app as a **web clip** to `https://<your host>/field/`
(Apple Configurator / your MDM's *Web Clip* payload; Android's managed *web app* shortcut). Require a
device passcode; keep automatic OS updates on. Nothing else is Talus-specific: there is no app to
sideload, no certificate to pin, no per-app VPN. Your server's TLS certificate must be one the phone
trusts (a public certificate, or your CA pushed by the MDM).

**BYOD (a personal phone).** Works the same. What the person keeps on their phone is the sandboxed
snapshot in §2 and nothing else; **Disable** or **Revoke** when they leave, as with any device. If
your policy needs the ability to erase a personal phone, that is an MDM decision, not a Talus one.

**Contractors.** Give them a real account (the runbook §1) and disable it when the contract ends.
Talus deliberately has no "link with a built-in credential" path: it would attribute a contractor's
edits to the person who made the link, and could not be switched off per person.

## 5. External GNSS receivers (Eos Arrow, SparkPNT TX2, Trimble, Bad Elf, Emlid…)

Talus Field is a web app, and a web app can only ask the phone where it is. Whether a survey receiver's fix
becomes *the phone's location*, or whether the app can read the receiver directly, is decided by the platform
and the receiver, not by Talus. Measured on real phones, 2026-09-26.

**On an iPhone.** Only a receiver that implements Apple's *Location* accessory feature becomes the phone's
location for every app. Eos Arrow and Bad Elf do: pair in Settings → Bluetooth and Talus Field's position is
the receiver's, with no app setting. iOS passes the position but not the receiver's own accuracy figure, so
the accuracy Talus Field shows and records is iOS's estimate. Receivers that offer only an accessory data
stream — the SparkPNT TX2 among them — are read by native apps such as Field Maps but never reach a browser,
and no companion app can bridge that: iOS lets no app supply the phone's location. **Ask the maker one
question before buying for iPhones: does it provide location to all iOS apps without a companion app?**

**On Android.** Talus Field reads the receiver itself over Bluetooth Low Energy, in Chrome, Edge or Samsung
Internet: Settings → Location → *Position from* → **Bluetooth receiver**, then on the map tap the GPS strip
and pick the receiver in the phone's chooser. No companion app is needed for position. The strip then names
the fix (*RTK fixed · GPS accuracy 2 cm*), the blue dot is the receiver's, and every point carries the full
GNSS bundle from the receiver's own sentences: fix type, satellites, PDOP/HDOP/VDOP, correction age, base
station, horizontal and vertical accuracy, altitude, fix time and the receiver's name. The link holds with
the screen off and reconnects on its own if it drops. Corrections stay the receiver's business (its own
NTRIP client, or the maker's app); Talus Field never holds correction-network credentials. Receivers
advertising the Nordic UART service work today (the TX2 and the SparkFun RTK family); others are added as
they are verified. A receiver Talus Field cannot read directly still works through a mock-location app such
as SW Maps or GNSS Master, with position, accuracy, altitude and time recorded and the quality fields empty.

**Which receivers, on which phone.** *Verified* = tested by Talus on the phone; the rest is the maker's own
documentation as of September 2026 — check the maker's page for your model before buying, and run the one-minute
test below. Every RTK receiver also needs its own correction source (the maker's app or the receiver's own
NTRIP client); that is separate from how the position reaches Talus Field.

| Receiver | iPhone: position reaches Talus Field? | Android: how |
|---|---|---|
| Eos Arrow (100, 200, Gold, Gold+) | **Yes** — Location Services mode, all apps; iOS reports its own accuracy figure, not the Arrow's | Eos Tools Pro mock location |
| Bad Elf (Flex, GNSS Surveyor, GPS Pro+) | **Yes** — every location-aware app, no setting | Bad Elf app mock location |
| Trimble (Catalyst DA2, R1, R2, R12i, R580/R980) and Spectra Geospatial | **Yes** — while **Trimble Mobile Manager** is running: positions replace the phone's own for every app | Trimble Mobile Manager mock location |
| Leica Zeno (GG04 plus, FLX100 plus) | **Yes** — while **Zeno Connect** is running (MFi) | Zeno Connect mock location |
| Juniper Geode (GNS2, GNS3) | **Yes** — automatically on pairing, no app; DOPs pass, the receiver's own accuracy does not | Geode Connect mock location |
| Emlid Reach (RX MFi; RS2+, RS3) | **No** — the RX serves NMEA to apps that read it themselves (Field Maps, QField); Emlid Flow does not feed iOS Location Services | Emlid Flow mock location; Bluetooth NMEA — a direct read once verified in hand |
| SparkPNT TX2 (SparkFun RTK family) | **No** — *verified 2026-09-26*: data-only accessory | **Direct read, verified** (Bluetooth receiver in Settings); SW Maps mock location also works |
| Garmin GLO 2, Dual XGPS 150/160 | **Yes** — classic Location accessories (meter-class, no RTK) | maker/mock apps |
| Topcon, Hemisphere / Carlson / Stonex, CHCNav, Septentrio | **Unknown** — no Apple Location-feature support found in their documentation; ask the maker the question below | their own apps or SW Maps mock location |

**The one question for a maker, for iPhones:** *does the receiver provide its position to all iOS apps through
Location Services, without a companion app running — or with which one?* **The one-minute test on any phone:**
open Talus Field with the receiver connected and fixed, note the position and the strip; power the receiver
off; if nothing moves, Talus Field was never hearing it.

**The receiver name.** With *Position from* set to *This phone*, pick the receiver under **GNSS receiver** so
each point records which one took the fix; the app cannot see what is paired. A receiver Talus Field reads
directly records its own advertised name.

**Antenna height.** On a pole, enter the antenna's height above the point under Settings → Location; it is
taken off the altitude recorded with each point. Leave 0 for a phone in hand.

**Datum — read this once.** A network RTK correction stream is delivered in a specific reference frame (on
many networks the *port* or mount point you connect to selects it: NAD83(2011) or WGS 84, for example). The phone labels
every fix WGS 84 regardless. For layers kept in a state plane or another NAD83 system, set **Fix datum** under
Settings → Location to the frame your correction network delivers (NAD83(2011) for a NAD83 port) so the
conversion into the layer's system is right; left at WGS 84, a NAD83 fix lands about a foot off in State
Plane. Check one captured point against a known monument before you trust the workflow.

**With the GNSS metadata fields** on a layer (Manage ▸ Fields ▸ *Add GNSS fields*), every point the crew
captures carries whatever the position source can supply; fields the source cannot fill stay empty rather
than being guessed.

## 6. Things worth knowing

- **Updates** arrive with your server: the app checks on launch and swaps to the new version on the
  *second* launch after an update. No app store, no per-device rollout.
- **Storage.** A pack is typically 2–50 MB for a town at street detail; imagery-heavy areas at lot
  detail run larger — the portal shows an estimate before you build. Phones give a web app several
  gigabytes; the app shows what it holds under *Packs on this phone*.
- **Photos** taken in the app go straight to the server when there is a connection, and wait on the
  phone (with the edit they belong to) when there is not. They are not written to the camera roll.
- **No analytics, and nothing sent to Talus.** Map backgrounds follow your organization's setting in
  Admin → Basemap. From a pack, and on a portal set to **Downloaded**, the basemap is the one stored on
  your server. Where your organization offers streamed maps, a phone with a connection fetches those
  from their publisher (OpenStreetMap or the U.S. Geological Survey), as a browser does. A phone picks
  up a change the next time it reaches the server; one that cannot reach it keeps the last setting it
  read for up to 7 days, then shows downloaded maps only.
