"""Keycloak Admin API seam (Talus 19 c4) — server-side provisioning of service-account clients.

The portal authenticates AS the confidential `talus-provisioner` client (client-credentials)
and calls the Keycloak Admin API to create / rotate / disable per-integration service-account
clients and assign their realm roles. This is the exact set of Admin-API calls that
scripts/bootstrap-19-service-accounts.sh + grant-19-demo-sa.sh make by hand, ported into the
backend so the c4 admin UI can drive them. It is the reusable seam the future user-admin slice
(Talus 20) builds on.

This module ONLY touches Keycloak. The platform.users principal row, the OpenFGA group
membership tuples, and the platform.groups registry are written by the router
(routers/service_accounts.py), keeping Keycloak / Postgres / OpenFGA writes separable and
each step's ordering explicit (mirrors publisher.py's discipline).

Fail-closed, same posture as authz.py: a missing provisioner credential or any Admin-API
error raises (503 unconfigured / 502 upstream) — never a silent partial success the caller
might mistake for a provisioned account.
"""
from typing import Any

import httpx
import structlog
from fastapi import HTTPException, status

from .config import settings

log = structlog.get_logger(__name__)

_KC_ADMIN_TIMEOUT = httpx.Timeout(10.0)


def _token_url() -> str:
    return (
        f"{settings.keycloak_admin_base_url}/realms/{settings.keycloak_realm}"
        f"/protocol/openid-connect/token"
    )


def _admin_base() -> str:
    return f"{settings.keycloak_admin_base_url}/admin/realms/{settings.keycloak_realm}"


def _unconfigured() -> HTTPException:
    log.error("provisioner_unconfigured")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="service-account provisioning is not configured",
    )


def _upstream(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


async def _admin_token(client: httpx.AsyncClient) -> str:
    """Mint a client-credentials token for the provisioner. 503 if no secret is configured;
    502 on any token-endpoint failure. Minted per operation — provisioning is admin-triggered
    and infrequent, so no token cache is worth the staleness risk."""
    if not settings.keycloak_provisioner_client_secret:
        raise _unconfigured()
    try:
        resp = await client.post(
            _token_url(),
            data={
                "grant_type": "client_credentials",
                "client_id": settings.keycloak_provisioner_client_id,
                "client_secret": settings.keycloak_provisioner_client_secret,
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("provisioner_token_failed", error=exc.__class__.__name__, detail=str(exc))
        raise _upstream("could not authenticate to the identity provider") from exc
    tok = resp.json().get("access_token")
    if not tok:
        raise _upstream("identity provider returned no token")
    return tok


async def _kc(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    path: str,
    *,
    json: Any | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """One authenticated Admin-API call against `_admin_base()/path`. Raises 502 on transport
    error or non-2xx (the upstream status + a clipped body are logged). Callers that need to
    branch on a specific status (e.g. 409 duplicate) catch httpx.HTTPStatusError themselves by
    passing through _kc_raw instead."""
    resp = await _kc_raw(client, token, method, path, json=json, params=params)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.warning(
            "kc_admin_rejected",
            method=method,
            path=path,
            upstream_status=exc.response.status_code,
            detail=exc.response.text[:500],
        )
        raise _upstream("identity provider admin call failed") from exc
    return resp


async def _kc_raw(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    path: str,
    *,
    json: Any | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Like _kc but does NOT raise on non-2xx — only on transport error. Lets a caller inspect
    the status (create maps 409 -> 409). Transport failures still fail closed (502)."""
    url = f"{_admin_base()}{path}"
    try:
        return await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=json,
            params=params,
        )
    except httpx.HTTPError as exc:
        log.warning(
            "kc_admin_unreachable",
            method=method,
            path=path,
            error=exc.__class__.__name__,
            detail=str(exc),
        )
        raise _upstream("identity provider unreachable") from exc


# --- small reads ----------------------------------------------------------------


async def _client_uuid(client: httpx.AsyncClient, token: str, client_id: str) -> str | None:
    resp = await _kc(client, token, "GET", "/clients", params={"clientId": client_id})
    arr = resp.json()
    return arr[0]["id"] if arr else None


async def _scope_id(client: httpx.AsyncClient, token: str, name: str) -> str | None:
    resp = await _kc(client, token, "GET", "/client-scopes")
    for s in resp.json():
        if s.get("name") == name:
            return s["id"]
    return None


async def _service_account_sub(client: httpx.AsyncClient, token: str, client_uuid: str) -> str:
    resp = await _kc(client, token, "GET", f"/clients/{client_uuid}/service-account-user")
    sub = resp.json().get("id")
    if not sub:
        raise _upstream("could not read service-account user id")
    return sub


async def _read_secret(client: httpx.AsyncClient, token: str, client_uuid: str) -> str:
    resp = await _kc(client, token, "GET", f"/clients/{client_uuid}/client-secret")
    secret = resp.json().get("value")
    if not secret:
        raise _upstream("could not read client secret")
    return secret


def _id_from_location(resp: httpx.Response) -> str | None:
    """The new user's id from the 201 Location header (POST /users returns no body)."""
    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    return loc.rstrip("/").rsplit("/", 1)[-1] or None


async def _user_id_by_username(client: httpx.AsyncClient, token: str, username: str) -> str | None:
    """Fallback lookup (exact match) when the Location header is missing."""
    resp = await _kc(client, token, "GET", "/users", params={"username": username, "exact": "true"})
    arr = resp.json()
    return arr[0]["id"] if arr else None


# --- public operations ----------------------------------------------------------


async def create_sa_client(client_id: str, name: str, description: str) -> dict[str, str]:
    """Create a confidential, client-credentials-only service-account client, attach the shared
    audience scope (so its token carries aud=talus-api), and return its identifiers + the
    one-time secret: {client_uuid, sa_sub, secret}.

    409 (HTTPException) if the clientId already exists — the caller derives a unique id, but a
    race or a leftover client surfaces honestly rather than silently reusing someone else's
    credential. The realm defaults (the `roles` scope etc.) are LEFT in place — only ADDING the
    audience scope — so the SA token still carries realm_access.roles (the edit-role key)."""
    rep = {
        "clientId": client_id,
        "name": name,
        "description": description,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": True,
        "redirectUris": [],
        "webOrigins": [],
        "attributes": {"access.token.lifespan": "300"},
    }
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        created = await _kc_raw(client, token, "POST", "/clients", json=rep)
        if created.status_code == status.HTTP_409_CONFLICT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a service account named {client_id!r} already exists",
            )
        try:
            created.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "kc_sa_create_rejected",
                client_id=client_id,
                upstream_status=exc.response.status_code,
                detail=exc.response.text[:500],
            )
            raise _upstream("could not create the service account") from exc

        client_uuid = await _client_uuid(client, token, client_id)
        if not client_uuid:
            raise _upstream("created the service account but could not read it back")

        # Attach the shared audience scope as a DEFAULT scope (adds to, never replaces, the
        # realm defaults). Without aud=talus-api the SA token fails auth.py's audience check.
        scope_id = await _scope_id(client, token, settings.keycloak_sa_audience_scope)
        if not scope_id:
            raise _upstream(
                f"audience scope {settings.keycloak_sa_audience_scope!r} is missing "
                "(run scripts/bootstrap-19c-provisioner.sh)"
            )
        await _kc(
            client, token, "PUT", f"/clients/{client_uuid}/default-client-scopes/{scope_id}"
        )

        sa_sub = await _service_account_sub(client, token, client_uuid)
        secret = await _read_secret(client, token, client_uuid)
        log.info("kc_sa_created", client_id=client_id, client_uuid=client_uuid, sa_sub=sa_sub)
        return {"client_uuid": client_uuid, "sa_sub": sa_sub, "secret": secret}


async def assign_realm_role(sa_sub: str, role_name: str) -> None:
    """Assign a realm role to a service-account user (read-write SAs get talus-data-editor).
    Idempotent — re-assigning an already-mapped role is a Keycloak no-op."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        role = (await _kc(client, token, "GET", f"/roles/{role_name}")).json()
        await _kc(
            client,
            token,
            "POST",
            f"/users/{sa_sub}/role-mappings/realm",
            json=[{"id": role["id"], "name": role["name"]}],
        )
        log.info("kc_sa_role_assigned", sa_sub=sa_sub, role=role_name)


async def regenerate_secret(client_id: str) -> str:
    """Rotate a service account's client secret and return the NEW value (shown once). 404 if
    the client is gone. The previous secret is invalidated immediately by Keycloak."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        client_uuid = await _client_uuid(client, token, client_id)
        if not client_uuid:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service account not found")
        resp = await _kc(client, token, "POST", f"/clients/{client_uuid}/client-secret")
        secret = resp.json().get("value")
        if not secret:
            raise _upstream("rotated the secret but could not read the new value")
        log.info("kc_sa_secret_rotated", client_id=client_id)
        return secret


async def set_enabled(client_id: str, enabled: bool) -> None:
    """Enable/disable a service-account client. Disabling stops new token issuance (the ~5-min
    access token expires fast); it is the primary revocation (decision-19 Q8). 404 if gone."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        client_uuid = await _client_uuid(client, token, client_id)
        if not client_uuid:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service account not found")
        rep = (await _kc(client, token, "GET", f"/clients/{client_uuid}")).json()
        rep["enabled"] = enabled
        await _kc(client, token, "PUT", f"/clients/{client_uuid}", json=rep)
        log.info("kc_sa_set_enabled", client_id=client_id, enabled=enabled)


async def enabled_map(client_ids: list[str]) -> dict[str, bool]:
    """Map each given clientId -> its `enabled` flag, for the list view. One Admin-API call
    (GET /clients) indexed by clientId; ids not present resolve absent (caller treats as
    unknown). Resilient: on failure returns {} so the list still renders from Postgres."""
    if not client_ids:
        return {}
    wanted = set(client_ids)
    try:
        async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
            token = await _admin_token(client)
            resp = await _kc(client, token, "GET", "/clients")
        return {
            c["clientId"]: bool(c.get("enabled", False))
            for c in resp.json()
            if c.get("clientId") in wanted
        }
    except HTTPException:
        log.warning("kc_enabled_map_unavailable")
        return {}


async def subs_with_realm_role(role_name: str, *, strict: bool = False) -> set[str]:
    """The set of user ids that hold `role_name`. One Admin-API call (GET /roles/{name}/users).

    `strict=False` (the list-view default): resilient — {} on failure, so the worst case is a
    degraded display, never a failed list. `strict=True` (the last-admin lockout guard): re-raises
    on failure so the guard can DENY rather than risk an unverifiable count (fail-safe, F7)."""
    try:
        async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
            token = await _admin_token(client)
            resp = await _kc(client, token, "GET", f"/roles/{role_name}/users")
        return {u["id"] for u in resp.json() if u.get("id")}
    except HTTPException:
        log.warning("kc_role_users_unavailable", role=role_name)
        if strict:
            raise
        return set()


async def set_user_enabled(sub: str, enabled: bool) -> bool:
    """Enable/disable a human user. `enabled=false` is the PRIMARY offboarding control — no new
    tokens issue and the short-lived (~5-min) access token expires fast; attribution/audit are
    preserved (no hard-delete).

    Returns True if a Keycloak identity was updated, **False if this principal has no identity at
    all** (decision-user-lifecycle.md F2). That second case is not an error: bulk import
    synthesises `platform.users` rows so imported features have a valid `created_by`, and those
    principals can never sign in. This used to raise 404 — which made Disable impossible for them
    AND reported "user not found" about a row the admin was looking at, because the caller writes
    `disabled_at` only after this returns. The 404 is unambiguous here: every caller passes through
    `_require_human` first, so the row is already proven to exist.

    Any OTHER Keycloak failure still raises — fail-closed is unchanged for real users."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        resp = await _kc_raw(client, token, "GET", f"/users/{sub}")
        if resp.status_code == status.HTTP_404_NOT_FOUND:
            log.info("kc_user_absent_no_identity", sub=sub, op="set_enabled")
            return False
        resp.raise_for_status()
        rep = resp.json()
        rep["enabled"] = enabled
        await _kc(client, token, "PUT", f"/users/{sub}", json=rep)
        log.info("kc_user_set_enabled", sub=sub, enabled=enabled)
        return True


async def delete_user(sub: str) -> bool:
    """DELETE the Keycloak identity — irreversible. The account is gone and its username/email are
    freed for reuse (decision-user-lifecycle.md F4).

    This is the REAL half of "remove a user": the sign-in identity is genuinely destroyed. What may
    survive is the `platform.users` row, and only so historical created_by / updated_by / audit
    references stay resolvable — the caller decides that, not this function.

    Returns True if an identity was deleted, False if there was none to delete (already-removed, or
    an attribution-only principal). Idempotent by that contract, so a re-run after a partial
    failure completes cleanly rather than 404-ing."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        resp = await _kc_raw(client, token, "DELETE", f"/users/{sub}")
        if resp.status_code == status.HTTP_404_NOT_FOUND:
            log.info("kc_user_absent_no_identity", sub=sub, op="delete")
            return False
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "kc_admin_rejected", method="DELETE", path=f"/users/{sub}",
                upstream_status=exc.response.status_code, detail=exc.response.text[:500],
            )
            raise _upstream("identity provider admin call failed") from exc
        log.info("kc_user_deleted", sub=sub)
        return True


async def all_user_subs() -> set[str]:
    """Every sub that HAS a Keycloak identity, in one call.

    Feeds the Users list's `can_sign_in` flag (decision-user-lifecycle.md F1): the page lists both
    real sign-in identities and attribution-only principals, and offering password/role actions on
    the latter is offering operations that cannot work. Set membership rather than a per-row lookup
    keeps this ONE Keycloak call per list, not one per user.

    `_roles_by_sub` is not a substitute: a real user may hold no realm role and would be
    misclassified as identity-less."""
    cap = 10000
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        resp = await _kc(
            client, token, "GET", "/users",
            params={"max": cap, "briefRepresentation": True},
        )
    try:
        subs = {str(u["id"]) for u in resp.json() if u.get("id")}
    except (ValueError, TypeError):
        raise _upstream("identity provider returned an unreadable user list") from None
    if len(subs) >= cap:
        # Truncated: the users beyond the cap would be misread as identity-less and silently lose
        # their password/role actions. A wrong answer is worse than no answer here, so refuse to
        # answer at all — the caller degrades every row to can_sign_in=True, which only over-offers
        # actions the server still gates. Never let a paging limit look like a missing account.
        log.warning("kc_user_list_truncated", cap=cap)
        raise _upstream("identity provider user list exceeded the page limit")
    return subs


async def set_user_realm_role(sub: str, role_name: str, assignable: tuple[str, ...]) -> None:
    """Set the user's realm role to EXACTLY `role_name` among the `assignable` tier: add it if
    missing, then remove any OTHER assignable role the user holds. Roles outside `assignable`
    (e.g. default-roles-*) are left untouched. Idempotent."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        current = (await _kc(client, token, "GET", f"/users/{sub}/role-mappings/realm")).json()
        current_names = {r["name"] for r in current}
        if role_name not in current_names:
            role = (await _kc(client, token, "GET", f"/roles/{role_name}")).json()
            await _kc(
                client, token, "POST", f"/users/{sub}/role-mappings/realm",
                json=[{"id": role["id"], "name": role["name"]}],
            )
        stale = [r for r in current if r["name"] in assignable and r["name"] != role_name]
        if stale:
            await _kc(
                client, token, "DELETE", f"/users/{sub}/role-mappings/realm",
                json=[{"id": r["id"], "name": r["name"]} for r in stale],
            )
        log.info("kc_user_role_set", sub=sub, role=role_name)


# --- human user operations (Talus 27) -------------------------------------------
# The user-admin slice extends the provisioner from CLIENTS to interactive USERS. Same
# `manage-users` realm-mgmt role the SA flow already uses to map roles (it also permits
# user CRUD + reset-password), so no extra Keycloak grant is needed. Same fail-closed
# posture: any Admin-API error raises (502/503), never a silent partial create.


async def create_user(username: str, email: str, display_name: str) -> str:
    """Create an ENABLED Keycloak user (a human principal) and return its id — which IS the
    sub == platform.users.id. 409 (HTTPException) if the username/email already exists: surfaced
    honestly rather than silently reusing someone else's account (mirrors create_sa_client).

    emailVerified=true so no SMTP-dependent verify action blocks first login (air-gapped-safe);
    the one-time temp password (set separately) carries the UPDATE_PASSWORD required action that
    forces a reset instead. display_name is split into first/last for the Keycloak console only —
    platform.users.display_name stays the authoritative label the portal renders."""
    first, _, last = display_name.strip().partition(" ")
    rep = {
        "username": username,
        "email": email,
        "firstName": first or display_name.strip(),
        "lastName": last,
        "enabled": True,
        "emailVerified": True,
    }
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        created = await _kc_raw(client, token, "POST", "/users", json=rep)
        if created.status_code == status.HTTP_409_CONFLICT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a user with that username or email already exists",
            )
        try:
            created.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "kc_user_create_rejected",
                username=username,
                upstream_status=exc.response.status_code,
                detail=exc.response.text[:500],
            )
            raise _upstream("could not create the user") from exc

        sub = _id_from_location(created) or await _user_id_by_username(client, token, username)
        if not sub:
            raise _upstream("created the user but could not read it back")
        log.info("kc_user_created", username=username, sub=sub)
        return sub


async def set_user_temp_password(sub: str, password: str) -> None:
    """Set a TEMPORARY password on a user: `temporary=true` adds the UPDATE_PASSWORD required
    action so Keycloak forces a reset at first login. The plaintext is shown to the admin ONCE by
    the caller and never stored by Talus (mirrors the SA secret — recovered by reset, not retrieval)."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        await _kc(
            client,
            token,
            "PUT",
            f"/users/{sub}/reset-password",
            json={"type": "password", "value": password, "temporary": True},
        )
        log.info("kc_user_temp_password_set", sub=sub)


async def user_federated_identities(sub: str) -> list[str]:
    """The identity-provider aliases linked to this user (empty = a local-password account).

    Talus 48: a brokered/SSO user has NO local password, so a password grant for them fails with
    the same "Invalid user credentials" a wrong password gives. Asking this FIRST is what lets the
    refusal name their identity provider instead of implying they typed it wrong — and it spends
    no brute-force attempt on an account that structurally cannot succeed."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        resp = await _kc(client, token, "GET", f"/users/{sub}/federated-identity")
    try:
        return [str(e.get("identityProvider", "")) for e in resp.json() if e.get("identityProvider")]
    except ValueError:
        return []


# --- self-service password change (decision-first-ten-minutes.md F3) ------------------------
# Two calls, deliberately separate: PROVE POSSESSION, then write. Anything that skips the first
# turns a lifted bearer token into a full account takeover, so they are never merged.


async def verify_user_password(username: str, password: str) -> tuple[bool, str]:
    """Does `password` currently authenticate `username`? Returns (ok, reason).

    Mechanism: a direct-access (password) grant as the confidential `talus-api` client. The
    returned token is DISCARDED — this is a yes/no oracle, never a way to mint a session. The
    username must come from server-validated token claims; a caller-supplied username would let
    the body name whose password is being checked.

    ★ The realm is bruteForceProtected (failureFactor 10): a wrong password here IS a failed auth
    attempt and counts toward lockout. That is correct, and it is why `reason` distinguishes
    "locked" from "bad_credentials" — the UI must be able to say so before a careful user locks
    themself out. Reasons: "" (ok) | "bad_credentials" | "locked".
    """
    if not settings.keycloak_api_client_secret:
        log.error("self_password_unconfigured", missing="TALUS_API_CLIENT_SECRET")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="self-service password change is not configured on this deployment",
        )
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        try:
            resp = await client.post(
                _token_url(),
                data={
                    "grant_type": "password",
                    "client_id": settings.keycloak_api_client_id,
                    "client_secret": settings.keycloak_api_client_secret,
                    "username": username,
                    "password": password,
                },
            )
        except httpx.HTTPError as exc:
            log.warning("kc_password_verify_unreachable", error=exc.__class__.__name__)
            raise _upstream("identity provider unreachable") from exc
    if resp.status_code == 200:
        return True, ""
    # Never log the attempted value; the description is Keycloak's, not the user's.
    desc = ""
    try:
        desc = str(resp.json().get("error_description", ""))
    except ValueError:
        desc = ""
    low = desc.lower()
    if "temporarily disabled" in low or "temporarily locked" in low:
        reason = "locked"
    else:
        # Everything else the token endpoint says is "invalid_grant / Invalid user credentials",
        # including a federated user who has no local password at all — the two are genuinely
        # indistinguishable HERE. That case is caught BEFORE this call (the router checks
        # user_federated_identities), so it never reaches this branch to be mislabelled.
        reason = "bad_credentials"
    log.info("kc_password_verify_failed", username=username, reason=reason, upstream=resp.status_code)
    return False, reason


async def set_user_password(sub: str, password: str) -> None:
    """Set a PERMANENT password (`temporary=False`) — no UPDATE_PASSWORD required action, so the
    user is not immediately forced to change what they just chose. Contrast set_user_temp_password,
    which is the admin-resets-SOMEONE-ELSE path and deliberately forces a reset.

    Keycloak validates the realm password policy on this write, so policy enforcement lives in one
    place. A 400 is the user's problem to fix and its reason is surfaced verbatim-ish; anything
    else is ours and fails closed as 502."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        resp = await _kc_raw(
            client,
            token,
            "PUT",
            f"/users/{sub}/reset-password",
            json={"type": "password", "value": password, "temporary": False},
        )
        if resp.status_code == 400:
            detail = "the new password does not meet this deployment's password policy"
            try:
                msg = str(resp.json().get("errorMessage", "")).strip()
            except ValueError:
                msg = ""
            if msg:
                detail = f"{detail} ({msg})"
            log.info("kc_password_policy_rejected", sub=sub, upstream_message=msg)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "kc_admin_rejected",
                method="PUT",
                path=f"/users/{sub}/reset-password",
                upstream_status=exc.response.status_code,
                detail=exc.response.text[:500],
            )
            raise _upstream("identity provider admin call failed") from exc
        log.info("kc_user_password_changed", sub=sub)


async def user_realm_roles(sub: str) -> list[str]:
    """The user's CURRENT realm-role names (GET /users/{sub}/role-mappings/realm).

    Interop I-3: a personal access token carries no Keycloak claims, so the GeoServices write
    path resolves the OWNER's live roles here to run the SAME three-key edit decision every
    other write runs — evaluated at request time (a role downgrade takes effect immediately;
    no stale mint-time snapshot). Fail-closed: an unreachable/unconfigured IdP raises the
    _kc 502/503, so a PAT write can never skip the role key."""
    async with httpx.AsyncClient(timeout=_KC_ADMIN_TIMEOUT) as client:
        token = await _admin_token(client)
        resp = await _kc(client, token, "GET", f"/users/{sub}/role-mappings/realm")
        return [r.get("name", "") for r in resp.json()]
