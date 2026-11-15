# Talus — Install runbook (new deployment)

**What this is:** the start-to-signed-in procedure for a brand-new Talus deployment, plus the
first-half-hour checklist that turns "it runs" into "it's set up properly."

**One command, a few questions.** In a **terminal** on the server (ssh, or the console) you run
one command; it asks who the administrator is and what address people will use, then installs
by itself. Your **browser** only enters at §5. Until then, browsing to the server's address
shows nothing — that's expected, nothing is installed yet.

---

## 1. What Talus needs

- **A Linux server** (Ubuntu Server 24.04 is what we test on; any modern distro with Docker
  works). One box runs everything.
- **Docker Engine with Compose v2**, plus the small tools the install scripts use — `curl`, `jq`,
  `python3`, `openssl` (most are stock on Ubuntu Server; `scripts/preflight.sh` names any that are
  missing, with the install command). The web app ships prebuilt, the api image is pulled ready-made,
  and nothing is compiled on your machine.
- **Sizing:** 4 CPUs and 8 GB RAM serves a small organization: measured at county scale, that
  carried about fifteen simultaneous requests, roughly 150 named users. More CPUs raise that
  number; more RAM does not. Disk: 40 GB for the system and images, plus whatever your data
  needs — imagery is the thing that grows.
- **Network:** ports **80 and 443** free (the TLS edge), and port **8443** as well if you will
  install without a DNS name (§4): sign-in is then served there. Outbound internet is needed once, to
  pull images at install time; after that Talus can run fully offline. A new install streams
  its map backgrounds and uses a public address search until an administrator switches them
  off (`security-overview.md` lists every outbound connection).

### On a Windows server

Talus runs on a Windows server as a **Linux virtual machine under Hyper-V**. You create the
virtual machine once; from then on Talus is installed, upgraded and backed up inside it exactly as
on any Linux server, and you never need to work in Linux beyond the commands in this runbook.
(There is no native Windows install, and Docker Desktop or WSL on Windows is not a server path.)
About twenty minutes, once:

1. **Enable the Hyper-V role** on the server (Server Manager → Add Roles and Features → Hyper-V;
   or in PowerShell, `Install-WindowsFeature -Name Hyper-V -IncludeManagementTools -Restart`).
2. **Download Ubuntu Server 24.04 LTS** (the ISO from ubuntu.com; the version this runbook is
   tested on).
3. **Create the virtual machine** in Hyper-V Manager: **Generation 2**, 4 virtual processors,
   8 GB of memory (turn Dynamic Memory off), a 60 GB virtual disk, the
   external virtual switch so the machine gets an address on your network. Under Security,
   **turn Secure Boot off** or set its template to *Microsoft UEFI Certificate Authority* —
   Ubuntu boots either way, and a wrong template is the one thing that stops it.
4. **Install Ubuntu Server** from the ISO: defaults throughout; name the machine (`talus` is
   fine); create one administrative user; **tick "Install OpenSSH server"**; leave every other
   snap and package unticked. Reboot and sign in — at the console, or by SSH from your desk.
5. **Install Docker Engine** from Docker's repository (the one-page instructions at
   docs.docker.com for Ubuntu; the `docker-ce`, `docker-ce-cli`, `containerd.io`,
   `docker-compose-plugin` packages), then add your user to the `docker` group and sign in again.
6. **Give the machine a fixed address** — a DHCP reservation on your network for the virtual
   machine's MAC is the simplest — so the portal's address never changes.

Then continue at §2 below, inside the virtual machine. Back up the virtual machine as you back up
any other (checkpoints, or your backup product's VM-level backup) **in addition to** Talus's own
backups, which is what carries your data between machines.

## 2. Get the release

In the terminal, download the release tarball from the Releases page (or clone the repository
at a version tag), then — these commands run verbatim for any version:

```
mkdir talus && tar xzf talus-[0-9v]*.tar.gz -C talus --strip-components 1 && cd talus
```

(The pattern picks the release tarball and nothing else — a Talus+ bundle saved in the same
folder is left alone; §3 says what happens to it.)

Everything happens from this directory. After the install it *is* your deployment — keep it.

## 3. Before you start

**Nothing to choose.** A Talus install starts with an empty catalog and fills up with your own
data. There is no separate evaluation mode — evaluate on the real thing, and keep the
deployment you evaluated.

Have three things in mind; the setup asks for them and nothing else:

- **Your name and an email address.** The setup creates the installation's first administrator
  account, named `admin`, and attaches that email to it — so you can sign in as `admin` or
  with the email. Use your own address, or a shared mailbox if `admin` will be looked after
  by more than one person.
- **A password of your own choosing** — 12 or more characters, with an upper-case letter, a
  lower-case letter and a digit. Talus never shows it, prints it or keeps a copy.
- **The address people will type to reach the portal.** A DNS name if you have one; otherwise
  nothing at all — this machine's network address is the default.

**Installing Talus+ as well?** Put the bundle (`talus-plus-<version>.tar.gz`) and your license
file in the same folder as the release tarball before you start. The setup finds them, checks
the bundle, and offers to install both. (Adding Talus+ later is one command —
`docs/talus-plus-runbook.md`.)

## 4. Install

```
scripts/talus-setup.sh
```

Every question comes first, and **nothing is installed until all of them are answered**:

1. *Your name* and *your email*.
2. *Password*, twice. It is not shown as you type.
3. *What hostname will users reach this portal at?*
   - **Have a DNS name?** Type it. You'll then be asked whether to request **public
     certificates** automatically — answer yes only if this server is reachable from the
     internet on ports 80/443; the summary tells you honestly whether issuance worked. It
     also prints the two DNS records to create.
   - **No DNS / LAN only? Just press Enter.** The portal runs at this machine's network
     address (`https://<server-ip>`, sign-in on port 8443) with the built-in certificate
     authority. This is the right answer for most evaluations and internal deployments.

Then it runs unattended: it generates all secrets, brings up the stack, applies the database
schema, and creates your administrator account. About two minutes on a modest machine; the
slow part is pulling images the first time.

It ends with a summary — the portal address, the sign-in address, and the name you sign in
with — and saves the same text as **`talus-summary.txt`** in this folder. The summary never
contains your password.

**If it stops partway** (a failed image pull is the usual cause), the message names the image
and the likely cause. Fix it and run the same command again: it carries on for the same
administrator and asks only for your password and the address again — the password was never
stored, so it has to ask.

**Changing the address later** is the same command. On a machine that is already installed it
asks only the hostname question, and it is safe to re-run whenever the answer changes
(including after an IP change). Sign-in configuration follows the name automatically.

> **Scripting an install?** The two-step form still works and asks nothing:
> `./install.sh --edge --tls-internal`, then `scripts/talus-setup.sh --hostname <name>`. That
> path creates an administrator named `admin` with a generated password, printed once as the
> very last thing the installer shows — copy it before you close the terminal, and change it
> after signing in (the account chip → **Profile** → **Password**).

## 5. Sign in the first time

Browse to the portal address the summary printed and sign in as **`admin`** — or with the
email you gave — and the password you chose.

**Using the built-in certificate authority?** Your browser warns about the certificate — the
signer is your own installation's CA. **You must accept the warning on TWO addresses**: the
portal itself, and the sign-in address (the summary printed both). The tell: if you skip the
second one, **the Log in button silently does nothing**. Open the sign-in address in a tab,
accept, go back, log in.

To make every browser on a machine trust the installation properly (and enable the Field
app), install the CA root once:
```
docker compose --profile edge cp caddy:/data/caddy/pki/authorities/local/root.crt talus-root-ca.crt
```
Import it into the OS trust store (Windows: certmgr, "Trusted Root Certification
Authorities" · macOS: Keychain Access → Always Trust · Linux:
`/usr/local/share/ca-certificates/` + `update-ca-certificates`).

## 6. Your first 30 minutes — the setup that matters

Before you bring real data in, back up the source where it is and leave the system it came
from running until you have checked the result in Talus. Talus never modifies the source, but
the original is your way back if a field type or a projection did not arrive as you expected.

1. **Create accounts for the people you work with** (Admin → Users) — one each, rather than
   sharing `admin`. `admin` belongs to the installation, not to a person: keep it for setup
   and recovery. If you want a named account of your own as well, give it a different email
   from the one attached to `admin` (an email belongs to one account).
   A **shared sign-in** — a dispatch console, a front counter, a wall display — is an ordinary
   account with a one-word name such as "Dispatch". Know the trade: whatever is done through
   it is recorded under that name, not a person's, so a view-only role is its natural fit.
   (A script or an integration is not an account at all — give it a service account or an
   API token.)
2. **Make your first map.** Create a layer (**Catalog → Layers → + New layer**), choose **New map**
   in the top bar, add the layer, put a point down. You'll have an OpenStreetMap street map under you from the first second,
   streamed from the publisher.
3. **Download your area** (Admin → Basemap): needed for offline use, the Light, Dark and
   Gray looks, Talus Field packs and the map on report sheets. Zoom the map to your
   jurisdiction first, then "Use last map view" scopes the download. Open map pages pick it
   up on their next focus. To keep viewers' browsers off the internet for map backgrounds,
   then choose **Downloaded** under **What this portal lists**.
4. **Set the advertised base URL** (Admin → Connections): the address desktop clients, the
   Field app and QR links use to reach this server — for most installs, the portal address
   the setup printed. Field projects cannot be generated until it is set, and the
   verification suite below says so (inv22 skips).
5. **Look at Admin → Backups & recovery.** Backups are already running on a default cadence; two
   things deserve deliberate choices, both editable in the panel:
   - **Destination** — same-host by default, which protects against mistakes but **not
     against losing the machine**. Point it at any S3-compatible store with the **Test
     connection** button before saving.
   - **Encryption** — add `age` recipients so backups are unreadable wherever they land.
   `docs/dr-hardening.md` walks both, including generating the keys — the best
   trust-per-minute in this list.
6. **Run the verification suite** and keep the output:
   ```
   bash scripts/verify-invariants/run.sh
   ```
   The security invariants, proven against *your* install — every leg should PASS (a loud
   SKIP with a printed reason is fine; a FAIL is worth a bug report). The runner prints the
   count it ran.
7. **Skim `docs/dr-restore-runbook.md`** — specifically *"Where your backups are — record
   this BEFORE the disaster"* — and put the answers somewhere that survives the server.

## 7. Where things live

- **`.env`** — every secret and setting; generated for you; **the one file that is yours**.
  Upgrades preserve it. Back it up (the encrypted lifeboat in Admin → Backups & recovery is the clean
  way — see `dr-hardening.md`).
- **Your data** — the Docker volume `storagedata` (media, imagery, backups as plain files)
  plus the PostgreSQL volumes. `docker volume ls` shows them.
- **`talus-summary.txt`** — the addresses and how to sign in, as the setup printed them.
  No secrets in it.
- **Which flags did I install with?** — `grep TALUS_INSTALL_MODE .env`. (The setup runs the
  installer for you; an upgrade re-runs it with these flags — `docs/upgrade-runbook.md`.)

## 8. If something goes wrong

- **Forgot your administrator password?** It isn't stored anywhere — but the identity
  service's master credential is, in `.env`. Browse to the sign-in address plus `/admin`
  (e.g. `https://<sign-in-address>/admin`), log in with `KEYCLOAK_ADMIN_USER` /
  `KEYCLOAK_ADMIN_PASSWORD` from `.env`, switch the realm dropdown from **master** to
  **talus**, open **Users → admin → Credentials → Reset password** (Temporary: Off).
- **The setup says your administrator "was already created by an earlier run"?** An earlier
  attempt got far enough to create the account, and its password was not changed: sign in
  with the password you chose in that first attempt, or reset it as above.
- **Running the setup again never changes an existing administrator account**: not its password, role,
  email or name. If you made `admin` a non-administrator on purpose, it stays that way, and the run says so.
  To make it an administrator again, use **Admin → Users**, or run the setup with `TALUS_RESTORE_ADMIN_ROLE=1`.
- **Log in button does nothing** → the second certificate warning, §5.
- **Ports 80/443 already taken** (or 8443, on an install without a DNS name) → nothing checks this
  beforehand. The install stops near its end, when the edge starts, with Docker's error naming the
  port. Free the port and run `scripts/talus-setup.sh` again (§4).
- **A pull fails** → the message names the image and cause; fix it and run
  `scripts/talus-setup.sh` again (§4).
- **The portal won't load, or a service shows unhealthy** → run `scripts/talus-doctor.sh` from the
  install directory. It changes nothing; it reads the services, the host's disk and clock, the edge,
  the certificate, the name, the database and the recent log, and prints one line per check with
  what to do next on every FAIL. Then `docker compose ps`, `docker compose logs <service>` for the
  detail; re-running `scripts/talus-setup.sh` is safe (same answers; your account and data are kept).
- **Stuck** → write to `support@talusgis.com` (`docs/support.md` says how support works); include the
  `talus-doctor.sh` report (it is already scrubbed of secrets) and `talus-summary.txt`.

## 9. What next

- **Hardening & off-host backups:** `docs/dr-hardening.md`
- **Restore procedures (read before you need them):** `docs/dr-restore-runbook.md`
- **Upgrades:** `docs/upgrade-runbook.md` — back up, unpack the new release, re-run the
  installer with the same flags.
- **Adding Talus+** (now or later — it is the same one command either way):
  `docs/talus-plus-runbook.md`
