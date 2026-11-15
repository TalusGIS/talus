#!/usr/bin/env bash
# Invariant C6 — the app's runtime DB role CANNOT disable, delete, or alter audit records.
#
# The threat (T6): if the API connects as an owner/superuser, the audit trigger is
# advisory — the app role could DISABLE TRIGGER, DELETE FROM audit.*, or set
# session_replication_role=replica to write assets with audit bypassed. The audit
# trail is only a control when the runtime role is *structurally unable* to do those.
#
# This test does NOT attempt any destructive DDL/DML. It reads the pg catalog to
# determine CAPABILITY (all read-only SELECTs):
#   - is the runtime role a superuser?               (superuser can bypass everything)
#   - does it hold DELETE on an audit table?         (can erase history)
#   - are audit triggers ENABLE ALWAYS ('A')?        ('O' = bypassable via replica role)
#   - can it UPDATE, DELETE or TRUNCATE the governance log (platform.admin_audit, V074/V117)?
#     and, as the control, can it still INSERT there?  (it must: the app writes that log)
#
# PASS requires: NOT superuser AND no DELETE on audit.* AND triggers ALWAYS AND the governance
# log append-only for this role (INSERT yes; UPDATE, DELETE, TRUNCATE no).
# Checks the API's MAIN runtime role (talus_app after S-1a). Pre-S-1a this role doesn't exist
# and the app ran as the `talus` superuser — override DB_ROLE=talus to see the original gap.
# S-1b will additionally remove owner creds from the app process (RCE-proof); until then the
# owner pool still holds tamper capability, which is an accepted, documented residual.
#
# bash 3.2-safe. Canonical read-only psql pattern; stdin fed /dev/null so a compose exec
# never steals it.
set -u

DB_ROLE="${DB_ROLE:-talus_app}"
PSQL="docker compose exec -T postgres psql -U talus -d talusgis -tA"

q() { $PSQL -c "$1" < /dev/null 2>&1; }

fail=0
echo "inv06: runtime DB role = $DB_ROLE"

# 1) superuser?
super=$(q "SELECT rolsuper FROM pg_roles WHERE rolname='$DB_ROLE';")
echo "  [1] rolsuper                    -> $super"
if [ "$super" = "t" ]; then
  echo "      GAP: runtime role is a superuser — can bypass every audit control"
  fail=1
elif [ "$super" != "f" ]; then
  echo "      FAIL: could not read rolsuper (stack down?) -> '$super'"; fail=1
fi

# 2) DELETE on any audit table?
candelete=$(q "SELECT bool_or(has_table_privilege('$DB_ROLE','audit.'||c.relname,'DELETE'))
               FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='audit' AND c.relkind='r';")
echo "  [2] can DELETE on audit.*       -> $candelete"
if [ "$candelete" = "t" ]; then
  echo "      GAP: runtime role can DELETE audit rows — history is erasable"
  fail=1
fi

# 3) any audit trigger NOT ENABLE ALWAYS ('A')?  'O' = bypassable via session_replication_role
bypassable=$(q "SELECT count(*) FROM pg_trigger t
                JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE NOT t.tgisinternal AND t.tgname LIKE '%audit%' AND t.tgenabled <> 'A';")
echo "  [3] audit triggers not ALWAYS   -> $bypassable"
if [ "$bypassable" != "0" ]; then
  echo "      GAP: $bypassable audit trigger(s) are mode 'O' — bypassable via replica role"
  fail=1
fi

# 4) the governance log: append-only for the runtime role. It lives in the platform schema, where
#    the role has full DML by default, so this is a separate grant (V117) and a separate check.
for priv in UPDATE DELETE TRUNCATE; do
  can=$(q "SELECT has_table_privilege('$DB_ROLE','platform.admin_audit','$priv');")
  echo "  [4] can $priv platform.admin_audit -> $can"
  if [ "$can" = "t" ]; then
    echo "      GAP: runtime role can $priv the governance log — its entries can be changed or erased"
    fail=1
  elif [ "$can" != "f" ]; then
    echo "      FAIL: could not read the privilege (stack down? table missing?) -> '$can'"; fail=1
  fi
done
caninsert=$(q "SELECT has_table_privilege('$DB_ROLE','platform.admin_audit','INSERT');")
echo "  [4] can INSERT platform.admin_audit -> $caninsert   (control: must be t)"
if [ "$caninsert" != "t" ]; then
  echo "      FAIL: runtime role cannot INSERT into the governance log — governance actions would go unrecorded"
  fail=1
fi

if [ "$fail" -eq 0 ]; then echo "inv06: PASS"; else echo "inv06: FAIL"; fi
exit "$fail"
