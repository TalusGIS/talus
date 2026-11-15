#!/usr/bin/env bash
# restore-drill.sh — Talus 44 clean-host restore DRILL: prove a backup rebuilds a *working* deployment from
# zero, in a fresh ISOLATED stack that shares NOTHING with the live one. Closes the gap between "the artifacts
# are complete & loadable into scratch DBs" (verify-t42-fullsystem-roundtrip.sh) and "we've rehearsed TOTAL
# recovery": it stands up the REAL compose under a separate project, restores all 3 DBs + media + .env, and
# asserts data + identity + authz + a media object + an end-to-end portal login all come back — then tears the
# drill stack and its volumes down, leaving the host clean. Decision: docs/decision-restore-drill.md.
#
# ISOLATION (the whole point — docs/decision-restore-drill.md §2): a hardcoded drill project
# (talus-restoredrill) + the docker-compose.drill.yml overlay => project-scoped volumes (talus-restoredrill_*),
# auto-named containers (can't collide with live talusgis-*), and NEVER the live ports (publishes only
# 18000/18080). Every `down -v` is project-guarded; the live project is NEVER brought down. The drill's only
# contact with live is READ-ONLY (download artifacts, read the media archive) plus two ADDITIVE acts: one
# `POST /admin/backups/run` (fresh mode; the same non-destructive action verify-t40/t42 take) and one
# throwaway verification principal (tverify-admin-*, random non-formula password, created BEFORE the backup
# so the restored realm provably contains it, deleted from live by teardown with deletion VERIFIED —
# decision-clean-box-verification.md §6, amending decision-restore-drill.md §2 item 6).
#
# Usage:
#   ./scripts/restore-drill.sh                      # take a fresh backup, drill it, assert, teardown
#   ./scripts/restore-drill.sh --backup-id 42       # drill an existing backup set instead of a fresh one
#   ./scripts/restore-drill.sh --age-key key.txt    # decrypt age-encrypted artifacts with this private key
#   ./scripts/restore-drill.sh --env-file /safe/.env   # supply the operator-preserved .env (default ./.env)
#   ./scripts/restore-drill.sh --env-from-backup --age-key key.txt   # use the backup's .env (T45; gated)
#   ./scripts/restore-drill.sh --keep               # leave the drill stack up for post-mortem (no teardown)
#
# REQUIRES: the live stack up; postgresql-client in the postgres images (it is); host `age` ONLY if the backup
# is encrypted. bash 3.2-safe (no declare -A / mapfile / ${var,,}); jq-free (python3). Explicit curl/psql bodies.
set -uo pipefail
cd "$(dirname "$0")/.."

# ---- constants (the drill identity is HARDCODED — never derived from anything live) --------------
DRILL_PROJECT="talus-restoredrill"          # MUST differ from the live project
DRILL_API="http://localhost:18000"          # published only by docker-compose.drill.yml (never the live 8000)
DRILL_KC="http://localhost:18080"           # published only by docker-compose.drill.yml (never the live 8080)
# The media archive is read THROUGH THE LIVE API, from the backup destination in effect — the one a
# backup run and the Download door resolve (Admin → Backups' setting, else BACKUP_S3_* in .env, else
# the local default). No S3 client image: the destination's secret is used inside the api container
# only, and the drill takes no endpoint, bucket or key of its own (decision-restore-drill-offhost.md).
# MEDIA_STREAM_PY writes every object under media/ as a tar stream on stdout (nothing is written
# inside the container); one line on stderr reports what it read.
MEDIA_STREAM_PY='import asyncio, io, os, sys, tarfile
_out = os.fdopen(os.dup(1), "wb")      # the tar stream is the only thing on stdout
os.dup2(2, 1); sys.stdout = sys.stderr # anything else the app prints goes to stderr
import logging
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
from app import db
from app.backup import resolve_backup_store
from app.backup_settings import get_effective_backup_destination

class _Chunks(io.RawIOBase):
    def __init__(self, it):
        self._it, self._buf = it, b""
    def readable(self):
        return True
    def readinto(self, b):
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf)); b[:n] = self._buf[:n]; self._buf = self._buf[n:]
        return n

async def _resolve():
    await db.open_pool()
    try:
        async with db.get_pool().connection() as conn:
            label = (await get_effective_backup_destination(conn)).label
            store = await resolve_backup_store(conn)
    finally:
        await db.close_pool()
    return label, store

label, store = asyncio.run(_resolve())
n = size = refused = 0
with tarfile.open(fileobj=_out, mode="w|") as tar:
    for o in store.list("media/"):
        rel = o.key[len("media/"):]
        if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in rel.split("/")):
            refused += 1   # never written: a key that would land outside the scratch folder
            continue
        info = tarfile.TarInfo(rel); info.size = o.size; info.mode = 0o644
        info.mtime = int(o.last_modified.timestamp()) if o.last_modified else 0
        tar.addfile(info, io.BufferedReader(_Chunks(store.iter_chunks(o.key))))
        n += 1; size += o.size
_out.flush()
print("MEDIA_SOURCE\t%s\t%d\t%d\t%d" % (label, n, size, refused), file=sys.stderr)'
# MEDIA_COUNT_PY prints "<store> <objects>" for every store a backup copies (the api's own list and
# lister, exactly as a backup run walks them); with an ISO time as argv[1], only objects not newer.
MEDIA_COUNT_PY='import datetime, os, sys
_out = os.fdopen(os.dup(1), "w"); os.dup2(2, 1); sys.stdout = sys.stderr
from app.config import settings
from app.backup import _list_source_objects
cut = datetime.datetime.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else None
for b in settings.backup_object_buckets_list:
    objs = _list_source_objects(b)
    _out.write("%s %d\n" % (b, sum(1 for o in objs if cut is None or o.last_modified is None or o.last_modified <= cut)))
_out.flush()'

DC(){ docker compose -f docker-compose.yml -f docker-compose.drill.yml -p "$DRILL_PROJECT" "$@"; }
LIVE(){ docker compose "$@"; }   # default project = the live stack

# ---- flags --------------------------------------------------------------------------------------
BACKUP_ID=""; ENV_FILE="./.env"; ENV_FROM_BACKUP=0; AGE_KEY=""; KEEP=0; FRESH=1
while [ $# -gt 0 ]; do
  case "$1" in
    --backup-id) BACKUP_ID="${2:-}"; FRESH=0; shift 2;;
    --env-file) ENV_FILE="${2:-}"; shift 2;;
    --env-from-backup) ENV_FROM_BACKUP=1; shift;;
    --age-key) AGE_KEY="${2:-}"; shift 2;;
    --keep) KEEP=1; shift;;
    -h|--help) sed -n '2,40p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# shellcheck disable=SC1091
. ./.env                                    # live values (mint token to take the backup, read live counts)
REALM="${KEYCLOAK_REALM:-talus}"
CLIENT="${TALUS_API_CLIENT_ID:-talus-api}"
LIVE_API="${API_URL:-http://localhost:8000}"
LIVE_KC="${KEYCLOAK_URL:-http://localhost:8080}"
PGUSER="${POSTGRES_USER:-talus}";       PGDB="${POSTGRES_DB:-talusgis}"
KCUSER="${KEYCLOAK_DB_USER:-keycloak}"; KCDB="${KEYCLOAK_DB_NAME:-keycloak}"
OFUSER="${OPENFGA_DB_USER:-openfga}";   OFDB="${OPENFGA_DB_NAME:-openfga}"

fails=0
pass(){ echo "  PASS: $1"; }
fail(){ echo "  FAIL: $1"; fails=$((fails + 1)); }
say(){ printf '\n== %s ==\n' "$1"; }
die(){ echo "FATAL: $1" >&2; exit 1; }

read_env_var(){ # <file> <VARNAME> — value with surrounding quotes stripped (bash 3.2-safe)
  v="$(grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2-)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; printf '%s' "$v"
}
mint(){ # <kc_base> <client_secret> <username> <password> — explicit password, no formula (D3)
  curl -sS -X POST "$1/realms/$REALM/protocol/openid-connect/token" \
    -d grant_type=password -d client_id="$CLIENT" -d client_secret="$2" \
    -d username="$3" -d "password=$4" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null; }
live_count(){  LIVE exec -T "$1" psql -U "$2" -d "$3" -tAc "$4" 2>/dev/null | tr -d '[:space:]'; }
drill_count(){ DC   exec -T "$1" psql -U "$2" -d "$3" -tAc "$4" 2>/dev/null | tr -d '[:space:]'; }

# ---- the destroy guard + teardown (project-pinned; the live project is NEVER touched) -----------
SCRATCH=""
guard_project(){
  [ "$DRILL_PROJECT" = "talus-restoredrill" ] || die "destroy guard: project is '$DRILL_PROJECT', not the drill project"
  case "$DRILL_PROJECT" in "") die "destroy guard: refusing to operate on the live project";; esac
}
teardown(){
  _td_rc=$?
  # live throwaway principal out FIRST (runs even with --keep; the restored stack keeps its own
  # copy from the backup, torn down with the volumes). Residue is LOUD and fails the drill.
  if type tv_cleanup >/dev/null 2>&1; then
    if ! tv_cleanup; then
      echo "  RESIDUE: throwaway verification principal survived in LIVE Keycloak/platform.users — remove tverify-* manually" >&2
      _td_residue=1
    fi
  fi
  if [ "$KEEP" -eq 1 ]; then
    echo
    echo "--keep set: leaving the drill stack UP for inspection."
    echo "  api:      $DRILL_API   keycloak: $DRILL_KC"
    echo "  tear down: docker compose -f docker-compose.yml -f docker-compose.drill.yml -p $DRILL_PROJECT down -v --remove-orphans"
    [ -n "$SCRATCH" ] && echo "  scratch:  $SCRATCH (remove when done)"
    [ "${_td_residue:-0}" -ne 0 ] && exit 1
    return
  fi
  guard_project
  echo
  echo "== teardown: down -v the drill project (never the live one) =="
  DC down -v --remove-orphans >/dev/null 2>&1 || true
  left="$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep "^${DRILL_PROJECT}_" || true)"
  if [ -n "$left" ]; then echo "  WARNING: drill volumes still present: $left" >&2; else echo "  drill volumes removed (host clean)."; fi
  [ -n "$SCRATCH" ] && rm -rf "$SCRATCH"
  if [ "${_td_residue:-0}" -ne 0 ] && [ "$_td_rc" -eq 0 ]; then exit 1; fi
}
trap teardown EXIT

# ---- 0. preflight & guards ----------------------------------------------------------------------
say "0. Preflight & guards"
guard_project
command -v docker >/dev/null 2>&1 || die "docker not found"
docker inspect talusgis-api >/dev/null 2>&1 || die "live stack not detected (expected running container talusgis-api) — start it first"
echo "  drill project: $DRILL_PROJECT (volumes ${DRILL_PROJECT}_*, ports 18000/18080)"
if [ "$ENV_FROM_BACKUP" -eq 0 ]; then [ -f "$ENV_FILE" ] || die "--env-file '$ENV_FILE' not found"; fi
echo "  clearing any leftover drill stack from a prior run (guarded)…"
DC down -v --remove-orphans >/dev/null 2>&1 || true
pass "guards ok; drill is project-isolated and will never bind the live ports"

LIVE_SECRET="$(read_env_var ./.env TALUS_API_CLIENT_SECRET)"
[ -n "$LIVE_SECRET" ] || die "TALUS_API_CLIENT_SECRET missing from ./.env"
# The drill's admin identity is a THROWAWAY verification principal (tverify-admin-*), minted from
# .env's KC admin credential — no formula passwords, runs on clean boxes (Stage C finding D3;
# decision-clean-box-verification.md §6). Created BEFORE the backup is taken (fresh mode) so the
# restored realm provably contains it; deleted from LIVE by teardown, deletion verified.
TV_NO_TRAP=1   # the drill has its own EXIT trap; teardown calls tv_cleanup itself
# shellcheck disable=SC1091
. scripts/verify-invariants/_principals.sh
tv_mint admin || die "could not provision the throwaway verification admin (KEYCLOAK_ADMIN_* in ./.env)"
ADMIN="$TV_ADMIN_TOKEN"
LIVE_AUTH="Authorization: Bearer $ADMIN"

# ---- 1. pick the backup -------------------------------------------------------------------------
say "1. Pick the backup to drill"
if [ "$FRESH" -eq 1 ]; then
  PREV="$(curl -sS -H "$LIVE_AUTH" "$LIVE_API/admin/backups" | python3 -c 'import sys,json;b=json.load(sys.stdin)["backups"];print(b[0]["id"] if b else 0)' 2>/dev/null)"
  HTTP="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$LIVE_AUTH" "$LIVE_API/admin/backups/run")"
  [ "$HTTP" = 202 ] || die "POST /admin/backups/run -> $HTTP (want 202)"
  echo "  took a fresh additive backup; waiting for the 'ok' row…"
  ID=""; i=0
  while [ "$i" -lt 30 ]; do
    sleep 3
    LINE="$(curl -sS -H "$LIVE_AUTH" "$LIVE_API/admin/backups" | PREV="$PREV" python3 -c 'import os,sys,json
b=json.load(sys.stdin)["backups"]; p=int(os.environ["PREV"]); t=b[0] if b else None
print("%s %s"%(t["id"],t["status"]) if t and t["id"]!=p else "")' 2>/dev/null)"
    if [ -n "$LINE" ]; then ID="$(echo "$LINE" | cut -d' ' -f1)"; ST="$(echo "$LINE" | cut -d' ' -f2)"; break; fi
    i=$((i + 1))
  done
  [ -n "$ID" ] || die "no new backup row appeared within ~90s"
  [ "$ST" = ok ] || die "fresh backup #$ID status=$ST (want ok)"
else
  ID="$BACKUP_ID"
  [ -n "$ID" ] || die "--backup-id requires a value"
fi
# Read the chosen row: which artifacts are present, and is the set encrypted?
ROW="$(curl -sS -H "$LIVE_AUTH" "$LIVE_API/admin/backups" | ID="$ID" python3 -c 'import os,sys,json
b=json.load(sys.stdin)["backups"]; i=int(os.environ["ID"])
r=next((x for x in b if x["id"]==i), None)
print("%s %s %s %s %s"%(r["status"], 1 if r["encrypted"] else 0, r["keycloakSizeBytes"], r["openfgaSizeBytes"], r["createdAt"]) if r else "")' 2>/dev/null)"
[ -n "$ROW" ] || die "backup #$ID not found in the live catalog"
RST="$(echo "$ROW" | cut -d' ' -f1)"; ENC="$(echo "$ROW" | cut -d' ' -f2)"
KCSZ="$(echo "$ROW" | cut -d' ' -f3)"; OFSZ="$(echo "$ROW" | cut -d' ' -f4)"
BK_CREATED="$(echo "$ROW" | cut -d' ' -f5)"   # the set's start: the cutoff for "live objects older than it"
[ "$RST" = ok ] || die "backup #$ID status=$RST (only 'ok' sets are drillable)"
if [ "$ENC" = 1 ]; then
  [ -n "$AGE_KEY" ] || die "backup #$ID is age-encrypted — pass --age-key <private key>"
  [ -f "$AGE_KEY" ] || die "--age-key '$AGE_KEY' not found"
  command -v age >/dev/null 2>&1 || die "backup is encrypted but 'age' is not on PATH (brew install age)"
fi
echo "  drilling backup #$ID (encrypted=$ENC, keycloak=${KCSZ}B, openfga=${OFSZ}B)"

# ---- 2. stage artifacts to an isolated scratch dir ----------------------------------------------
say "2. Stage artifacts (download from live = READ-ONLY; decrypt if needed)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/talus-drill.XXXXXX")"
echo "  scratch dir: $SCRATCH"
maybe_decrypt(){ # <file> — decrypt in place if it carries the age magic header
  if head -c 30 "$1" | grep -q 'age-encryption.org'; then
    age -d -i "$AGE_KEY" -o "$1.plain" "$1" || die "age decrypt failed for $1"
    mv "$1.plain" "$1"
  fi
}
dl(){ # <artifact> <outfile>
  code="$(curl -sS -H "$LIVE_AUTH" -o "$2" -w '%{http_code}' "$LIVE_API/admin/backups/$ID/download?artifact=$1")"
  [ "$code" = 200 ] || { echo "  download $1 -> HTTP $code: $(head -c 300 "$2" 2>/dev/null)"; return 1; }
  [ -s "$2" ] || { echo "  download $1 -> empty"; return 1; }
  maybe_decrypt "$2"; return 0
}
dl db "$SCRATCH/talusgis.dump" || die "could not stage the talusgis dump"
DO_KC=0; DO_OF=0
if [ "${KCSZ:-0}" -gt 0 ]; then dl keycloak "$SCRATCH/keycloak.dump" && DO_KC=1 || die "could not stage the keycloak dump"; else echo "  keycloak: not in this set (skip)"; fi
if [ "${OFSZ:-0}" -gt 0 ]; then dl openfga  "$SCRATCH/openfga.dump"  && DO_OF=1 || die "could not stage the openfga dump";  else echo "  openfga: not in this set (skip)";  fi

# .env source (F-env): operator-preserved copy (default) OR the backup's .env (T45; gated).
if [ "$ENV_FROM_BACKUP" -eq 1 ]; then
  echo "  --env-from-backup: pulling the .env artifact from the backup (T45)…"
  code="$(curl -sS -H "$LIVE_AUTH" -o "$SCRATCH/.env" -w '%{http_code}' "$LIVE_API/admin/backups/$ID/download?artifact=env")"
  [ "$code" = 200 ] || die "this backup has no .env artifact (HTTP $code) — T45 (encryption-gated .env lifeboat) not yet shipped; use --env-file"
  maybe_decrypt "$SCRATCH/.env"
else
  cp "$ENV_FILE" "$SCRATCH/.env" || die "could not copy --env-file '$ENV_FILE'"
fi
DRILL_SECRET="$(read_env_var "$SCRATCH/.env" TALUS_API_CLIENT_SECRET)"
[ -n "$DRILL_SECRET" ] || die "no TALUS_API_CLIENT_SECRET in the restore .env"

# Media: stage the backup destination's media archive -> scratch (READ-ONLY), decrypt if needed.
# One path for every destination: the live api reads media/ from the destination in effect (the same
# resolution as a backup run and the Download door) and streams it out as a tar; the host extracts it
# as the invoking user, so there is no root-owned scratch residue. A failure here is fatal and named:
# a drill that staged nothing because its fetch broke must never look like a set with no media.
echo "  staging the media archive through the live api, from the backup destination in effect (read-only)…"
mkdir -p "$SCRATCH/media"
printf '%s' "$MEDIA_STREAM_PY" | docker exec -i -w /app talusgis-api python3 - 2>"$SCRATCH/.media-src.log" \
  | tar -xf - -C "$SCRATCH/media"
_st=("${PIPESTATUS[@]}")
if [ "${_st[1]}" -ne 0 ] || [ "${_st[2]}" -ne 0 ]; then
  echo "  --- media fetch (api side) ---"; tail -15 "$SCRATCH/.media-src.log" | sed 's/^/  /'
  die "could not stage the media archive from the backup destination (api exit ${_st[1]}, tar exit ${_st[2]})"
fi
MEDIA_SRC="$(grep '^MEDIA_SOURCE' "$SCRATCH/.media-src.log" | tail -1)"
[ -n "$MEDIA_SRC" ] || die "the media fetch reported nothing (see $SCRATCH/.media-src.log)"
DEST_LABEL="$(printf '%s' "$MEDIA_SRC" | cut -f2)"; REFUSED="$(printf '%s' "$MEDIA_SRC" | cut -f5)"
echo "  backup destination: $DEST_LABEL — $(printf '%s' "$MEDIA_SRC" | cut -f3) object(s), $(printf '%s' "$MEDIA_SRC" | cut -f4) bytes"
[ "${REFUSED:-0}" -eq 0 ] || fail "$REFUSED media key(s) at the destination would land outside the scratch folder — refused, not staged"
if [ "$ENC" = 1 ]; then
  find "$SCRATCH/media" -type f -name '*.age' | while IFS= read -r b; do
    age -d -i "$AGE_KEY" -o "${b%.age}" "$b" 2>/dev/null && rm -f "$b"
  done
fi
STAGED_MEDIA="$(find "$SCRATCH/media" -type f 2>/dev/null | wc -l | tr -d '[:space:]')"
echo "  staged $STAGED_MEDIA media object(s), every store"
# The live server's objects in each store a backup copies, not newer than this set (counted by the
# api's own lister): what the restored copy must hold in fresh mode.
LIVE_MEDIA="$(printf '%s' "$MEDIA_COUNT_PY" | docker exec -i -w /app talusgis-api python3 - "$BK_CREATED" 2>"$SCRATCH/.media-live.log")" \
  || { tail -5 "$SCRATCH/.media-live.log" | sed 's/^/  /'; die "could not count the live server's stored objects"; }
echo "  live objects not newer than backup #$ID: $(printf '%s' "$LIVE_MEDIA" | tr '\n' ' ')"

# ---- 3. bring up the drill DATA plane (empty DBs) -----------------------------------------------
say "3. Bring up the drill data plane (isolated, empty)"
# roles-init added A5 2026-07-27 (defect #8): the runtime roles moved from the initdb
# mount to the roles-init one-shot, and a restore into a roleless cluster fails on every
# GRANT — the exact class the faf15fd classifier exists to catch (and did, on its first
# live end-to-end run).
if ! DC --env-file "$SCRATCH/.env" up -d postgres roles-init keycloak-postgres openfga-postgres >"$SCRATCH/.up-data.log" 2>&1; then
  echo "  --- data-plane 'up' output ---"; tail -40 "$SCRATCH/.up-data.log"; die "drill data plane failed to start (see above)"
fi
wait_health(){ # <service> <max>
  i=0; max="${2:-40}"
  while [ "$i" -lt "$max" ]; do
    cid="$(DC ps -q "$1" 2>/dev/null)"
    if [ -n "$cid" ]; then
      hs="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null)"
      case "$hs" in healthy|running) return 0;; esac
    fi
    i=$((i + 1)); sleep 3
  done
  return 1
}
for svc in postgres keycloak-postgres openfga-postgres; do
  wait_health "$svc" 40 || die "drill $svc never became healthy — DC logs $svc"
done
pass "data plane healthy (postgres + keycloak-postgres + openfga-postgres)"

# ---- 4. restore (the runbook's commands, into the drill DBs) ------------------------------------
say "4. Restore the dumps + media into the drill stack"
restore_db(){ # <dumpfile> <service> <dbuser> <dbname> <label>
  DC cp "$1" "$2":/tmp/r.dump >/dev/null 2>&1 || { fail "$5: could not copy dump into $2"; return 1; }
  err="$(DC exec -T "$2" pg_restore --clean --if-exists --no-owner -U "$3" -d "$4" /tmp/r.dump 2>&1 >/dev/null)"; rc=$?
  DC exec -T "$2" rm -f /tmp/r.dump >/dev/null 2>&1
  # pg_restore emits benign NOTICEs on a fresh DB ("... does not exist, skipping" from --clean
  # --if-exists). Anything at ERROR level is NOT benign and must fail the drill.
  #
  # ★ 2026-07-26 — the old filter was `fatal|could not|connection`, which misses the failure this
  # drill most needs to catch. `--no-owner` suppresses ALTER ... OWNER TO, but GRANTs are still
  # restored (no -x), so a bundle restored into a cluster lacking db/init/00-roles.sh produces
  #     pg_restore: error: could not execute query: ERROR:  role "talus_app" does not exist
  # once per GRANT — and pg_restore still **exits 0**, whispering only
  #     pg_restore: warning: errors ignored on restore: N
  # Neither string matched the old pattern, so the drill printed "restored" + PASS while every
  # privilege in the dump had been silently dropped. Check the exit code AND the text.
  # (decision-dr-roles-dump.md §3.)
  restore_err_re='error:|ERROR:|errors ignored on restore|fatal|could not|connection'
  if [ "$rc" -ne 0 ] || echo "$err" | grep -qE "$restore_err_re"; then
    fail "$5: pg_restore failed (exit $rc): $(echo "$err" | grep -E "$restore_err_re" | head -3)"
    return 1
  fi
  pass "$5: restored"
}
restore_db "$SCRATCH/talusgis.dump" postgres "$PGUSER" "$PGDB" "talusgis (data+audit)"
[ "$DO_KC" -eq 1 ] && restore_db "$SCRATCH/keycloak.dump" keycloak-postgres "$KCUSER" "$KCDB" "keycloak (identity)"
[ "$DO_OF" -eq 1 ] && restore_db "$SCRATCH/openfga.dump"  openfga-postgres  "$OFUSER" "$OFDB" "openfga (authz)"

# Media restore happens in step 5, AFTER the drill api is up — its /storage volume and the
# store roots exist then, and a plain `compose cp` puts the files where the seam reads them.

# ---- 5. bring up the drill APP plane (against restored data) ------------------------------------
say "5. Bring up the drill app plane (boot guard validates restored OpenFGA IDs)"
# --build (A5 defect #9): the drill project keeps its OWN api image, which otherwise goes
# stale forever (`up` never rebuilds; this one was months old and predated baked deps).
# A drill must drill TODAY's api, not the api of whenever the drill project last built.
if ! DC --env-file "$SCRATCH/.env" up -d --build openfga-migrate openfga keycloak api >"$SCRATCH/.up-app.log" 2>&1; then
  echo "  --- app-plane 'up' output ---"; tail -40 "$SCRATCH/.up-app.log"; die "drill app plane failed to start (see above)"
fi
wait_health openfga 40 || die "drill openfga never healthy — DC logs openfga"
i=0; kc_up=0
while [ "$i" -lt 60 ]; do curl -sf "$DRILL_KC/realms/$REALM" >/dev/null 2>&1 && { kc_up=1; break; }; i=$((i+1)); sleep 3; done
[ "$kc_up" = 1 ] && pass "restored Keycloak realm reachable on :18080" || die "restored Keycloak realm not reachable on :18080 within ~180s"
i=0; api_up=0
while [ "$i" -lt 40 ]; do curl -sf "$DRILL_API/health" >/dev/null 2>&1 && { api_up=1; break; }; i=$((i+1)); sleep 3; done
if [ "$api_up" = 1 ]; then
  pass "restored api healthy on :18000 (boot guard passed => .env OpenFGA IDs match restored store)"
  if [ "$STAGED_MEDIA" -gt 0 ]; then
    DC cp "$SCRATCH/media/." api:/storage/ >/dev/null 2>&1 \
      && pass "media copied into the drill storage volume" \
      || fail "could not copy staged media into the drill api's /storage"
  else
    echo "  (no media in this backup — media restore is a no-op)"
  fi
else
  # A5: dump the evidence BEFORE the teardown trap destroys it — telling the operator to
  # run "DC logs api" after down -v was an unactionable hint.
  echo "  --- restored api logs (last 25 lines) ---"
  DC logs --tail=25 api 2>&1 | sed 's/^/  /'
  die "restored api not healthy on :18000 (boot guard may have failed — see logs above)"
fi

# ---- 6. assert recovery (verify-don't-assert) ---------------------------------------------------
say "6. Assert recovery: data + identity + authz + media + end-to-end login"
assert_count(){ # <label> <live> <drill>
  if [ "$FRESH" -eq 1 ]; then
    { [ -n "$3" ] && [ "$2" = "$3" ]; } && pass "$1: restored == live ($3)" || fail "$1 mismatch: live=$2 drill=${3:-<none>}"
  else
    { [ -n "$3" ] && [ "$3" -ge 1 ] 2>/dev/null; } && pass "$1: restored = $3 (live now=$2)" || fail "$1 restored=${3:-<none>} (want >=1; live=$2)"
  fi
}
assert_count "platform.layers (talusgis)" "$(live_count postgres "$PGUSER" "$PGDB" 'SELECT count(*) FROM platform.layers')" "$(drill_count postgres "$PGUSER" "$PGDB" 'SELECT count(*) FROM platform.layers')"
[ "$DO_KC" -eq 1 ] && assert_count "user_entity (Keycloak)" "$(live_count keycloak-postgres "$KCUSER" "$KCDB" 'SELECT count(*) FROM user_entity')" "$(drill_count keycloak-postgres "$KCUSER" "$KCDB" 'SELECT count(*) FROM user_entity')"
[ "$DO_OF" -eq 1 ] && assert_count "tuple (OpenFGA)" "$(live_count openfga-postgres "$OFUSER" "$OFDB" 'SELECT count(*) FROM tuple')" "$(drill_count openfga-postgres "$OFUSER" "$OFDB" 'SELECT count(*) FROM tuple')"

# media objects present in the drill storage volume (read where the seam reads)
# Per store a backup copies, counted by the api's own lister in the drill stack (the same code and
# list as the live count). Fresh mode: the restored copy must hold at least every live object not newer
# than the backup just taken — the drill is held to what the server HAS, not to what it happened to
# stage (a drill that read a stale or wrong archive used to pass on whatever it found).
LIVE_TOTAL=0; for n in $(printf '%s\n' "$LIVE_MEDIA" | awk '{print $2}'); do LIVE_TOTAL=$((LIVE_TOTAL + n)); done
if [ "$STAGED_MEDIA" -gt 0 ] || [ "$LIVE_TOTAL" -gt 0 ]; then
  DRILL_COUNTS="$(printf '%s' "$MEDIA_COUNT_PY" | DC exec -T api python3 - 2>/dev/null)"
  DRILL_TOTAL=0; for n in $(printf '%s\n' "$DRILL_COUNTS" | awk '{print $2}'); do DRILL_TOTAL=$((DRILL_TOTAL + n)); done
  if [ "$FRESH" -eq 1 ]; then
    printf '%s\n' "$LIVE_MEDIA" | while read -r b live_n; do
      [ -n "$b" ] || continue
      got="$(printf '%s\n' "$DRILL_COUNTS" | awk -v b="$b" '$1 == b {print $2}')"
      if [ -n "$got" ] && [ "$got" -ge "$live_n" ] 2>/dev/null; then
        echo "  PASS: media store $b: restored $got >= live $live_n (objects not newer than backup #$ID)"
      else
        echo "  FAIL: media store $b: restored ${got:-<none>}, live $live_n — the backup destination's archive is short"
      fi
    done > "$SCRATCH/.media-assert"
    cat "$SCRATCH/.media-assert"
    short="$(grep -c '^  FAIL:' "$SCRATCH/.media-assert" | tr -d '[:space:]')"
    [ "$short" -eq 0 ] 2>/dev/null || fails=$((fails + short))
  else
    { [ "$DRILL_TOTAL" -ge "$STAGED_MEDIA" ]; } 2>/dev/null \
      && pass "media restored & readable in the drill storage volume ($DRILL_TOTAL object(s); live now: $(printf '%s' "$LIVE_MEDIA" | tr '\n' ' '))" \
      || fail "media mismatch: drill=$DRILL_TOTAL staged=$STAGED_MEDIA"
  fi
else
  echo "  SKIP: no stored file on the live server and none in this backup to retrieve"
fi

# end-to-end portal login over the PUBLISHED drill stack (the proof scratch-DB tests can't give).
# Fresh mode only: the throwaway principal was created BEFORE the backup, so the restored realm
# must contain it. --backup-id drills a PRE-EXISTING set that cannot contain it -> loud SKIP.
DTOK=""
if [ "$FRESH" -eq 1 ] && [ "$DO_KC" -eq 1 ]; then
  DTOK="$(mint "$DRILL_KC" "$DRILL_SECRET" "$TV_ADMIN_USER" "$TV_ADMIN_PW")"
  [ -n "$DTOK" ] && pass "minted the throwaway admin ($TV_ADMIN_USER) vs restored Keycloak (:18080)" || fail "could not mint a token vs restored Keycloak"
elif [ "$FRESH" -eq 0 ]; then
  echo "  SKIP: --backup-id mode — a pre-existing backup cannot contain a fresh throwaway principal;"
  echo "        restored-token login not asserted (run a fresh-mode drill for the full claim)"
else
  echo "  SKIP: this backup set has no Keycloak dump — restored-token login not assertable"
fi
if [ -n "$DTOK" ]; then
  code="$(curl -sS -o "$SCRATCH/.body" -w '%{http_code}' -H "Authorization: Bearer $DTOK" "$DRILL_API/admin/backups")"
  [ "$code" = 200 ] && pass "gated GET /admin/backups -> 200 with token (restored api validated the restored token)" \
    || fail "gated GET with token -> $code (want 200): $(head -c 200 "$SCRATCH/.body" 2>/dev/null)"
  code2="$(curl -sS -o "$SCRATCH/.body2" -w '%{http_code}' -H "Authorization: Bearer $DTOK" "$DRILL_API/me/layers")"
  [ "$code2" = 200 ] && pass "GET /me/layers -> 200 (OpenFGA read path works on restored authz)" \
    || fail "GET /me/layers -> $code2: $(head -c 200 "$SCRATCH/.body2" 2>/dev/null)"
fi
codeN="$(curl -sS -o /dev/null -w '%{http_code}' "$DRILL_API/admin/backups")"
case "$codeN" in 401|403) pass "gated GET without token -> $codeN (fail-closed)";; *) fail "no-token gated GET -> $codeN (want 401/403)";; esac

# ---- 7. summary (teardown runs on EXIT) ---------------------------------------------------------
echo
if [ "$fails" -eq 0 ]; then
  # Say what THIS run proved, not what the drill can prove. A set with no sign-in or authorization
  # dump, no media staged, or no token mintable skips those checks above (each prints its own SKIP);
  # the closing line used to claim all five regardless.
  recovered="data"; not_checked=""
  [ "$DO_KC" -eq 1 ] && recovered="$recovered + identity" || not_checked="$not_checked identity"
  [ "$DO_OF" -eq 1 ] && recovered="$recovered + authz" || not_checked="$not_checked authz"
  { [ "${STAGED_MEDIA:-0}" -gt 0 ] || [ "${LIVE_TOTAL:-0}" -gt 0 ]; } && recovered="$recovered + media" || not_checked="$not_checked media"
  [ -n "$DTOK" ] && recovered="$recovered + login" || not_checked="$not_checked login"
  echo "T44 CLEAN-HOST RESTORE DRILL: ALL PASS — a from-zero rebuild recovered $recovered."
  echo "  backup destination drilled: $DEST_LABEL"
  [ -n "$not_checked" ] && echo "  NOT CHECKED in this run (skipped above, each with its reason):$not_checked"
  # Drill marker (decision-system-health.md F1): record the successful drill in the LIVE portal's
  # maintenance_runs so Admin → Health can answer "when was recovery last proven?". Best-effort by
  # design — a completed drill must never fail on bookkeeping (live stack may not even be up).
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-talus}" -d "${POSTGRES_DB:-talusgis}" -c \
    "INSERT INTO platform.maintenance_runs (job_name, started_at, finished_at, status, items_affected, bytes_freed, detail)
     VALUES ('restore_drill', now(), now(), 'ok', 0, 0, 'clean-host drill ALL PASS: $recovered')" \
    >/dev/null 2>&1 && echo "  (drill recorded in Admin → Health)" \
    || echo "  (note: could not record the drill marker in the live DB — live stack down?)"
  exit 0
else
  echo "T44 CLEAN-HOST RESTORE DRILL: $fails FAILURE(S) — see above."
  echo "  backup destination drilled: ${DEST_LABEL:-<not reached>}"
  exit 1
fi
