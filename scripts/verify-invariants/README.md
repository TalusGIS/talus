# Security invariant suite

Standing negative tests for the product's security invariants.
**Security tests prove the sad path fails** — for every "user X *can* do Y", assert "user Z
*cannot* do Y". Run on every build; a red *required* invariant is a release blocker, same class
as data loss.

## Run

    scripts/verify-invariants/run.sh

Requires the full stack up (`docker compose ps` healthy). The suite writes: it creates and
removes its own throwaway users and items.

Override targets via env: `API_BASE` (default `http://localhost:8000/api/v1`),
`PRIVATE_SLOT` (a `tal_<8hex>` layer the anon caller must NOT see).

## Exit codes & known gaps

- A test in `KNOWN_GAPS` (see `run.sh`) may fail without failing the build — the runner prints it
  loudly as a KNOWN GAP. This is how a *known-broken* invariant lives in the suite as a forcing
  function instead of being silently absent. When the fix lands, move the id out of `KNOWN_GAPS`
  and its RED becomes a real blocker.
- Any other failing test → overall exit 1.

There are no known gaps at this release.

## Coverage

Each `inv*.sh` script proves one invariant: a door that must refuse an anonymous caller, the wrong user,
or a forged identity. The comment at the top of each script says which, and what the script creates and
removes. `run.sh` runs them all and ends with a summary of what passed, what was skipped and why.
