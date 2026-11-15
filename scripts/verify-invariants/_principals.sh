#!/usr/bin/env bash
# _principals.sh — shared throwaway-principal helper for the invariant suite + restore drill
# (decision-clean-box-verification.md §3). SOURCED, never executed; the underscore name keeps it
# out of run.sh's inv*.sh glob.
#
# Mints ephemeral, least-privilege principals from credentials EVERY deployment has — the
# Keycloak admin credential in ./.env (KEYCLOAK_ADMIN_USER / KEYCLOAK_ADMIN_PASSWORD, generated
# at install) — so verification tooling runs identically on dev stacks and clean customer boxes.
# No demo-realm formula passwords, ever (Stage C findings D2/D3).
#
#   tv_mint <shape>      shape: viewer | editor | publisher | admin | norow
#                        Creates KC user tverify-<shape>-<8hex> (random policy-compliant
#                        password, full profile, emailVerified, requiredActions:[] — the
#                        "account not fully set up" trap, pre-solved), maps the realm role
#                        (viewer=talus-viewer, editor=talus-data-editor, publisher=
#                        talus-publisher, admin=talus-admin), inserts the platform.users row
#                        (KC id as id), and mints an API token via the talus-api client.
#                        Exports TV_<SHAPE>_USER / _PW / _ID / _TOKEN.
#                        `norow` = KC user + token ONLY (no role, no platform.users row) —
#                        inv01's users-row-gate fixture.
#   tv_cleanup           Deletes every minted principal and VERIFIES deletion (KC GET -> 404,
#                        platform.users count -> 0) with a FRESH admin token (the old one may
#                        have expired mid-leg — an unverified stale-token trap is a silent
#                        no-op). Any failure prints RESIDUE: lines and sets TV_RESIDUE=1.
#
# Trap contract: the first tv_mint installs `trap tv_on_exit EXIT` unless TV_NO_TRAP=1 is set
# BEFORE the first mint (restore-drill.sh sets it and calls tv_cleanup from its own teardown).
# tv_on_exit runs the leg's optional `leg_cleanup` function first (declare it instead of your
# own EXIT trap), then tv_cleanup, and forces a nonzero exit if residue was left — a leaked
# ephemeral admin must never be silent.
#
# Requirements (decision doc F5): source AFTER cd to the repo root, ./.env present, stack up.
# bash 3.2-safe (no declare -A / mapfile / ${var,,}); jq-free (python3); -sS curl.

TV_REALM="${KEYCLOAK_REALM:-talus}"
TV_CLIENT="${TALUS_API_CLIENT_ID:-talus-api}"
TV_MINTED="${TV_MINTED:-}"
TV_RESIDUE=0

tv_acc() { python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null; }

tv_env() { # <VARNAME> — value from ./.env, surrounding quotes stripped
  v="$(grep -E "^$1=" ./.env 2>/dev/null | head -1 | cut -d= -f2-)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"; printf '%s' "$v"
}

tv_init() { # resolve endpoints/creds once; returns 1 (loudly) if the box lacks what F5 requires
  [ -n "${TV_KC:-}" ] && return 0
  [ -f ./.env ] || { echo "  _principals: FAIL — ./.env not found (run from the repo root on the deployment box)"; return 1; }
  _p="$(tv_env KEYCLOAK_PORT)"; _p="${_p:-8080}"
  TV_KC="${KEYCLOAK_URL:-http://localhost:$_p}"
  TV_ADMIN_CLI_USER="$(tv_env KEYCLOAK_ADMIN_USER)"; TV_ADMIN_CLI_USER="${TV_ADMIN_CLI_USER:-admin}"
  TV_ADMIN_CLI_PW="$(tv_env KEYCLOAK_ADMIN_PASSWORD)"
  TV_SECRET="$(tv_env TALUS_API_CLIENT_SECRET)"
  TV_PGUSER="$(tv_env POSTGRES_USER)"; TV_PGUSER="${TV_PGUSER:-talus}"
  TV_PGDB="$(tv_env POSTGRES_DB)";     TV_PGDB="${TV_PGDB:-talusgis}"
  [ -n "$TV_ADMIN_CLI_PW" ] || { echo "  _principals: FAIL — KEYCLOAK_ADMIN_PASSWORD missing from ./.env"; return 1; }
  [ -n "$TV_SECRET" ] || { echo "  _principals: FAIL — TALUS_API_CLIENT_SECRET missing from ./.env"; return 1; }
  return 0
}

tv_kc_admin_token() { # fresh master-realm admin-cli token (short-lived — never cache across a leg)
  curl -sS -X POST "$TV_KC/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli \
    -d username="$TV_ADMIN_CLI_USER" -d "password=$TV_ADMIN_CLI_PW" | tv_acc
}

tv_role_for() { # shape -> realm role ('' for norow)
  case "$1" in
    viewer) echo talus-viewer ;;
    editor) echo talus-data-editor ;;
    publisher) echo talus-publisher ;;
    admin) echo talus-admin ;;
    norow) echo "" ;;
    *) return 1 ;;
  esac
}

tv_mint() { # <shape> — exports TV_<SHAPE>_USER/_PW/_ID/_TOKEN; registers principal for cleanup
  shape="$1"
  role="$(tv_role_for "$shape")" || { echo "  _principals: FAIL — unknown shape '$shape'"; return 1; }
  tv_init || return 1
  rid_hex="$(openssl rand -hex 4)"
  nu="tverify-$shape-$rid_hex"
  npw="Tv9Aa-$(openssl rand -hex 16)!"   # upper+lower+digit+special, len 24+ — policy-compliant

  at="$(tv_kc_admin_token)"
  [ -n "$at" ] || { echo "  _principals: FAIL — could not get a KC admin token (KEYCLOAK_ADMIN_* in ./.env; is Keycloak up at $TV_KC?)"; return 1; }

  cc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$TV_KC/admin/realms/$TV_REALM/users" \
    -H "Authorization: Bearer $at" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$nu\",\"enabled\":true,\"emailVerified\":true,\"firstName\":\"Talus Verify\",\"lastName\":\"Throwaway\",\"email\":\"$nu@talus.invalid\",\"requiredActions\":[],\"credentials\":[{\"type\":\"password\",\"value\":\"$npw\",\"temporary\":false}]}")
  [ "$cc" = "201" ] || { echo "  _principals: FAIL — KC create $nu -> $cc (expect 201)"; return 1; }

  kid=$(curl -sS "$TV_KC/admin/realms/$TV_REALM/users?username=$nu&exact=true" -H "Authorization: Bearer $at" \
    | python3 -c "import sys,json;u=json.load(sys.stdin);print(u[0]['id'] if u else '')" 2>/dev/null)
  [ -n "$kid" ] || { echo "  _principals: FAIL — created $nu but could not read its id back"; return 1; }
  TV_MINTED="$TV_MINTED $shape:$nu:$kid"
  if [ -z "${TV_NO_TRAP:-}" ] && [ -z "${TV_TRAP_SET:-}" ]; then TV_TRAP_SET=1; trap tv_on_exit EXIT; fi

  if [ "$shape" != "norow" ]; then
    rjson=$(curl -sS "$TV_KC/admin/realms/$TV_REALM/roles/$role" -H "Authorization: Bearer $at")
    rrid=$(printf '%s' "$rjson" | python3 -c "import sys,json;print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
    [ -n "$rrid" ] || { echo "  _principals: FAIL — realm role $role not found (clean install should have created it)"; return 1; }
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$TV_KC/admin/realms/$TV_REALM/users/$kid/role-mappings/realm" \
      -H "Authorization: Bearer $at" -H 'Content-Type: application/json' \
      -d "[{\"id\":\"$rrid\",\"name\":\"$role\"}]")
    case "$rc" in 2*) : ;; *) echo "  _principals: FAIL — map $role onto $nu -> $rc"; return 1 ;; esac
    docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$TV_PGUSER" -d "$TV_PGDB" -c \
      "INSERT INTO platform.users (id, username, email, display_name)
       VALUES ('$kid','$nu','$nu@talus.invalid','Talus Verify Throwaway')" >/dev/null < /dev/null \
      || { echo "  _principals: FAIL — platform.users insert for $nu"; return 1; }
  fi

  tok=$(curl -sS -X POST "$TV_KC/realms/$TV_REALM/protocol/openid-connect/token" \
    -d grant_type=password -d client_id="$TV_CLIENT" -d "client_secret=$TV_SECRET" \
    -d username="$nu" -d "password=$npw" | tv_acc)
  [ -n "$tok" ] || { echo "  _principals: FAIL — could not mint $nu's token via $TV_CLIENT"; return 1; }

  up=$(printf '%s' "$shape" | tr '[:lower:]' '[:upper:]')
  eval "TV_${up}_USER=\"\$nu\"; TV_${up}_PW=\"\$npw\"; TV_${up}_ID=\"\$kid\"; TV_${up}_TOKEN=\"\$tok\""
  eval "export TV_${up}_USER TV_${up}_PW TV_${up}_ID TV_${up}_TOKEN"
  echo "  minted throwaway $shape principal $nu"
  return 0
}

tv_cleanup() { # delete + VERIFY every minted principal; loud RESIDUE on any survivor
  [ -n "$TV_MINTED" ] || return 0
  at="$(tv_kc_admin_token)"   # always fresh — the mint-time token may have expired
  if [ -z "$at" ]; then
    echo "  RESIDUE: could not re-mint a KC admin token to tear down:$TV_MINTED" >&2
    TV_RESIDUE=1; return 1
  fi
  for ent in $TV_MINTED; do
    shape="${ent%%:*}"; rest="${ent#*:}"; nu="${rest%%:*}"; kid="${rest#*:}"
    curl -sS -o /dev/null -X DELETE "$TV_KC/admin/realms/$TV_REALM/users/$kid" -H "Authorization: Bearer $at"
    gc=$(curl -sS -o /dev/null -w '%{http_code}' "$TV_KC/admin/realms/$TV_REALM/users/$kid" -H "Authorization: Bearer $at")
    [ "$gc" = "404" ] || { echo "  RESIDUE: KC user $nu ($kid) still present (GET -> $gc)" >&2; TV_RESIDUE=1; }
    if [ "$shape" != "norow" ]; then
      # Hard-delete first: principals with no audited writes vanish entirely. An actor the audit
      # trail references CANNOT be hard-deleted (FK by design — audit actors stay resolvable
      # forever), so fall back to the product's own REMOVE semantics (users.py delete_user, V091):
      # removed_at = now(), which takes the row OFF THE ROSTER while keeping its references
      # resolvable. Residue = a KC user surviving, or a row still on the roster.
      #
      # ★ This used to set disabled_at only, which left every audit-referenced throwaway on the
      # Users page forever. Measured 2026-08-25: 133 of 147 roster rows were tverify-* leftovers
      # from past suite runs — the suite had been slowly polluting the roster for months. Removal
      # (decision-user-lifecycle.md F4) is exactly the semantic this cleanup always wanted.
      if ! docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$TV_PGUSER" -d "$TV_PGDB" -c \
          "DELETE FROM platform.users WHERE id='$kid' AND username='$nu'" >/dev/null 2>&1 < /dev/null; then
        docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$TV_PGUSER" -d "$TV_PGDB" -c \
          "UPDATE platform.users SET disabled_at = COALESCE(disabled_at, now()), removed_at = now()
           WHERE id='$kid' AND username='$nu'" >/dev/null < /dev/null
        echo "  note: $nu is audit-referenced — row removed from the roster, references kept resolvable"
      fi
      rows=$(docker compose exec -T postgres psql -U "$TV_PGUSER" -d "$TV_PGDB" -tA < /dev/null \
        -c "SELECT count(*) FROM platform.users WHERE username='$nu' AND removed_at IS NULL;" | tr -d '[:space:]')
      [ "$rows" = "0" ] || { echo "  RESIDUE: platform.users row for $nu still ON THE ROSTER ($rows)" >&2; TV_RESIDUE=1; }
    fi
  done
  TV_MINTED=""
  [ "$TV_RESIDUE" -eq 0 ] || return 1
  return 0
}

tv_on_exit() { # preserve the leg's exit code; force FAIL on residue
  _tv_rc=$?
  if type leg_cleanup >/dev/null 2>&1; then leg_cleanup || true; fi
  tv_cleanup || true
  if [ "$TV_RESIDUE" -ne 0 ] && [ "$_tv_rc" -eq 0 ]; then exit 1; fi
  exit "$_tv_rc"
}
