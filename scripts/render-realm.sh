#!/usr/bin/env bash
# render-realm.sh — produce keycloak/import/<realm>-realm.json from keycloak/realm-export.template.json,
# substituting install-time values (client secrets, portal origin) from ./.env.
#
# WHY (decision-deployment-appliance.md §5.4): the shipped realm template contains NO secrets and
# NO users. Secrets are minted per deployment (generate-secrets.sh / install.sh) and injected
# here. A CLEAN render (the default, the customer path) therefore imports a realm with zero
# logins in it.
#
# --demo is DEV-TREE ONLY (decision-demo-seed-retirement.md §4). It merges
# keycloak/realm-demo-users.json into the render: the 8 dev users WITH THEIR
# PINNED UUIDs. That overlay does NOT cross the release MANIFEST, so an artifact copy of
# this script refuses --demo rather than rendering dev logins into a customer's realm.
# The pin site must stay realm-import — Keycloak IGNORES `id` in admin-API user
# POSTs (proven empirically, matrix-user-uuid-pinning-addendum.md), and unpinned users collide
# with surviving platform.users rows on a kcpgdata-only wipe (the reproduced 7c3 bug). The
# stub-auth identity (validate-phase1, the all-zeros UUID) also lives there. The dev
# bring-up renders --demo; roles still come from bootstrap-7b-keycloak.sh, as 7c3 decided.
#
# Keycloak imports the rendered file on FIRST boot only (`start --import-realm`), so re-rendering
# is always safe: a running Keycloak ignores it. The rendered file is GITIGNORED.
#
# ★★ The output FILENAME is <realm>-realm.json, derived from the template's realm key, and the
# directory keycloak/import/ is what docker-compose.yml mounts (a DIRECTORY, not the file).
# Keycloak 26.0 derives the realm name from that filename pattern and looks it up for the
# deferred service-account setup (keycloak/keycloak#36284): a mismatch = "Session not bound to
# a realm" on every fresh install. Naming the file from the realm key makes the match structural.
# Any previously rendered *-realm.json in the directory is removed first — Keycloak parses EVERY
# file there, and a stale one would re-import a realm under its old name.
#
# Placeholders in the template use @VAR@ form. Values are JSON-escaped on substitution and the
# result is parse-validated before it is written. Unresolved placeholders fail loud.
#
# PORTAL_ORIGIN defaults to the dev SPA origin; a real deployment sets it in .env (install.sh
# will own this in the appliance flow).
#
# Usage:
#   ./scripts/render-realm.sh          # clean render — no users (the customer/appliance path)
#   ./scripts/render-realm.sh --demo   # + the 8 pinned dev users (DEV TREE ONLY)
#
# bash 3.2-safe. Requires python3 (already a bring-up dependency).

set -euo pipefail
cd "$(dirname "$0")/.."

DEMO=0
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# --demo needs an overlay that deliberately does not ship. Refuse with the reason rather
# than letting the python child die on a missing file with a traceback.
if [ "$DEMO" -eq 1 ] && [ ! -f keycloak/realm-demo-users.json ]; then
  echo "FAIL: --demo needs keycloak/realm-demo-users.json, which is not present." >&2
  echo "  That overlay is a development-tree file — 8 pinned dev users with formula" >&2
  echo "  passwords — and does not cross the release MANIFEST. A Talus install renders" >&2
  echo "  CLEAN, with zero logins in the realm:" >&2
  echo "    ./scripts/render-realm.sh" >&2
  exit 2
fi

[ -f .env ] || { echo "FAIL: ./.env not found — run scripts/generate-secrets.sh (clean install) or create .env from .env.example (dev)." >&2; exit 1; }

# Export .env vars so the python child sees them.
set -a
# shellcheck disable=SC1091
. ./.env
set +a
export PORTAL_ORIGIN="${PORTAL_ORIGIN:-http://localhost:5173}"
export RENDER_DEMO="$DEMO"
# Does this Docker remap container ids (rootless, userns-remap)? Then the sign-in server's
# "uid 1000 / gid 0" are other ids on the host and the rendered file's mode cannot be tightened
# safely (see the permissions block below). Unknown or no docker = not remapped.
RENDER_DOCKER_REMAPPED=0
if command -v docker >/dev/null 2>&1; then
  case "$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null || true)" in
    *rootless*|*userns*) RENDER_DOCKER_REMAPPED=1 ;;
  esac
fi
export RENDER_DOCKER_REMAPPED

python3 - <<'EOF'
import json, os, re, sys

TEMPLATE = "keycloak/realm-export.template.json"
DEMO_USERS = "keycloak/realm-demo-users.json"
OUT_DIR = "keycloak/import"

text = open(TEMPLATE).read()
realm_name = json.loads(re.sub(r'@[A-Z_]+@', 'x', text)).get("realm", "")
if not realm_name:
    sys.exit("FAIL: template has no realm name")
OUT = os.path.join(OUT_DIR, f"{realm_name}-realm.json")
placeholders = sorted(set(re.findall(r'@([A-Z_]+)@', text)))

missing = [p for p in placeholders if not os.environ.get(p)]
if missing:
    sys.exit("FAIL: unresolved realm placeholders (set in .env): " + ", ".join(missing))

for p in placeholders:
    # JSON-escape the value (json.dumps adds quotes; strip them) so any character is safe.
    escaped = json.dumps(os.environ[p])[1:-1]
    text = text.replace(f"@{p}@", escaped)

try:
    realm = json.loads(text)
except json.JSONDecodeError as e:
    sys.exit(f"FAIL: rendered realm is not valid JSON: {e}")

mode = "clean (no users)"
if os.environ.get("RENDER_DEMO") == "1":
    overlay = json.load(open(DEMO_USERS))
    realm["users"] = overlay["users"]
    unpinned = [u["username"] for u in realm["users"] if "id" not in u]
    if unpinned:
        sys.exit("FAIL: demo users missing pinned UUIDs (7c3 invariant): " + ", ".join(unpinned))
    # KC26 VALIDATES the realm password policy against imported user credentials (A5 defect
    # #6) — the formula dev passwords would abort the whole import. Mirror history instead:
    # the dev realm's policy arrived AFTER its users existed (Talus 48, live API), so demo
    # imports policy-free and seed-demo.sh SETS the policy afterward — users grandfathered,
    # realm ends policy-enforcing, byte-for-byte the dev stack's real shape.
    realm.pop("passwordPolicy", None)
    mode = f"demo (+{len(realm['users'])} pinned dev users; policy set post-seed)"

os.makedirs(OUT_DIR, exist_ok=True)
for stale in os.listdir(OUT_DIR):
    if stale.endswith("-realm.json") and stale != os.path.basename(OUT):
        os.remove(os.path.join(OUT_DIR, stale))
        print(f"  removed stale render {OUT_DIR}/{stale}")
# The rendered realm holds two client secrets in clear (one of them can reset the portal
# administrator's password), so it is written owner-only and then opened up exactly as far as the
# sign-in server needs (once-over S3, decision-once-over-edge-deployment.md E2). Keycloak reads it
# through a read-only bind mount, inside its container, as uid 1000 / gid 0 — so "owner-only" for
# an installer who is neither would stop a fresh install at the realm import. Three cases:
#   - run as root (the documented path, sudo): group 0, mode 0640 — the container reads it as a
#     member of group 0; no other local account can.
#   - run as uid 1000 (not root): 0600 — the container reads it as its owner.
#   - macOS (Docker Desktop reads bind mounts as the signed-in user, whatever the container's
#     uid): 0600, owned by that user — under sudo the file is handed back to SUDO_UID first, since
#     a root-owned file would be unreadable to Docker Desktop.
#   - anything else — another uid, or a Docker that remaps container ids (rootless, userns-remap:
#     the container's "uid 1000 / gid 0" are then other ids on the host): the file is left
#     readable (0644) and says so. Failing the install to protect the file would be the worse
#     outcome; before this change the file was always 0644.
# The mode is set on the open descriptor BEFORE anything is written (an existing file keeps its
# old mode through O_CREAT), and a symlink at the path is refused rather than followed.
KC_UID, KC_GID = 1000, 0
remapped = os.environ.get("RENDER_DOCKER_REMAPPED") == "1"
fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(realm, f, indent=2)
    f.write("\n")
euid = os.geteuid()
if sys.platform == "darwin":
    sudo_uid, sudo_gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if euid == 0 and sudo_uid and sudo_uid.isdigit():
        os.chown(OUT, int(sudo_uid), int(sudo_gid) if sudo_gid and sudo_gid.isdigit() else -1)
        perms = "0600, owned by the user who ran sudo"
    elif euid == 0:
        os.chmod(OUT, 0o644)
        perms = "0644 — LEFT READABLE: run as root on macOS with no SUDO_UID, so the Docker Desktop user is unknown"
    else:
        perms = "0600 (owner only)"
elif remapped:
    os.chmod(OUT, 0o644)
    perms = "0644 — LEFT READABLE: this Docker remaps container ids (rootless or userns-remap), so the sign-in server's ids on this host are not known"
elif euid == 0:
    os.chown(OUT, -1, KC_GID)
    os.chmod(OUT, 0o640)
    perms = "0640, group 0 (the sign-in server's group)"
elif euid == KC_UID:
    perms = "0600 (owner only)"
else:
    os.chmod(OUT, 0o644)
    perms = "0644 — LEFT READABLE: this user cannot give the file to the sign-in server's group; run with sudo to tighten it"
print(f"  permissions: {perms}")

print(f"  rendered {OUT} from {TEMPLATE} — {mode}")
print(f"  substituted: {', '.join(placeholders)}")
EOF
