#!/usr/bin/env bash
# talus-setup — the ONE-question hostname prompt (decision-talus-setup.md, D1–D8).
#
# Re-runnable console command. Asks what hostname users will reach the portal at;
# [Enter] means IP mode at this machine's CURRENT address (on a fresh appliance a no-op,
# on a converted one an honest revert, and on a box whose address changed the supported
# re-derivation path). A real name collapses TLS into the proven SNI shape: the portal
# and auth.<name> share :443, certs are per-name (internal CA by default, ACME on
# confirm — D3), and the IP-mode overlay retires.
#
# This script DRIVES install.sh --edge (the wiring owner: PORTAL_ORIGIN, ISSUER, CORS,
# KC_PUBLIC_URL, config.json all derive there) and never duplicates the wiring. The few
# steps it shares with firstboot (IP derivation, overlay copy) mirror
# packaging/firstboot/talus-firstboot.sh — change one, check the other.
#
# Invoked on the appliance via the baked shim /usr/local/sbin/talus-setup (D2).
#
# Talus+ (decision-paid-packaging.md p3):
#   talus-setup --plus BUNDLE.tar.gz [--licence FILE] [--check]
# verifies the bundle's checksums (refusing on ANY mismatch), loads the plus api image,
# stages the plus web app (previous dist kept beside it for rollback), places the compose
# fragment, pins COMPOSE_FILE in .env so every plain `docker compose` includes it from then
# on, optionally places the license, and restarts. UPGRADES are the same command with the
# next bundle. --check stops after verification (a dry run). The zero-argument behavior —
# the one-question hostname prompt — is unchanged.
# Unattended (an install driven by another tool or a script):
#   talus-setup --hostname NAME [--public-cert]
# answers the one question from the command line; --public-cert is the ACME confirm
# (default stays the internal CA). Same D5 fence, same wiring — nothing else changes.
# Guided first run (decision-first-run-setup.md §12, w1):
#   talus-setup            on a machine with NO install yet
# is the one command: it asks who the administrator is (name, email, a password of their own
# choosing) and where the portal lives, installs unattended, applies a Talus+ bundle found beside
# the release folder, and writes talus-summary.txt. Flags never enter that path.
# bash 3.2-safe. Never prints .env contents (secrets file) — derived values only.
set -u

say() { echo "talus-setup: $*"; }
die() { echo "talus-setup: ERROR — $*" >&2; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || die "cannot cd to release tree"
[ -f ./install.sh ] || die "install.sh not found — run from the Talus release tree"

APPLIANCE=0
[ -d /opt/talus-firstboot ] && APPLIANCE=1
OVERRIDE=./docker-compose.override.yml
# The guided first run's "not finished yet" marker. A FILE of its own, never a key in .env: .env is
# a single-file bind mount into the api, and rewriting it (temp + rename) after the installer has
# started the api leaves the api reading the OLD file — marker included — until its next recreate,
# so an include-.env backup taken then would restore as a half-finished setup (security review
# 2026-10-05). With the marker out here, a plain first run never touches .env after the installer.
PENDING_MARK=./.talus-setup-pending

# .env holds every deployment secret, and install.sh's LAST act on it is chmod 600 + chown to the
# api's uid. A rewrite here is a NEW file (temp + mv), so it must be born private and handed to
# the same owner — otherwise the first write after install.sh quietly makes the secrets
# world-readable (security review 2026-10-04: the first-run marker removal did exactly that; the
# COMPOSE_FILE sync on the Talus+ path had the same shape). Never a script-wide umask: install.sh
# inherits it, and files other containers read (the realm import) would turn unreadable.
env_tmp() {
  # mktemp: mode 600 by construction and a name nobody can have left behind (a leftover from an
  # interrupted run would keep whatever mode it was born with).
  tmp="$(mktemp .env.tmp.XXXXXX 2>/dev/null)" || die "cannot write beside .env (run with sudo)"
  own="$(ls -ln .env 2>/dev/null | awk '{print $3":"$4}')"
  if [ -n "$own" ]; then chown "$own" "$tmp" 2>/dev/null || true; fi
}
# A rewrite that fails (a full disk is the usual cause) must STOP the run: these writes carry the
# settings that keep the database, Keycloak and the api off the LAN, and a silent miss would leave
# them published. .env itself is untouched (the rename never happened); the partial copy is removed.
env_fail() {
  rm -f "$tmp" 2>/dev/null
  die "could not update .env (is the disk full?) — .env is unchanged; fix the cause and run talus-setup again"
}
# Mirror of install.sh's upsert_env. .env is SOURCED by shell scripts, so any value that is not
# plainly a hostname/port/path is double-quoted: an apostrophe (O'Brien) or a leading ~ unquoted
# breaks every script that sources the file. Callers keep " $ ` \ | & out of values.
upsert_env() {
  key="$1"; val="$2"
  case "$val" in *[!A-Za-z0-9._:/@%+=,-]*) val="\"$val\"" ;; esac
  if grep -q "^${key}=" .env; then
    env_tmp
    if ! sed "s|^${key}=.*|${key}=${val}|" .env > "$tmp" || ! mv "$tmp" .env; then env_fail; fi
  else
    printf '%s=%s\n' "$key" "$val" >> .env || die "could not update .env (is the disk full?) — fix the cause and run talus-setup again"
  fi
}
remove_env() {
  if grep -q "^$1=" .env; then
    env_tmp
    grep -v "^$1=" .env > "$tmp"
    # grep -v exits 1 when it printed nothing (the key was the only line) — not a failure.
    if [ "$?" -gt 1 ] || ! mv "$tmp" .env; then env_fail; fi
  fi
}
env_value() { # <KEY> -> its value, surrounding double quotes removed
  grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//'
}

# D5 — the injection fence: strict RFC-1123 shape, or nothing is written anywhere.
validate_hostname() {
  h="$1"
  [ -n "$h" ] || { echo "empty"; return 1; }
  [ "${#h}" -le 253 ] || { echo "name is longer than 253 characters"; return 1; }
  if printf '%s' "$h" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "that is an IP address — press Enter for IP mode instead"; return 1
  fi
  case "$h" in
    *.*) : ;;
    *) echo "need a full name with at least two labels (e.g. gis.example.org) — single-label names break certificate issuance and DNS search domains"; return 1 ;;
  esac
  if ! printf '%s' "$h" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'; then
    echo "invalid hostname (allowed: lowercase letters, digits, hyphens; dot-separated labels, no leading/trailing hyphen)"; return 1
  fi
  return 0
}

# The same physical-interface derivation as firstboot (docker0/bridges must never match).
derive_ip() {
  ip -4 -o addr show scope global 2>/dev/null | awk '$2 ~ /^(en|eth)/ {print $4}' | cut -d/ -f1 | head -1
}

# ---- Talus+ (decision-paid-packaging.md p3) -------------------------------------------
PLUS_FRAGMENT=./compose.plus.fragment.yml

# COMPOSE_FILE in .env is how the plus fragment joins EVERY plain `docker compose` run with
# no -f flags to remember — and it must TRACK MODE STATE, because the IP-mode override file
# comes and goes with talus-setup re-runs (hostname mode deletes it). Free installs get no
# COMPOSE_FILE at all, so their behavior stays byte-identical to before Talus+ existed.
sync_compose_file() {
  if [ -f "$PLUS_FRAGMENT" ]; then
    files="docker-compose.yml"
    [ -f "$OVERRIDE" ] && files="${files}:docker-compose.override.yml"
    files="${files}:compose.plus.fragment.yml"
    upsert_env COMPOSE_FILE "$files"
  else
    remove_env COMPOSE_FILE
  fi
}

apply_plus() {
  [ -f "$PLUS_BUNDLE" ] || die "bundle not found: $PLUS_BUNDLE"
  [ -z "$LICENCE_FILE" ] || [ -f "$LICENCE_FILE" ] || die "license not found: $LICENCE_FILE"
  TMP="$(mktemp -d "${TMPDIR:-/tmp}/talus-plus-setup.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT
  say "unpacking bundle ..."
  tar -xzf "$PLUS_BUNDLE" -C "$TMP" 2>/dev/null || die "cannot unpack the bundle (corrupted download?)"
  BDIR="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d -name 'talus-plus-*' | head -1)"
  [ -n "$BDIR" ] || die "bundle layout unexpected (no talus-plus-<version>/ inside)"
  # AUTHENTICITY first (security-review advisory, 2026-09-05): the checksum manifest travels
  # inside the bundle, so alone it proves nothing against tampering. The manifest is
  # Ed25519-SIGNED at build time; the verifying key ships in THIS release tree — out of band
  # from the bundle — and the context prefix domain-separates bundle signatures from license
  # signatures (same key, different domains).
  SIGNING_PUB="${TALUS_BUNDLE_SIGNING_PUB:-scripts/talus-signing.pub.pem}"
  [ -f "$SIGNING_PUB" ] || die "verifying key not found ($SIGNING_PUB) — this release tree is too old for Talus+ bundles"
  [ ! -f "$BDIR/UNSIGNED" ] || die "this bundle is marked UNSIGNED (a local build experiment) — refusing. Real bundles are signed."
  [ -f "$BDIR/MANIFEST.sha256.sig" ] || die "bundle carries no manifest signature — refusing. Re-download it from your delivery link."
  say "verifying authenticity (Ed25519 signature over the manifest) ..."
  SIGIN="$TMP/sig-input.$$"
  { printf 'talus-bundle-manifest-v1\n'; cat "$BDIR/MANIFEST.sha256"; } > "$SIGIN"
  openssl pkeyutl -verify -rawin -pubin -inkey "$SIGNING_PUB" -in "$SIGIN" -sigfile "$BDIR/MANIFEST.sha256.sig" >/dev/null 2>&1 \
    || die "SIGNATURE INVALID — this bundle was not signed by Talus (or was altered). Do not install it; contact support with the delivery details. (openssl 1.1.1+ required for verification.)"
  rm -f "$SIGIN"
  say "verifying integrity (member checksums) ..."
  ( cd "$BDIR" && shasum -a 256 -c MANIFEST.sha256 >/dev/null 2>&1 ) \
    || die "CHECKSUM MISMATCH — refusing this bundle. Re-download it; a corrupted or altered bundle must never install."
  IMG_TAG="$(awk '$1=="image:"{print $2; exit}' "$BDIR/compose.plus.fragment.yml")"
  [ -n "$IMG_TAG" ] || die "the bundle's compose fragment names no image"
  say "bundle OK: $(basename "$BDIR")  (api image ${IMG_TAG})"
  if [ "$CHECK_ONLY" -eq 1 ]; then
    say "--check requested: verified only, nothing changed."
    return 0
  fi

  say "loading the Talus+ api image ..."
  docker load < "$BDIR"/images/talus-api-plus-*.tar >/dev/null || die "docker load failed"
  # Digest compare after load (decision-paid-packaging.md §5): the loaded image's id must
  # equal the signed IMAGE_DIGEST member — docker load of the wrong/edited image tar fails
  # here even though the tar itself was checksummed (belt + suspenders around docker's own
  # content addressing).
  WANT_ID="$(cat "$BDIR/IMAGE_DIGEST" 2>/dev/null)"
  [ -n "$WANT_ID" ] || die "bundle carries no IMAGE_DIGEST — refusing"
  GOT_ID="$(docker inspect --format '{{.Id}}' "$IMG_TAG" 2>/dev/null)"
  [ "$GOT_ID" = "$WANT_ID" ] || die "loaded image id does not match the signed IMAGE_DIGEST (got ${GOT_ID:-none}) — refusing"
  say "image digest verified (${WANT_ID})"

  # The bundle must have been built ON the api image this release pins — the real meaning of "the
  # matching bundle". Names can agree or differ (a release that changes no image, such as an
  # installer-only fix, keeps the previous bundle); the image layers cannot. A bundle built for
  # another release would run Talus+ code against the wrong core, so it is refused before anything
  # on this server changes. (Until 2026-10-03 the runbook asked the operator to compare two names
  # by eye, and nothing checked.)
  CORE_REF="$(awk '$1=="image:" && $2 ~ /talus-api@sha256:/ {print $2; exit}' docker-compose.yml 2>/dev/null)"
  # HOW it is compared, in order:
  #  1. The bundle says which api image it was built on (BASE_IMAGE_REF, a signed member, bundles
  #     from beta.14 on). Comparing that digest with the pin needs no image and no network, so it
  #     works on an air-gapped server and on an upgrade.
  #  2. An older bundle carries no such member: compare the image LAYERS instead. That needs the
  #     pinned image in the local Docker store, and on a Talus+ upgrade it is NOT there (the plus
  #     image runs in its place, so the installer never pulled it) — so it is pulled first.
  #  3. If neither can be done, the bundle is REFUSED. ★ Until beta.14 this case printed a note
  #     and continued, which silently skipped the check on exactly the upgrade it exists for
  #     (found rehearsing beta.13 -> beta.14 on a Talus+ fixture, 2026-10-03).
  BUNDLE_BASE="$(cat "$BDIR/BASE_IMAGE_REF" 2>/dev/null || true)"
  MATCH=unknown
  if [ -n "$CORE_REF" ] && [ -n "$BUNDLE_BASE" ]; then
    if [ "${BUNDLE_BASE##*@}" = "${CORE_REF##*@}" ]; then MATCH=yes; else MATCH=no; fi
  elif [ -n "$CORE_REF" ]; then
    docker image inspect "$CORE_REF" >/dev/null 2>&1 || docker pull -q "$CORE_REF" >/dev/null 2>&1 || true
    CORE_LAYERS="$(docker image inspect --format '{{join .RootFS.Layers " "}}' "$CORE_REF" 2>/dev/null)"
    PLUS_LAYERS="$(docker image inspect --format '{{join .RootFS.Layers " "}}' "$IMG_TAG" 2>/dev/null)"
    if [ -n "$CORE_LAYERS" ] && [ -n "$PLUS_LAYERS" ]; then
      case "$PLUS_LAYERS " in
        "$CORE_LAYERS "*) MATCH=yes ;;
        *) MATCH=no ;;
      esac
    fi
  fi
  case "$MATCH" in
    yes) say "bundle matches this release's api image" ;;
    no)
      if [ "$ALLOW_BASE_MISMATCH" -eq 1 ]; then
        say "WARNING: this bundle was built for a different release than the one installed — continuing because --allow-base-mismatch was given"
      else
        die "this bundle was built for a DIFFERENT Talus release than the one installed here (its api image is not built on the image docker-compose.yml pins). Use the bundle delivered for this release, or upgrade the release first (docs/upgrade-runbook.md). Nothing on this server was changed."
      fi ;;
    *)
      if [ "$ALLOW_BASE_MISMATCH" -eq 1 ]; then
        say "WARNING: could not confirm this bundle was built for the installed release — continuing because --allow-base-mismatch was given"
      else
        die "could not confirm this bundle was built for the Talus release installed here: it does not say which api image it was built on, and this release's api image could not be fetched to compare. Use the bundle delivered for this release. Nothing on this server was changed."
      fi ;;
  esac

  say "staging the Talus+ web app ..."
  rm -rf web/dist.plus-new
  mkdir -p web/dist.plus-new
  tar -xzf "$BDIR"/web-dist-plus-*.tar.gz -C web/dist.plus-new || die "cannot unpack the web app"
  [ -d web/dist.plus-new/dist ] || die "web app layout unexpected"
  # The web build is a tar made on the build machine, and tar run as root keeps the ARCHIVE's owner
  # (the build machine's uid) — so after this step the install user could no longer write
  # web/dist, and the NEXT core upgrade's overlay (rsync as that user) failed on every Talus+ box
  # (an upgrade rehearsal: 1,161 "Permission denied" lines under web/dist). Hand the
  # staged tree to whoever owns the install directory, as env_tmp() does for .env.
  dist_own="$(ls -lnd web 2>/dev/null | awk '{print $3":"$4}')"
  if [ -n "$dist_own" ]; then chown -R "$dist_own" web/dist.plus-new 2>/dev/null || true; fi
  STAMP="$(date '+%Y%m%d-%H%M%S')"
  if [ -d web/dist ]; then
    mv web/dist "web/dist.prev-$STAMP" || die "cannot set aside the current web app"
    say "previous web app kept at web/dist.prev-$STAMP (rollback = swap the directories back)"
  fi
  mv web/dist.plus-new/dist web/dist || die "cannot stage the web app"
  rmdir web/dist.plus-new 2>/dev/null

  cp "$BDIR/compose.plus.fragment.yml" "$PLUS_FRAGMENT" || die "cannot place the compose fragment"
  sync_compose_file

  say "starting Talus+ ..."
  # Only the services the fragment names (today: the api, whose image it pins) — with --no-deps.
  # A blanket `up -d` here re-runs every one-shot (roles-init, openfga-migrate, flyway) and, on a
  # fresh install, recreated keycloak-postgres and pg_tileserv as well (found on a release
  # rehearsal): Keycloak lost its database for a few seconds right after "Talus is up", so a
  # person signing in within ~30 s met an error page once. The fragment is the only thing that changed,
  # so only what it names is (re)created; the edge is recreated explicitly below. bash 3.2-safe: the
  # service names are read from the fragment's two-space-indented keys under `services:`.
  plus_services="$(awk '/^services:/{f=1; next} f && /^  [A-Za-z0-9_-]+:/{sub(":", "", $1); print $1} f && /^[^ ]/{f=0}' "$PLUS_FRAGMENT" | tr '\n' ' ')"
  [ -n "$plus_services" ] || plus_services="api"
  say "  (re)creating only what the bundle changes: $plus_services"
  # shellcheck disable=SC2086 — intentional word split on the space-joined service names
  docker compose up -d --no-deps $plus_services || die "docker compose up failed"

  # Wait for the api before touching it (fresh image pull-up can take a moment).
  i=0
  until curl -s -o /dev/null "http://127.0.0.1:8000/health" 2>/dev/null; do
    i=$((i+1)); [ "$i" -ge 30 ] && die "api did not come up — check: docker compose logs api"
    sleep 2
  done

  # caddy bind-mounts ./web/dist, and a bind
  # mount follows the directory INODE, not the path — after the mv swap above the running
  # edge keeps serving the PREVIOUS app until its container is recreated. `up -d` without
  # the edge profile never touches caddy, so recreate it explicitly. NEVER `restart` (same
  # container, same inode). Desktop installs without --edge have no caddy to recreate.
  if grep -E '^TALUS_INSTALL_MODE=' .env 2>/dev/null | grep -q 'edge'; then
    say "recreating the edge so the Talus+ web app is served ..."
    docker compose --profile edge up -d --force-recreate caddy \
      || die "caddy recreate failed — run: docker compose --profile edge up -d --force-recreate caddy"
  fi

  if [ -n "$LICENCE_FILE" ]; then
    # The licence's home is the storage volume (survives upgrades; NOT in a backup set); the
    # api verifies it on every read, so no restart is needed — and never store unverified
    # garbage: the api itself is the verifier, so a bad file simply reads as unlicensed and
    # the state probe below says so honestly.
    say "placing the license ..."
    docker compose exec -T api mkdir -p /storage/talus-licence || die "cannot create the license home"
    # Written BY the api's own user through stdin — never `docker compose cp`, which preserves the
    # host file's owner and mode: a license kept at mode 600 (a careful admin) arrived unreadable to
    # the non-root api and read as UNLICENSED (found at the beta.11 fixture install, 2026-10-03).
    # The rename also replaces a file an earlier run left unreadable.
    # The file may be an ACTIVATION FILE (the license plus a one-time AI activation code). Only its
    # license member is written — the code must never land on the storage volume or in a backup —
    # so the bytes pass through the api's own unwrap on the way in. A file that is not JSON stops here.
    docker compose exec -T api sh -c 'umask 027; python -c "
import json, sys
from app.entitlement import unwrap_activation
json.dump(unwrap_activation(json.load(sys.stdin)), sys.stdout)
" > /storage/talus-licence/licence.json.new && mv -f /storage/talus-licence/licence.json.new /storage/talus-licence/licence.json' \
      < "$LICENCE_FILE" || die "cannot place the license (is it the JSON file Talus sent, unmodified?)"
    if grep -q '"talus_activation"' "$LICENCE_FILE"; then
      ACTIVATION_HINT=1
    fi
  fi

  say "verifying entitlement state ..."
  STATE="$(docker compose exec -T api python -c '
from app.entitlement_runtime import current_status
s = current_status()
print(("LICENSED: " + ",".join(sorted(s.modules))) if s.signed_and_valid else "UNLICENSED")
' 2>/dev/null | tail -1)"
  # tail -1: the api logs its own warnings to stdout (a missing license is one), and only the
  # verdict line belongs in the banner below.
  [ -n "$STATE" ] || STATE="probe failed"

  echo ""
  echo "  ====================================================================="
  echo "   Talus+ is installed.  Entitlement: ${STATE}"
  if [ "$STATE" = "UNLICENSED" ]; then
    echo ""
    echo "   No valid license is in place yet. Either re-run with"
    echo "     talus-setup --plus <bundle> --licence /path/to/licence.json"
    echo "   or sign in as an administrator and upload it in the portal."
    echo "   Talus+ features stay hidden until a license is present; everything"
    echo "   else runs normally."
  fi
  if [ "${ACTIVATION_HINT:-0}" = "1" ]; then
    echo ""
    echo "   That file is an activation file: its license is now in place. To turn"
    echo "   on the AI assists, sign in as an administrator and upload the SAME file"
    echo "   at Administration > Talus+ > License (or paste its activation code at"
    echo "   Administration > AI). The code works once and expires on the date in"
    echo "   the file; the license does not expire with it."
  elif [ "$STATE" != "${STATE#LICENSED:}" ] && printf '%s\n' "${STATE#LICENSED: }" | tr ',' '\n' | grep -qx ai; then
    # A self-install run: "LICENSED: ai,..." read as
    # "the AI assists are on" — the plain license file installs the modules, but AI needs the one-time
    # activation code that only an ACTIVATION FILE carries, and nothing on the screen said so.
    echo ""
    echo "   The AI assists are NOT on yet: \"ai\" in the license unlocks them, but they"
    echo "   also need an ACTIVATION FILE from Talus (this license wrapped with a one-time"
    echo "   code). When you have it, sign in as an administrator and upload it at"
    echo "   Administration > Talus+ > License (or paste its code at Administration > AI);"
    echo "   Administration > AI then reads Connected. Every other Talus+ module works now."
    echo "   (Already Connected from an earlier activation? Then there is nothing to do.)"
  fi
  echo ""
  echo "   Upgrades: run the standard core upgrade, then this same command with"
  echo "   the matching new bundle. Your license and data are untouched by both."
  echo "  ====================================================================="
  return 0
}

# ---- guided first run (decision-first-run-setup.md §12, w1) ----------------------------
# Every question is asked BEFORE anything is installed, so a refusal here leaves the machine
# untouched. Three tries each; a closed stdin (no terminal) is a refusal, never a default.
answer() { # <prompt> [silent] -> REPLY_LINE
  printf '%s' "$1"
  if [ "${2:-}" = "silent" ]; then
    IFS= read -rs REPLY_LINE || die "setup needs its answers typed at a terminal — nothing was installed"
    echo ""
  else
    IFS= read -r REPLY_LINE || die "setup needs its answers typed at a terminal — nothing was installed"
  fi
}

# The values below are written to .env, which the shell SOURCES — so a name may not carry anything
# the shell or sed would act on. Parentheses are the Keycloak standing rule (name fields reject them).
# A name may be one word ("Dispatch", a shared counter sign-in, a person with a single name): the
# installer makes the last name optional in the sign-in system before the administrator is created
# (scripts/sync-user-profile.sh, decision-one-word-names.md). What Keycloak still refuses in a name
# is the punctuation below — and it says so only when the account is created, which is the
# installer's LAST step. So that is settled here, up front.
check_name() {
  [ -n "$1" ] || { echo "a name is needed"; return 1; }
  [ "${#1}" -le 100 ] || { echo "that name is longer than 100 characters"; return 1; }
  if printf '%s' "$1" | LC_ALL=C grep -q '[][(){}<>"$`\\|&;%!#?*~/^=[:cntrl:]]'; then
    echo "names can use letters, spaces, hyphens, apostrophes and full stops — not brackets, quotes or other punctuation"; return 1
  fi
  return 0
}
check_email() {
  [ "${#1}" -le 254 ] || { echo "that address is longer than 254 characters"; return 1; }
  printf '%s' "$1" | grep -Eq '^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$' \
    || { echo "that does not look like an email address (for example pat@example.org)"; return 1; }
  return 0
}
# The realm's password policy (keycloak/realm-export.template.json), checked before anything is
# installed. create-initial-admin.sh checks again — that one is the gate, this one is the courtesy.
check_password() { # <password> <email>
  [ "${#1}" -ge 12 ] || { echo "at least 12 characters are needed"; return 1; }
  case "$1" in *[[:upper:]]*) : ;; *) echo "an upper-case letter is needed"; return 1 ;; esac
  case "$1" in *[[:lower:]]*) : ;; *) echo "a lower-case letter is needed"; return 1 ;; esac
  case "$1" in *[[:digit:]]*) : ;; *) echo "a digit is needed"; return 1 ;; esac
  [ "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" != "$2" ] || { echo "the password cannot be your email address"; return 1; }
  return 0
}

first_run_questions() {
  echo ""
  echo "  Talus setup — a few questions, then the install runs by itself (about two minutes)."
  echo "  Nothing is installed until every question is answered."
  echo ""
  # A run that died partway already told the installer who the administrator is — and may already
  # have created them. Asking again would let a different email mint a SECOND administrator and
  # leave the first one behind, so a resumed run keeps the identity and asks only for the rest.
  FR_NAME="" FR_EMAIL="" FR_RESUMED=0
  if [ -f ./.env ] && [ -f "$PENDING_MARK" ]; then
    FR_EMAIL="$(env_value ADMIN_EMAIL)"
    FR_NAME="$(env_value ADMIN_DISPLAY_NAME)"
    # What is read back is checked like what is typed: a value saved by an older talus-setup or
    # edited by hand would otherwise never be asked for again, and the install would fail at its
    # very last step on every retry. A usable email is kept even when the name is not — the
    # email is the identity an earlier attempt may already have created.
    check_email "$FR_EMAIL" >/dev/null 2>&1 || FR_EMAIL=""
    check_name "$FR_NAME" >/dev/null 2>&1 || FR_NAME=""
    if [ -n "$FR_EMAIL" ] && [ -n "$FR_NAME" ]; then
      FR_RESUMED=1
      echo "  Continuing the setup that was started for:  ${FR_NAME} <${FR_EMAIL}>"
      echo ""
    elif [ -n "$FR_EMAIL" ]; then
      echo "  Continuing the setup that was started for:  ${FR_EMAIL}"
      echo "  (the name saved with it cannot be used, so it is asked for again)"
      echo ""
    fi
  fi
  if [ -z "$FR_NAME" ]; then
  t=0
  while [ "$t" -lt 3 ]; do
    answer "Your name: "
    cand="$(printf '%s' "$REPLY_LINE" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    if why="$(check_name "$cand")"; then FR_NAME="$cand"; break; fi
    echo "  $why"; t=$((t+1))
  done
  [ -n "$FR_NAME" ] || die "no usable name after three tries — nothing was installed"
  fi

  if [ -z "$FR_EMAIL" ]; then
  t=0
  while [ "$t" -lt 3 ]; do
    answer "Your email (you can sign in with it, or as admin): "
    cand="$(printf '%s' "$REPLY_LINE" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    if why="$(check_email "$cand")"; then FR_EMAIL="$cand"; break; fi
    echo "  $why"; t=$((t+1))
  done
  [ -n "$FR_EMAIL" ] || die "no usable email address after three tries — nothing was installed"
  fi

  echo "Choose a password: 12 or more characters, with an upper-case letter, a lower-case"
  echo "letter and a digit. It is not shown as you type, and Talus keeps no copy of it."
  t=0; FR_PW=""
  while [ "$t" -lt 3 ]; do
    answer "Password: " silent; p1="$REPLY_LINE"
    # Judged as soon as it is typed: a password that cannot be used is not worth typing twice.
    if ! why="$(check_password "$p1" "$FR_EMAIL")"; then
      echo "  $why"
    else
      answer "The same password again: " silent; p2="$REPLY_LINE"
      if [ "$p1" = "$p2" ]; then FR_PW="$p1"; break; fi
      echo "  the two did not match"
    fi
    t=$((t+1))
  done
  p1=""; p2=""
  [ -n "$FR_PW" ] || die "no acceptable password after three tries — nothing was installed"
}

# Talus+ files sitting beside the install: the release folder itself, or the folder it was unpacked
# in ("three files in a folder"). The bundle is VERIFIED here, before the install starts — a bad
# download is better reported now than two minutes in — and a bundle that fails is left out: the
# core install is never held hostage to an optional file.
first_run_find_plus() {
  FR_BUNDLE="" FR_LICENCE=""
  n=0
  for f in ./talus-plus-*.tar.gz ../talus-plus-*.tar.gz; do
    [ -f "$f" ] || continue
    n=$((n+1)); FR_BUNDLE="$f"
  done
  [ "$n" -gt 0 ] || return 0
  if [ "$n" -gt 1 ]; then
    say "found $n Talus+ bundles beside the install — not guessing which. After setup, run: scripts/talus-setup.sh --plus <bundle> --licence <file>"
    FR_BUNDLE=""; return 0
  fi
  say "found a Talus+ bundle: $FR_BUNDLE — verifying it ..."
  CHK="$(mktemp "${TMPDIR:-/tmp}/talus-plus-check.XXXXXX")" || die "cannot create a temporary file"
  if ! ( PLUS_BUNDLE="$FR_BUNDLE"; LICENCE_FILE=""; CHECK_ONLY=1; apply_plus ) > "$CHK" 2>&1; then
    echo "  $(tail -1 "$CHK")"
    echo "  Talus+ will NOT be installed in this run; the rest of the install goes ahead."
    echo "  Fetch the bundle again, then run: scripts/talus-setup.sh --plus <bundle> --licence <file>"
    rm -f "$CHK"
    FR_BUNDLE=""; return 0
  fi
  rm -f "$CHK"
  # A license or an activation file: a JSON file that says so. Exactly one, or none is offered.
  n=0
  for f in ./*.json ../*.json; do
    [ -f "$f" ] || continue
    if grep -q '"talus_activation"' "$f" 2>/dev/null || { grep -q '"signature"' "$f" 2>/dev/null && grep -q '"modules"' "$f" 2>/dev/null; }; then
      n=$((n+1)); FR_LICENCE="$f"
    fi
  done
  if [ "$n" -gt 1 ]; then
    say "found $n license files beside the install — not guessing which; upload yours in the portal after signing in"
    FR_LICENCE=""
  fi
  if [ -n "$FR_LICENCE" ]; then
    answer "Install Talus+ from $FR_BUNDLE with the license $FR_LICENCE? [Y/n]: "
  else
    answer "Install Talus+ from $FR_BUNDLE (no license file found beside it — you can upload one in the portal)? [Y/n]: "
  fi
  case "$REPLY_LINE" in n|N|no|NO|No) FR_BUNDLE=""; FR_LICENCE="" ;; esac
}

# ---- arguments ------------------------------------------------------------------------
ARGC=$#
PLUS_BUNDLE="" LICENCE_FILE="" CHECK_ONLY=0 ARG_HOSTNAME="" ARG_HOSTNAME_SET=0 ARG_PUBLIC_CERT=0 ALLOW_BASE_MISMATCH=0
USAGE="usage: talus-setup [--hostname NAME [--public-cert]] | [--plus BUNDLE [--licence FILE] [--check]]"
while [ $# -gt 0 ]; do
  case "$1" in
    --plus)             [ $# -ge 2 ] || die "--plus needs a bundle path"; PLUS_BUNDLE="$2"; shift 2 ;;
    --licence|--license) [ $# -ge 2 ] || die "--licence needs a file path"; LICENCE_FILE="$2"; shift 2 ;;
    --check)            CHECK_ONLY=1; shift ;;
    --allow-base-mismatch) ALLOW_BASE_MISMATCH=1; shift ;;      # support use only — see apply_plus
    --hostname)         [ $# -ge 2 ] || die "--hostname needs a name"; ARG_HOSTNAME="$2"; ARG_HOSTNAME_SET=1; shift 2 ;;
    --public-cert)      ARG_PUBLIC_CERT=1; shift ;;
    *) die "unknown argument: $1  ($USAGE)" ;;
  esac
done
[ -n "$LICENCE_FILE" ] && [ -z "$PLUS_BUNDLE" ] && die "--licence requires --plus"
[ "$CHECK_ONLY" -eq 1 ] && [ -z "$PLUS_BUNDLE" ] && die "--check requires --plus"
[ -n "$PLUS_BUNDLE" ] && [ "$ARG_HOSTNAME_SET" -eq 1 ] && die "--plus and --hostname are separate runs ($USAGE)"
[ "$ARG_PUBLIC_CERT" -eq 1 ] && [ "$ARG_HOSTNAME_SET" -eq 0 ] && die "--public-cert requires --hostname (a public certificate needs a name)"
# The guided first run: no arguments, and either nothing is installed or an earlier guided run
# did not finish (it must ask again — the password was never stored).
FIRST_RUN=0
if [ "$ARGC" -eq 0 ]; then
  if [ ! -f ./.env ] || [ -f "$PENDING_MARK" ]; then FIRST_RUN=1; fi
fi
if [ "$FIRST_RUN" -eq 0 ]; then
  [ -f ./.env ] || die "no .env — nothing is installed here yet. Run talus-setup with no arguments for the guided setup, or ./install.sh first"
  [ -w ./.env ] || die "need write access to .env (run with sudo)"
fi
if [ -n "$PLUS_BUNDLE" ]; then
  apply_plus
  exit $?
fi

if [ "$FIRST_RUN" -eq 1 ]; then
  [ ! -f ./.env ] || [ -w ./.env ] || die "need write access to .env (run with sudo)"
  ./scripts/preflight.sh || die "this machine is not ready for Talus yet — fix the item above and run talus-setup again. Nothing was installed."
  first_run_questions
else
  # ---- current state ------------------------------------------------------------------
  CUR_HOST="$(grep -E '^TALUS_HOSTNAME=' .env | cut -d= -f2- | tr -d '"' || true)"
  if [ -f "$OVERRIDE" ]; then
    say "current mode: IP mode (${CUR_HOST:-unset})"
  else
    say "current mode: hostname mode (${CUR_HOST:-unset})"
  fi
fi

# ---- the one question -----------------------------------------------------------------
if [ "$ARG_HOSTNAME_SET" -eq 1 ]; then
  RAW="$ARG_HOSTNAME"
  say "hostname given on the command line (unattended)"
else
  printf 'What hostname will users reach this portal at?\n[Enter = IP mode at this machine'"'"'s current address]: '
  if ! read -r RAW; then
    # A closed stdin used to read as Enter. On a first run that would install on a guess.
    [ "$FIRST_RUN" -eq 0 ] || die "setup needs its answers typed at a terminal — nothing was installed"
  fi
fi
NAME="$(printf '%s' "$RAW" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
[ "$ARG_HOSTNAME_SET" -eq 1 ] && [ -z "$NAME" ] && die "--hostname was empty (unattended runs never fall back to IP mode)"

TLS_FLAG="--tls-internal"
MODE="ip"
if [ -n "$NAME" ]; then
  MODE="hostname"
  reason="$(validate_hostname "$NAME")" || die "$reason"
  AUTH="auth.${NAME}"
  # D3 — RULED: one confirm with a safe default. Inference cannot be honest (a
  # real-looking name can be LAN-only, and ACME on an unreachable host retries forever
  # while serving nothing). Default N = internal CA = always works.
  if [ "$ARG_HOSTNAME_SET" -eq 1 ]; then
    # Unattended: the confirm is the flag. Absent flag = internal CA, the same safe default.
    if [ "$ARG_PUBLIC_CERT" -eq 1 ]; then TLS_FLAG=""; say "public certificate requested (--public-cert)"; else TLS_FLAG="--tls-internal"; fi
  else
    printf 'Should this portal request a public certificate automatically?\nRequires this server to be reachable from the internet on ports 80/443. [y/N]: '
    read -r ANS
    case "$ANS" in
      y|Y|yes|YES|Yes) TLS_FLAG="" ;;
      *) TLS_FLAG="--tls-internal" ;;
    esac
  fi
fi

# ---- first run: Talus+ beside the install, then the secrets ----------------------------
if [ "$FIRST_RUN" -eq 1 ]; then
  first_run_find_plus
  if [ ! -f ./.env ]; then
    say "generating this installation's secrets ..."
    ./scripts/generate-secrets.sh >/dev/null || die "could not generate the secrets file — nothing was installed"
  fi
  # From here the machine is changing. The marker makes the next plain run the guided one again
  # if this one dies; the identity is kept because install.sh is re-run on every rename and would
  # otherwise look for 'admin', not find it, and mint a second administrator.
  : > "$PENDING_MARK" || die "cannot write in the release folder (run with sudo)"
  # The first administrator is the account `admin`
  # — an account that belongs to the installation, not to one
  # person, so it survives a change of staff and there is never a question of whose it is. The
  # person's real email is attached to it, and the realm lets a person sign in by email, so the
  # email they just typed works too (on one early install the old `admin` carried a placeholder address,
  # which is why an email was "user not found").
  upsert_env ADMIN_USERNAME admin
  upsert_env ADMIN_EMAIL "$FR_EMAIL"
  upsert_env ADMIN_DISPLAY_NAME "$FR_NAME"
fi

# ---- apply ----------------------------------------------------------------------------
# D4 — port hygiene, unconditional in BOTH modes: the LAN sees only the edge. The
# *_PUBLISH vars are compose-only publish specs; *_PORT vars stay pure port numbers
# (URL/DSN consumers — build finding, D4). Requires the beta.2 compose (publish vars);
# on an older compose these upserts are inert and the beta.2 upgrade closes the doors.
upsert_env POSTGRES_PUBLISH "127.0.0.1:5432"
upsert_env KEYCLOAK_PUBLISH "127.0.0.1:8080"
upsert_env API_PUBLISH "127.0.0.1:8000"

if [ "$MODE" = "ip" ]; then
  IP="$(derive_ip)"
  [ -n "$IP" ] || die "no IPv4 address on a physical interface — plug in a network and re-run"
  say "IP mode at ${IP}"
  # decision-install-reachability.md F2 (F5 finding #6): the overlay ships in the release
  # tree (packaging/firstboot/), so IP mode works on tarball installs too. ★ The IN-TREE copy
  # wins when present, on an appliance too (2026-10-11): the overlay imports the release
  # Caddyfile and must match its shape — since the release file carries Caddy's one global
  # options block, an older baked overlay (its own global block first) would make Caddy refuse
  # to start after a tree upgrade. The baked copy is the fallback for a tree without one.
  if [ -f packaging/firstboot/compose.ip-mode.override.yml ]; then
    # The shipped override mounts the appliance path; rewrite the volume SOURCE to the
    # in-tree copy. The appliance file itself is untouched (firstboot mirrors it).
    sed 's|/opt/talus-firstboot/Caddyfile.ip-mode|./packaging/firstboot/Caddyfile.ip-mode|' \
      packaging/firstboot/compose.ip-mode.override.yml > "$OVERRIDE" \
      || die "cannot write the IP-mode override"
  elif [ "$APPLIANCE" -eq 1 ]; then
    cp /opt/talus-firstboot/compose.ip-mode.override.yml "$OVERRIDE" || die "cannot write the IP-mode override"
  else
    die "IP-mode overlay not found (packaging/firstboot/ missing from this tree)"
  fi
  sync_compose_file
  export TALUS_HOSTNAME="$IP"
  export KEYCLOAK_HOSTNAME="${IP}:8443"
  TLS_FLAG="--tls-internal"   # load-bearing for fallback_sni (B6), not just posture
  DISPLAY_PORTAL="https://${IP}"
  DISPLAY_AUTH="https://${IP}:8443"
else
  say "hostname mode: portal ${NAME}, auth ${AUTH}"
  rm -f "$OVERRIDE"
  sync_compose_file
  export TALUS_HOSTNAME="$NAME"
  export KEYCLOAK_HOSTNAME="$AUTH"
  DISPLAY_PORTAL="https://${NAME}"
  DISPLAY_AUTH="https://${AUTH}"
fi

# install.sh --edge owns ALL derived wiring; its compose up recreates env-changed
# services. Re-runnability is bite-proven (Stage 0 firstboot re-runs).
# TALUS_DRIVEN_BY_SETUP tells the installer its failure messages must send the person back HERE,
# not to install.sh or create-initial-admin.sh by name (which would skip the chosen password).
say "running install.sh --edge ${TLS_FLAG} ..."
# shellcheck disable=SC2086   # TLS_FLAG is deliberately word-split ("" or one flag)
if [ "$FIRST_RUN" -eq 1 ]; then
  # The chosen password rides the installer's ENVIRONMENT to create-initial-admin.sh: never an
  # argument (ps), never .env, never a file. Not stdin — install.sh runs `docker compose run`,
  # which eats it.
  # create-initial-admin.sh reports back, through a file it is pointed at, whether it CREATED the
  # administrator or found one from an earlier run (whose password it never changes) — the closing
  # words must not promise "the password you just chose" when it is not the one in force.
  FR_RESULT="$(mktemp "${TMPDIR:-/tmp}/talus-admin-result.XXXXXX")" || die "cannot create a temporary file"
  # shellcheck disable=SC2086
  if ! TALUS_DRIVEN_BY_SETUP=1 TALUS_ADMIN_PASSWORD="$FR_PW" TALUS_ADMIN_RESULT_FILE="$FR_RESULT" ./install.sh --edge $TLS_FLAG; then
    rm -f "$FR_RESULT"
    die "the install did not finish — fix the cause above and run talus-setup again. It will ask for your password again (it was never stored) and resume."
  fi
  FR_PW=""
  FR_ADMIN_STATE="$(cat "$FR_RESULT" 2>/dev/null)"; rm -f "$FR_RESULT"
  rm -f "$PENDING_MARK"
else
  # shellcheck disable=SC2086
  TALUS_DRIVEN_BY_SETUP=1 ./install.sh --edge $TLS_FLAG || die "install.sh failed — fix the cause and re-run talus-setup (safe to repeat)"
fi

# The edge changed identity (config/ports/command) — force-recreate is the documented
# overlay transition (compose.ip-mode.override.yml rollback contract). NEVER `restart`.
docker compose --profile edge up -d --force-recreate caddy || die "caddy recreate failed"

# D6 — machine hostname: set from the portal name (hostname mode); never churn on Enter.
if [ "$MODE" = "hostname" ] && [ "$APPLIANCE" -eq 1 ] && command -v hostnamectl >/dev/null 2>&1; then
  hostnamectl set-hostname "$NAME" 2>/dev/null && say "machine hostname set to ${NAME}" \
    || say "note: could not set machine hostname (non-fatal)"
fi

# ---- appliance banner (URLs only — never credentials) ---------------------------------
if [ "$APPLIANCE" -eq 1 ] && [ -w /etc/issue ]; then
  cat > /etc/issue <<EOF

  =====================================================
   Talus GIS appliance
     Portal:  ${DISPLAY_PORTAL}
     Auth:    ${DISPLAY_AUTH}
     Reconfigure anytime:  sudo talus-setup
     Clear this banner:    sudo talus-clear-banner
  =====================================================

EOF
  # getty renders /etc/issue only when respawning its prompt (B6 lesson); --no-block:
  # unit-context scripts never synchronously start units they may be ordered against.
  systemctl --no-block try-restart getty@tty1.service serial-getty@ttyS0.service 2>/dev/null || true
fi

# ---- operator guidance ----------------------------------------------------------------
print_guidance() {
echo ""
echo "  ====================================================================="
echo "   Talus is up."
echo "     Portal:  ${DISPLAY_PORTAL}"
echo "     Auth:    ${DISPLAY_AUTH}"
if [ "$FIRST_RUN" -eq 1 ]; then
  echo ""
  echo "   Sign in as:  admin   (your email works too: ${FR_EMAIL})"
  if [ "${FR_ADMIN_STATE:-}" = "existed" ]; then
    echo "   Password:    this administrator was already created by an earlier run, and"
    echo "                its password was NOT changed — use the one chosen THEN, not the"
    echo "                one typed just now. Lost it? See the admin-password recovery in"
    echo "                docs/install-runbook.md."
  else
    echo "   Password:    the one you chose a moment ago (Talus kept no copy)."
  fi
fi
if [ "$MODE" = "hostname" ]; then
  IP_NOW="$(derive_ip)"
  echo ""
  echo "   Create these two DNS records (same address):"
  # NOTE: no ${VAR:-...} default here — bash honors quote chars INSIDE a double-quoted
  # expansion's default word, so an apostrophe there (machine's) quote-swallowed the
  # entire next echo line (Stage 2 finding). Fallback assigned separately instead.
  [ -n "$IP_NOW" ] || IP_NOW="(this machine's IP)"
  echo "     ${NAME}    A    ${IP_NOW}"
  echo "     ${AUTH}    A    ${IP_NOW}"
  echo "   DNS not resolving yet is fine — the portal is already up; browse once"
  echo "   the records land."
fi
echo ""
echo "   Reserve this machine's IP address in your DHCP server (router/AD DHCP)."
echo "   Note: a re-imaged appliance presents a NEW DHCP identity — reservations"
echo "   do not follow it; re-create them after a re-image."
if [ "$FIRST_RUN" -eq 1 ]; then
  # What a fresh install still lacks after this screen (decision-first-run-setup.md W17; a
  # self-install run): the street basemap and the advertised base URL are both first-hour
  # steps only an administrator can take, and until this line nothing on the screen said so — the
  # verification suite's SKIP reasons were the only place a person could learn it.
  echo ""
  echo "   Two things to do in the first hour, after signing in (docs/install-runbook.md §6):"
  echo "     - Admin > Basemap: maps stream OpenStreetMap from the start. Download your"
  echo "       area there (\"Use last map view\" after zooming a map to it) for offline"
  echo "       use; the Field app's packs need it."
  echo "     - Admin > Connections: set the advertised base URL — the address desktop"
  echo "       clients, the Field app and QR links use to reach this server (for most"
  echo "       installs it is the portal address above). Field projects cannot be"
  echo "       generated until it is set."
fi
if [ "$TLS_FLAG" = "--tls-internal" ]; then
  echo ""
  # Said HERE, where the person is looking, not only in the runbook: skipping the second address is
  # the stumble every first sign-in has hit (the page loads, and the Log in button does nothing).
  echo "   ★ Before you sign in: open BOTH addresses above in your browser, once each,"
  echo "     and accept the certificate warning on each. If the second (Auth) is skipped,"
  echo "     the portal loads but its Log in button does nothing."
  echo ""
  echo "   TLS uses this appliance's own certificate authority. Browsers warn until"
  echo "   the CA is trusted — and Chrome disables ServiceWorkers (the Field app)"
  echo "   on untrusted origins. Export the CA root and install it on client devices:"
  echo "     docker compose --profile edge cp caddy:/data/caddy/pki/authorities/local/root.crt talus-root-ca.crt"
  # The root is named for THIS deployment (two Talus boxes used to mint
  # indistinguishable "Caddy Local Authority" roots, and a phone trusted the wrong one), and its
  # fingerprint is printed so the person installing it can match what the phone shows. Best effort:
  # the CA may not exist until the first TLS handshake, and openssl may be absent on a minimal host.
  ROOT_PEM="$(docker compose --profile edge exec -T caddy cat /data/caddy/pki/authorities/local/root.crt 2>/dev/null || true)"
  if [ -n "$ROOT_PEM" ] && command -v openssl >/dev/null 2>&1; then
    ROOT_CN="$(printf '%s\n' "$ROOT_PEM" | openssl x509 -noout -subject 2>/dev/null | sed 's/^subject=//; s/^ *CN *= *//')"
    ROOT_FP="$(printf '%s\n' "$ROOT_PEM" | openssl x509 -noout -fingerprint -sha256 2>/dev/null | sed 's/^.*=//')"
    [ -n "$ROOT_CN" ] && echo "   The root is named \"${ROOT_CN}\" — that is the name a phone shows under its"
    [ -n "$ROOT_CN" ] && echo "   certificate trust settings."
    [ -n "$ROOT_FP" ] && echo "   SHA-256 fingerprint (check it on the device before trusting):"
    [ -n "$ROOT_FP" ] && echo "     ${ROOT_FP}"
  fi
else
  # decision-install-reachability.md F5 (F5 finding #7): report the certificate OUTCOME,
  # not the choice — ACME on an internet-unreachable host retries forever while the
  # summary used to claim success. Probe the real handshake (loopback-resolved, so DNS
  # lag can't false-negative); public issuance can take ~a minute, so poll briefly.
  echo ""
  CERT_OK=0
  i=0
  while [ "$i" -lt 12 ]; do
    if curl -s -o /dev/null --resolve "${NAME}:443:127.0.0.1" "https://${NAME}/" 2>/dev/null; then
      CERT_OK=1; break
    fi
    i=$((i+1)); sleep 5
  done
  if [ "$CERT_OK" -eq 1 ]; then
    echo "   TLS: public certificate ISSUED and serving — nothing to install on clients."
  else
    echo "   ⚠ TLS: the public certificate has NOT been issued (yet). If this server is"
    echo "   not reachable from the internet on ports 80/443, automatic certificates"
    echo "   CANNOT work — re-run talus-setup and press Enter at the certificate"
    echo "   question to use the built-in certificate authority instead."
  fi
fi
if [ "$MODE" = "hostname" ]; then
  echo ""
  echo "   Changing the portal name signs everyone out (the login authority moved);"
  echo "   users simply sign in again — no data is affected."
elif [ "$APPLIANCE" -eq 1 ] && [ "$(hostname 2>/dev/null)" = "talus" ]; then
  echo ""
  echo "   Running more than one Talus appliance? Give this one a unique machine"
  echo "   name so router UIs can tell them apart:  sudo hostnamectl set-hostname <name>"
fi
echo ""
echo "   Safe to re-run anytime:  talus-setup"
echo "  ====================================================================="
}

if [ "$FIRST_RUN" -eq 0 ]; then
  print_guidance
  exit 0
fi

# First run: the same guidance is kept as a file (addresses and the sign-in name — never the
# password), then Talus+ if its bundle was found and accepted.
SUMMARY=./talus-summary.txt
( umask 077; : > "$SUMMARY" ) || say "note: could not write $SUMMARY (non-fatal)"
chmod 600 "$SUMMARY" 2>/dev/null || true
{
  echo "Talus setup summary — $(date '+%Y-%m-%d %H:%M')"
  print_guidance
} | tee -a "$SUMMARY"

if [ -n "$FR_BUNDLE" ]; then
  PLUS_BUNDLE="$FR_BUNDLE"; LICENCE_FILE="$FR_LICENCE"
  ( apply_plus ) 2>&1 | tee -a "$SUMMARY"
  if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    echo ""
    echo "  Talus itself is installed and running; Talus+ was NOT applied (see above)."
    echo "  Once fixed:  scripts/talus-setup.sh --plus $FR_BUNDLE${FR_LICENCE:+ --licence $FR_LICENCE}"
  fi
fi
echo ""
echo "  This summary is saved in $(pwd)/talus-summary.txt"
echo "  If something looks wrong later:  scripts/talus-doctor.sh   (read-only; one line per check)"
exit 0
