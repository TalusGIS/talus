# _paid.sh — how a paid-module invariant runs on THIS box (decision-paid-invariants-live.md §4).
# SOURCED, never executed; the underscore keeps it out of run.sh's inv*.sh glob.
#
# Until 2026-10-04 each paid invariant SKIPped as a whole unless the box had the dev entitlement
# mount and `paid.<m>` in ./.env — so on every real Talus+ install (where the modules live in the api
# container's environment and the licence is the customer's own) the Field and AI gates never ran,
# and the suite still said OK. Now one probe decides a MODE, and each script runs every leg it can:
#
#   dev         ENVIRONMENT=development in the api AND ./.dev-entitlement present — every leg,
#               including the ones that swap in throwaway licences signed by a throwaway key (the api
#               honours that key only in development, so this mode cannot fire on a real box).
#   live        the module is installed and licensed here — the legs that need only the box's own
#               licence run; the licence-swapping ones are skipped BY NAME (tv_dev_leg).
#   unlicensed  installed, not licensed — the "the doors stay shut" legs run.
#
#   tv_paid_mode <module> <inv-name>
#       Sets TV_PAID_MODE and prints it. Exits the CALLING script with 3 (SKIP) when the module is not
#       installed (a Community box), and with 1 (FAIL) when the box cannot say what is installed or
#       licensed: a check that can only answer "could not check" must answer no.
#   tv_dev_leg <label> <why>
#       0 in dev mode (run the leg). Otherwise prints `SKIP-LEG <label> — dev rig only: <why>`,
#       counts it, and returns 1.
#   tv_env_leg <label> <why>
#       A leg whose GENUINE environmental prerequisite is missing on this box (the product itself says
#       so — e.g. no basemap archive, so no Field pack can be built): prints `SKIP-LEG <label> —
#       unavailable on this box: <why>` and counts it. Never for a probe that merely failed.
#   tv_field_no_basemap <owner-token> <field-ready-map-id>
#       0 when the product answers the pack-estimate door (which writes nothing) with its own 409
#       "no basemap archive is installed" — the one reason a licensed box cannot build packs.
#   tv_partial_report
#       When legs were skipped (either kind), prints `PARTIAL: …` and appends `<inv>(<n>)` to $TV_PARTIAL_FILE,
#       which run.sh collects into its PARTIAL: summary line. A partial run passes (exit 0) but can
#       never again read as a plain OK.
#
# LIVE-mode fences (by construction, in every caller): no leg writes the licence file, pastes or
# deletes an AI token, or reaches an AI gateway. Requires cwd = the repo/install root, stack up.
# bash 3.2-safe; jq-free.

TV_PAID_MODE=""
TV_PAID_NAME=""
TV_PAID_SKIPPED=0

tv_paid_mode() { # <module> <inv-name>
  TV_PAID_NAME="$2"
  _tpe="$(docker compose exec -T api sh -c 'printf "ENV|%s|%s" "${ENVIRONMENT:-}" "${TALUS_EXTRA_MODULES:-}"' < /dev/null 2>/dev/null)"
  case "$_tpe" in
    ENV\|*) : ;;
    *) echo "$2: FAIL — cannot read the api container's environment (is the stack up?)"; exit 1 ;;
  esac
  _tpe="${_tpe#ENV|}"; _tp_environment="${_tpe%%|*}"; _tp_extra="${_tpe#*|}"
  case ",$_tp_extra," in
    *",paid.$1,"*) : ;;
    *) echo "$2: SKIP — paid.$1 is not installed on this box (the api's TALUS_EXTRA_MODULES: ${_tp_extra:-none})"; exit 3 ;;
  esac
  if [ "$_tp_environment" = "development" ] && [ -d .dev-entitlement ]; then
    TV_PAID_MODE=dev
    echo "$2: mode DEV — every leg runs (the dev entitlement mount swaps throwaway licences)"
    return 0
  fi
  # talus-setup's own probe (the line it prints as LICENSED: …); tail -1 because the api logs its
  # own warnings to stdout. The module gate is signed_and_valid + the module listed (never within_term).
  _tpl="$(docker compose exec -T api python -c '
from app.entitlement_runtime import current_status
s = current_status()
print("LIC " + ("1" if s.signed_and_valid else "0") + " ," + ",".join(sorted(s.modules)) + ",")
' < /dev/null 2>/dev/null | tail -1)"
  case "$_tpl" in
    "LIC 1 "*",$1,"*) TV_PAID_MODE=live
      echo "$2: mode LIVE — $1 is licensed here; legs that swap licences or reach a gateway are skipped by name" ;;
    "LIC "*) TV_PAID_MODE=unlicensed
      echo "$2: mode UNLICENSED — paid.$1 is installed but not licensed here; the closed-door legs run" ;;
    *) echo "$2: FAIL — the entitlement probe gave no verdict (got: '${_tpl:-nothing}')"; exit 1 ;;
  esac
  return 0
}

tv_dev_leg() { # <label> <why>
  [ "$TV_PAID_MODE" = dev ] && return 0
  echo "  SKIP-LEG $1 — dev rig only: $2"
  TV_PAID_SKIPPED=$((TV_PAID_SKIPPED + 1))
  return 1
}

tv_env_leg() { # <label> <why>
  echo "  SKIP-LEG $1 — unavailable on this box: $2"
  TV_PAID_SKIPPED=$((TV_PAID_SKIPPED + 1))
  return 0
}

tv_field_no_basemap() { # <owner-token> <field-ready-map-id>
  _tfb="$(curl -sS -X POST -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d '{"title":"basemap probe","bbox":[-82.6,27.3,-82.4,27.4],"basemapMaxzoom":14,"ttlDays":7}' \
    "${API:-http://localhost:8000/api/v1}/web-maps/$2/field-packs/estimate")"
  case "$_tfb" in *"no basemap archive is installed"*) return 0 ;; *) return 1 ;; esac
}

tv_partial_report() {
  [ "$TV_PAID_SKIPPED" -gt 0 ] || return 0
  echo "PARTIAL: $TV_PAID_NAME — $TV_PAID_SKIPPED leg(s) could not run here and were skipped by name above (mode $TV_PAID_MODE)"
  [ -n "${TV_PARTIAL_FILE:-}" ] && echo "$TV_PAID_NAME($TV_PAID_SKIPPED)" >> "$TV_PARTIAL_FILE"
  return 0
}
