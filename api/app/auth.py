"""
Auth dependency. Two modes, selected by settings.auth_mode:

- "stub": returns the validate-phase1 user without checking any token. Dev only.
- "keycloak": validates a Bearer JWT against the talus realm, then resolves
  the sub claim to a row in platform.users.

Router signatures (`current_user: User = Depends(get_current_user)`) are stable
across modes — FastAPI resolves get_current_user's internal sub-deps transparently.
"""
import asyncio
import base64
import hashlib
import time
from typing import Any
from uuid import UUID

import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient, PyJWKClientError
from psycopg import AsyncConnection
from psycopg import errors as pg_errors

from .config import settings
from .db import get_db
from .models.user import User
from .ratelimit import PAT_SHAPE_RE, forget_validated_pat, note_validated_bearer, note_validated_pat

log = structlog.get_logger(__name__)


STUB_USER = User(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    username="validate-phase1",
    email="validate-phase1@talusgis.invalid",
    display_name="Validate Phase 1",
    is_admin=True,  # stub mode is full dev access; mirrors stub authorizing every gate
)


# auto_error=False so a missing/malformed header reaches our code (we want to
# return a uniform 401 with our own message, not FastAPI's default 403).
_bearer = HTTPBearer(auto_error=False)


# Module-level JWKS client so its cache is shared across requests. Recreated
# inside _refresh_jwks_client() when we suspect key rotation.
_jwks_client: PyJWKClient | None = None
_jwks_lock = asyncio.Lock()


def _build_jwks_client() -> PyJWKClient:
    return PyJWKClient(
        settings.keycloak_jwks_url,
        cache_keys=True,
        lifespan=settings.keycloak_jwks_cache_ttl_seconds,
    )


# An unknown key id may refetch the sign-in server's key set at most once per interval. Long enough
# that tokens cannot be used to make the api hammer the sign-in server; short enough that a real key
# rotation is picked up promptly (the cache's own lifespan is the normal refresh).
_JWKS_FORCED_REFETCH_MIN_INTERVAL_S = 30.0
_jwks_last_forced_refetch: float | None = None


async def _get_signing_key(token: str):
    """The signing key for `token`, from the cached key set; an UNKNOWN key id refetches the set at
    most once per _JWKS_FORCED_REFETCH_MIN_INTERVAL_S (key rotation between cache lifespans).

    ★ The key id comes from the token's UNVERIFIED header — attacker-chosen. The first version
    handed the token to PyJWKClient.get_signing_key_from_jwt, which refetches the key set whenever
    the id is not in its cache, and then rebuilt the client and tried again: two requests to the
    sign-in server for every request carrying a made-up id (decision-once-over-security-fixes.md
    A7). Untrusted input still decides nothing about WHERE the fetch goes (the configured JWKS URL)
    — the budget bounds WHEN. Fail-closed: an unknown id with the budget spent is PyJWKClientError,
    which every caller turns into the uniform 401.

    PyJWKClient does sync HTTP, so the network-touching calls run in a thread."""
    global _jwks_client, _jwks_last_forced_refetch
    if _jwks_client is None:
        async with _jwks_lock:
            if _jwks_client is None:
                _jwks_client = _build_jwks_client()
    client = _jwks_client
    kid = jwt.get_unverified_header(token).get("kid")   # PyJWTError on a non-token → 401 upstream
    # Cached read: PyJWKClient answers from memory within its lifespan and fetches only when the
    # cache is empty or expired — the ordinary, time-driven refresh.
    key = client.match_kid(await asyncio.to_thread(client.get_signing_keys), kid) if kid else None
    if key is not None:
        return key
    async with _jwks_lock:
        now = time.monotonic()
        if (_jwks_last_forced_refetch is not None
                and now - _jwks_last_forced_refetch < _JWKS_FORCED_REFETCH_MIN_INTERVAL_S):
            raise PyJWKClientError("unknown signing key id (key refetch budget spent)")
        _jwks_last_forced_refetch = now
    key = client.match_kid(await asyncio.to_thread(client.get_signing_keys, True), kid) if kid else None
    if key is None:
        raise PyJWKClientError("unknown signing key id")
    return key


_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

# The Basic challenge for the PAT/desktop path — shared by the OGC sign-in door's initial 401 and
# every Basic-credential rejection below. Two deliberate choices:
#  - RFC 7235: a 401 advertises the scheme the caller should use. A failed BASIC attempt must be
#    answered with `Basic`, not `Bearer` — QGIS/Pro treat a Bearer answer as "this server doesn't
#    do Basic" and silently STOP re-prompting (found live: a revoked PAT produced an empty,
#    prompt-less connection until restart).
#  - The realm renders verbatim in both clients' credential dialogs (QGIS: "Realm: <realm> at
#    <host>") — free, zero-surface guidance at exactly the moment users type their PORTAL password
#    by mistake (the #1 anticipated support case).
BASIC_CHALLENGE = 'Basic realm="Talus GIS - sign in with an access token, not your portal password"'

# The 401-INVITE (interop I-3). When a PAT authenticates fine but its
# owner can't VIEW the requested layer, the desktop façade doors answer 401 + THIS challenge
# instead of a dead-end 403 — so the client re-prompts and the user can paste a DIFFERENT token
# (the fix is "use another token," and only a challenge triggers a re-prompt in Pro/QGIS). The
# realm renders verbatim in the credential dialog, so it explains WHY. Scoped to PAT callers on
# the esri/ogc doors only (see routers/geoservices.py, routers/ogc.py); Bearer stays 403, anon
# stays 404. Still fail-closed — no data served, and a 401 leaks less than a 403 (which would
# confirm the resource exists).
# ASCII-only: WWW-Authenticate is an HTTP header (latin-1) — no em-dash/smart-quote (a —
# realm 500s in Starlette's header encoder).
BASIC_CHALLENGE_NO_VIEW = (
    'Basic realm="This access token cannot see this layer - sign in with a token whose account '
    'has access"'
)

_BASIC_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing credentials",
    headers={"WWW-Authenticate": BASIC_CHALLENGE},
)

# Talus Field f5 (decision-talus-field-f5.md §4.1): WHY a once-valid PAT is refused, as a response
# header the phone reads to choose between "re-mint" (expired) and "wipe the pack" (revoked). Two
# values only: a revoked token and a disabled / removed OWNER both say `token_revoked` — the caller
# learns its token is dead, not that a user is — and an UNKNOWN or malformed credential carries no
# header at all (no existence signal). Status, body and the Basic challenge are unchanged, so every
# other PAT client (Pro, QGIS, the façade) sees exactly what it saw before.
AUTH_CODE_HEADER = "X-Talus-Auth-Code"
PAT_CODE_REVOKED = "token_revoked"
PAT_CODE_EXPIRED = "token_expired"


# The header a BROWSER-hosted PAT client sends so a 401 comes back WITHOUT the Basic challenge:
# WebKit answers any 401 + `WWW-Authenticate: Basic` on a fetch with its own native "Sign in to
# <host>" sheet, even when the request carried its own Authorization header — an iPhone
# showed it the moment a device token was revoked. Desktop GIS NEEDS the challenge (it is what
# re-prompts Pro/QGIS), so it stays the default; only a client that says it is the field app opts out.
FIELD_CLIENT_HEADER = "x-talus-client"
FIELD_CLIENT_VALUE = "field"


def pat_denied(code: str | None, *, challenge: bool = True) -> HTTPException:
    """The PAT path's 401 — the Basic challenge (unless the client opted out), plus the reason
    header when `code` names one."""
    headers = {"WWW-Authenticate": BASIC_CHALLENGE} if challenge else {}
    if code:
        headers[AUTH_CODE_HEADER] = code
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing credentials",
        headers=headers,
    )


async def _user_from_db(conn: AsyncConnection, user_id: UUID) -> User | None:
    """Resolve a validated token's `sub` -> the active Talus principal, or None (which both auth
    dependencies turn into a 401). None covers two deny cases, each logged with its own reason:
      - no platform.users row (an un-provisioned identity — the admin-provision gate), and
      - a row with `disabled_at` set (offboarded). Talus 48/27: Keycloak only blocks NEW logins, so a
        token minted just before the disable stays cryptographically valid (~5 min). Rejecting the
        disabled row HERE closes that window at the API boundary on the user's next request.
      - a row with `removed_at` set (V091): the account was deleted and this row survives ONLY to
        keep historical references resolvable. It is a record, never a principal. Delete already
        requires the account to be disabled first, so `disabled_at` alone would cover this today —
        checked separately anyway so the guarantee is STRUCTURAL rather than a consequence of the
        delete path's ordering. Personal access tokens ride this same resolver, so a removed user's
        app passwords stop working here too, not just their SSO login."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, username, email, display_name, principal_type, disabled_at, removed_at "
            "FROM platform.users WHERE id = %s",
            (str(user_id),),
        )
        row = await cur.fetchone()
    if row is None:
        log.info("auth_rejected", reason="no_platform_user_row", user_id=str(user_id))
        return None
    if row[6] is not None:
        log.info("auth_rejected", reason="user_removed", user_id=str(user_id))
        return None
    if row[5] is not None:
        log.info("auth_rejected", reason="user_disabled", user_id=str(user_id))
        return None
    return User(
        id=row[0], username=row[1], email=row[2], display_name=row[3], principal_type=row[4]
    )


async def _touch_last_seen(conn: AsyncConnection, user_id: UUID) -> None:
    """Best-effort, THROTTLED 'last seen' stamp for an authenticated principal. Updates at most once
    per hour per user (no-op via the WHERE clause otherwise), so it's cheap on the auth hot path.
    Powers the Admin → Users 'Pending = never signed in' metric (last_seen_at IS NULL) and a future
    last-active display. NON-CRITICAL: any failure rolls back and logs — it must never fail the
    request that triggered it."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE platform.users SET last_seen_at = now() "
                "WHERE id = %s AND (last_seen_at IS NULL OR last_seen_at < now() - interval '1 hour')",
                (str(user_id),),
            )
        await conn.commit()
    except Exception as exc:  # noqa: BLE001 — non-critical side effect; never break auth
        log.warning("last_seen_touch_failed", user_id=str(user_id), detail=str(exc))
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass


async def _validated_claims(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any] | None:
    """Validate the bearer token and return its claims.

    None in stub mode (no token is checked). In keycloak mode, raises a uniform
    401 on a missing/invalid token. Shared by get_current_user and the role/capability
    gates so the token is decoded once per request (FastAPI caches this dependency).
    """
    if settings.auth_mode == "stub":
        return None

    if settings.auth_mode != "keycloak":
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"auth_mode={settings.auth_mode!r} not implemented",
        )

    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        log.info("auth_rejected", reason="missing_or_malformed_bearer")
        raise _CREDENTIALS_EXCEPTION

    token = creds.credentials
    try:
        signing_key = await _get_signing_key(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.keycloak_audience,
            issuer=settings.keycloak_issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
        # PROVEN: signature, issuer, audience, expiry. Only now may the rate limiter treat this
        # token as a session (ratelimit.py — a Bearer is exempt only once validated, A7).
        note_validated_bearer(token, claims.get("exp"))
        return claims
    except jwt.PyJWTError as exc:
        log.info("auth_rejected", reason=exc.__class__.__name__, detail=str(exc))
        raise _CREDENTIALS_EXCEPTION from exc
    except PyJWKClientError as exc:
        log.warning("JWKS fetch failed: %s", exc)
        raise _CREDENTIALS_EXCEPTION from exc


def _claims_is_admin(claims: dict[str, Any] | None) -> bool:
    """Whether the token carries the talus-admin realm role. Lazy import of authz._has_admin_role to avoid the
    auth↔authz module cycle (authz imports get_optional_user from here). Stamped onto User.is_admin at auth."""
    from .authz import _has_admin_role  # local import: break the import cycle

    return _has_admin_role(claims)


async def get_current_user(
    claims: dict[str, Any] | None = Depends(_validated_claims),
    conn: AsyncConnection = Depends(get_db),
) -> User:
    if settings.auth_mode == "stub":
        return STUB_USER

    # keycloak mode: _validated_claims guarantees claims is not None here.
    assert claims is not None
    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise _CREDENTIALS_EXCEPTION from exc

    user = await _user_from_db(conn, user_id)
    if user is None:
        # _user_from_db logged the precise reason (no_platform_user_row | user_disabled). Lazy
        # user-creation is a Phase 2 policy decision; an unknown/disabled sub is denied here.
        raise _CREDENTIALS_EXCEPTION
    user.is_admin = _claims_is_admin(claims)  # token-derived admin role → god-mode READ (view_allowed)
    await _touch_last_seen(conn, user_id)  # throttled, best-effort (Talus 28)
    return user


# --- optional-auth seam (Talus 23): the ONLY path that admits anonymous callers --------------
# Public read endpoints depend on these instead of get_current_user. A MISSING Authorization header
# yields None (anonymous) rather than a 401; a PRESENT but invalid/expired token still 401s (a
# caller who sent a token wants to be authenticated — a bad one is an error, not a silent downgrade
# to anonymous). The existing get_current_user 401 gate is untouched; this is purely additive.


async def _optional_claims(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any] | None:
    """Validated claims when a token is present, else None (anonymous). Stub mode: None (no token
    checked). Keycloak mode: None when no header; otherwise validate via _validated_claims (which
    raises 401 on a bad token)."""
    if settings.auth_mode == "stub":
        return None
    if creds is None:
        return None  # anonymous — no Authorization header
    return await _validated_claims(creds)


async def get_optional_user(
    request: Request,
    claims: dict[str, Any] | None = Depends(_optional_claims),
    conn: AsyncConnection = Depends(get_db),
) -> User | None:
    """The optional-auth principal: a real User for a valid token, or None for an anonymous caller.
    Never 401s on the no-token case (that IS the anonymous path); a valid token whose sub has no
    platform.users row still 401s (a token must map to a user). Stub mode returns STUB_USER (dev).

    Interop I-1 (decision-desktop-interop-i1.md): this seam ALSO resolves a personal access token
    sent as HTTP Basic (the desktop path — QGIS auth configs / Pro "Server Authentication"). PATs
    ride the OPTIONAL-auth surfaces only — the read side (features/columns/extent/export/tiles/
    ogc); get_current_user (every write path + /me/*) stays Bearer-only until I-3's read_write
    story needs it. A presented-but-invalid PAT 401s (a credential is a claim to identity — never
    a silent downgrade to anonymous), same posture as a bad Bearer.

    NOTE: a None return means ANONYMOUS here — unlike `claims is None`, which the role helpers
    (_has_admin_role etc.) treat as stub-mode-authorized. Callers MUST NOT pass an anonymous
    caller's (None) claims to those helpers; gate role checks on `principal is not None`."""
    if settings.auth_mode == "stub":
        return STUB_USER
    if claims is None:
        # No (valid-scheme) Bearer. A Basic header is the PAT path; no header at all is anonymous.
        return await _pat_user(request, conn)
    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise _CREDENTIALS_EXCEPTION from exc
    user = await _user_from_db(conn, user_id)
    if user is None:
        # _user_from_db logged the precise reason (no_platform_user_row | user_disabled) — the SSO
        # disabled-user deny (Talus 48/27) closes the offboarding token window on THIS path too.
        raise _CREDENTIALS_EXCEPTION
    user.is_admin = _claims_is_admin(claims)  # token-derived admin role (admin-sees-all), like get_current_user
    return user


async def require_principal(principal: User | None = Depends(get_optional_user)) -> User:
    """A signed-in principal by session OR personal access token (a desktop PAT, a Field device token)
    — anonymous 401s exactly as get_current_user does. For READ doors that serve no data of their own
    (a locator proxy, a coordinate transform) and so need no view gate, only a caller who is somebody.
    A presented-but-bad credential already 401'd inside the seam, with the PAT reason header.
    First used by the geocode doors (Talus Field f7b, inv44); the transform door followed (inv45)."""
    if principal is None:
        raise _CREDENTIALS_EXCEPTION
    return principal


# --- personal access tokens (interop I-1, decision-desktop-interop-i1.md) --------------------
# Token shape: tpat_<secrets.token_urlsafe(32)>. Stored as sha256 hex (V062); compared by hash
# lookup, so timing is index-lookup-shaped, not string-compare-shaped. The PAT may arrive as the
# Basic PASSWORD (the documented recipe: username is anything, e.g. 'talus') or as the USERNAME
# (some clients send an empty password) — whichever half matches the token shape.

# The shape lives in ratelimit.py (which cannot import this module — auth runs downstream of the
# middleware), so the pre-auth classifier and this validator can never drift on what a PAT looks like.
_PAT_RE = PAT_SHAPE_RE
_PAT_LAST_USED_THROTTLE_SQL = (
    "UPDATE platform.personal_access_tokens SET last_used_at = now() "
    "WHERE id = %s AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')"
)


def _wants_challenge(request: Request) -> bool:
    """False only for the field app (see FIELD_CLIENT_HEADER); every other PAT client keeps the challenge."""
    return request.headers.get(FIELD_CLIENT_HEADER, "").lower() != FIELD_CLIENT_VALUE


async def _pat_user(request: Request, conn: AsyncConnection) -> User | None:
    """Resolve an `Authorization: Basic` personal access token to its OWNING user, or None when no
    Basic header is present (the anonymous fallthrough). Fail-closed on everything else: malformed
    Basic, a credential that isn't PAT-shaped (Talus has no password auth), an unknown/revoked/
    expired token, or a disabled owner all 401. The resolved user NEVER carries admin god-mode —
    a leaked token must not read every layer, so is_admin stays False regardless of the owner's
    roles (least privilege; the owner uses the portal for admin work)."""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("basic "):
        return None  # not the PAT path — genuine anonymous (or a Bearer already handled upstream)
    try:
        userpart, _, passpart = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except Exception as exc:  # noqa: BLE001 — any undecodable Basic header is a bad credential
        log.info("auth_rejected", reason="malformed_basic_header")
        raise _BASIC_CREDENTIALS_EXCEPTION from exc
    secret = passpart if _PAT_RE.fullmatch(passpart) else userpart if _PAT_RE.fullmatch(userpart) else None
    if secret is None:
        log.info("auth_rejected", reason="basic_credential_not_a_pat")
        raise _BASIC_CREDENTIALS_EXCEPTION
    secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, owner_id, revoked_at IS NOT NULL, "
            "       expires_at IS NOT NULL AND expires_at < now(), "  # DB clock — no host skew
            "       scope "
            "FROM platform.personal_access_tokens WHERE secret_hash = %s",
            (secret_hash,),
        )
        row = await cur.fetchone()
    if row is None:
        log.info("auth_rejected", reason="unknown_pat")
        forget_validated_pat(secret_hash)
        raise _BASIC_CREDENTIALS_EXCEPTION
    token_id, owner_id, revoked, expired = row[0], row[1], bool(row[2]), bool(row[3])
    # Interop I-3: the token's scope rides request.state so a WRITE surface (the GeoServices
    # façade) can gate on read_write without re-resolving the credential. Reads never look at it.
    request.state.pat_scope = row[4]
    # Talus Field f5c: the token's own id, so a paid door can let a DEVICE retire its own credential
    # (`DELETE /me/field-tokens/self`) with no session at hand — the expiry sweep and Remove use it.
    request.state.pat_token_id = token_id
    # Talus Field f4 (decision-talus-field-f4.md §4.2): the per-layer allow-list a DEVICE token
    # carries (V108). Read behind a guard so the auth path never 500s before the migration lands —
    # an absent column reads as NULL, and NULL is "not a field-sync token" (the native doors refuse).
    request.state.pat_layer_ids = await _pat_layer_ids(conn, token_id)
    if revoked or expired:
        log.info("auth_rejected", reason="pat_revoked" if revoked else "pat_expired", token_id=str(token_id))
        forget_validated_pat(secret_hash)
        raise pat_denied(PAT_CODE_REVOKED if revoked else PAT_CODE_EXPIRED, challenge=_wants_challenge(request))
    user = await _user_from_db(conn, owner_id)
    if user is None:
        # Disabled/offboarded owner: their PATs die with them (the Talus 27/48 deny, same window).
        # f5: the phone hears "revoked" — the token is dead either way, and which is not its business.
        forget_validated_pat(secret_hash)
        raise pat_denied(PAT_CODE_REVOKED, challenge=_wants_challenge(request))
    user.is_admin = False  # NEVER god-mode via a PAT (deliberate; see docstring)
    # Rate-limit tiering (decision-desktop-rate-limit-tiering.md): this is the ONLY writer of the
    # validated-PAT cache, and it is reached only after the token proved live, unrevoked, unexpired
    # (DB clock) and owned by a live user. From here the middleware charges this token's requests to
    # its own elevated per-token bucket instead of the caller's per-IP ANONYMOUS bucket — which is
    # what stops ArcGIS Pro / QGIS bulk work from being throttled to 2 features/sec. Every rejection
    # path above evicts, so revocation drops the tier on the token's very next request.
    note_validated_pat(secret_hash)
    # Throttled best-effort last-used stamp (the _touch_last_seen pattern) — never breaks auth.
    try:
        async with conn.cursor() as cur:
            await cur.execute(_PAT_LAST_USED_THROTTLE_SQL, (token_id,))
        await conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("pat_last_used_touch_failed", token_id=str(token_id), detail=str(exc))
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    return user


# --- Talus Field f4: the read_write PAT seam on the NATIVE write doors (I-3's read_write story) ---
#
# decision-talus-field-f4.md §4.2. Until f4, every native feature write was Bearer-only and the only
# PAT write surface was the GeoServices façade. A phone syncing an outbox holds no Bearer — its
# Keycloak session lapsed hours ago — so the create / update / attachment doors accept a PAT that is
# (a) read_write AND (b) a field-sync token (`layer_ids` set, V108) AND (c) minted for THIS layer.
# Anything else is refused with the façade's sentences. Delete, import, calc and /me/* stay
# Bearer-only. The edit decision itself is unchanged: `edit_allowed` runs with claims synthesised
# from the owner's LIVE realm roles (the façade's own pattern), so a revoked editor role bites on the
# very next write, and the PAT resolver's `is_admin=False` still holds — a device token is never
# admin.

_ROLE_CACHE: dict[str, tuple[float, list[str]]] = {}
_ROLE_TTL_S = 60.0


async def pat_owner_claims(user_id: str) -> dict[str, Any]:
    """Claims for the three-key edit decision when the credential is a PAT (no JWT to read roles
    from): the owner's realm roles, resolved live from Keycloak (60 s cache), in the shape
    `_has_edit_role` reads. Fail-closed: IdP trouble is 502/503 from the admin client, never an
    empty role list. Lifted from the GeoServices façade (which now imports it) for f4."""
    from . import keycloak_admin  # local import: keycloak_admin imports auth

    hit = _ROLE_CACHE.get(user_id)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _ROLE_TTL_S:
        roles = hit[1]
    else:
        roles = await keycloak_admin.user_realm_roles(user_id)
        _ROLE_CACHE[user_id] = (now, roles)
    return {"sub": user_id, "realm_access": {"roles": roles}}


async def _pat_layer_ids(conn: AsyncConnection, token_id: Any) -> list[str] | None:
    """The token's `layer_ids` (V108), or None when unset OR when the column does not exist yet."""
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT layer_ids FROM platform.personal_access_tokens WHERE id = %s", (token_id,)
            )
            row = await cur.fetchone()
    except pg_errors.UndefinedColumn:  # before V108; the transaction is unusable after
        await conn.rollback()
        return None
    # Any OTHER failure propagates (a 5xx): "could not read the list" must never be mistaken for
    # "no list". None means an ordinary token, and the GeoServices edit gate lets an ordinary
    # read_write token through — so swallowing an error here would let a DEVICE credential pass as
    # one (found by the independent review of once-over fix A4). Fail closed.
    if row is None or row[0] is None:
        return None
    return [str(x) for x in row[0]]


def rw_pat_gate(scope: str | None, layer_ids: list[str] | None, layer_id: str) -> str | None:
    """The PURE decision for a PAT on a native write door: None = allowed, else the refusal sentence.
    Three keys, in the order a crew member can fix them: the token must be read_write, must be a
    field-sync token (layer_ids set), and must name THIS layer."""
    if scope != "read_write":
        return ("This access token is read-only. Create a “Read & edit” token "
                "(in the portal: Profile → Access tokens) and reconnect with it.")
    if layer_ids is None:
        return ("This access token cannot write features directly — only a Talus Field device "
                "token (minted for a pack) can. Use the field app, or the GeoServices endpoint.")
    if layer_id not in layer_ids:
        return "This device token was minted for another pack — it cannot write this layer."
    return None


async def get_current_user_rw(
    layer_id: str,
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn: AsyncConnection = Depends(get_db),
) -> User:
    """The write-door principal: a Bearer exactly as `get_current_user` resolves it, OR a read_write
    field-sync PAT scoped to `layer_id`. Stores the claims the edit decision needs on
    `request.state.rw_claims` (the JWT's, or the PAT owner's live roles) for `rw_claims` to hand
    to the door. Stub mode: STUB_USER with no claims (the role helpers treat None as authorised)."""
    if settings.auth_mode == "stub":
        request.state.rw_claims = None
        return STUB_USER
    if creds is not None:
        claims = await _validated_claims(creds)
        request.state.rw_claims = claims
        return await get_current_user(claims, conn)
    user = await _pat_user(request, conn)
    if user is None:
        raise _CREDENTIALS_EXCEPTION  # no credential at all — the door is not anonymous
    deny = rw_pat_gate(getattr(request.state, "pat_scope", None), getattr(request.state, "pat_layer_ids", None), layer_id)
    if deny is not None:
        log.info("authz_rejected", capability=f"{layer_id}:write", reason="pat_not_field_sync_scope", user_id=str(user.id))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=deny)
    request.state.rw_claims = await pat_owner_claims(str(user.id))
    return user


def rw_claims(request: Request) -> dict[str, Any] | None:
    """The claims `get_current_user_rw` resolved for this request (declare it AFTER the user
    dependency in a door's signature so FastAPI runs the resolver first)."""
    return getattr(request.state, "rw_claims", None)
