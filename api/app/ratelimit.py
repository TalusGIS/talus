"""Surface rate limiting (Talus 23; desktop tiering — decision-desktop-rate-limit-tiering.md).

A lightweight in-memory token-bucket limiter with two tiers:

  ANONYMOUS  per-IP, three cost classes (tile / download / read). Bounds credential-probing and
             bulk egress from the public surface (T23 public access, T33 Open Data).
  AUTHENTICATED  per-TOKEN, one class, a larger ceiling (AUTH_RATE_PER_MIN). Reached ONLY by a
             `Basic` PAT that has been VALIDATED against the DB downstream within the last
             _VALIDATED_TTL_S seconds.

EXEMPT entirely: VALIDATED `Bearer` sessions (Keycloak-issued — rate-limited upstream at the IdP
and short-lived), OPTIONS preflight, /health.

A `Bearer` IS EXEMPT ONLY ONCE PROVEN — the same rule as the PAT tier below, and for the same reason
(decision-once-over-security-fixes.md A7). Until that fix the exemption was on the scheme alone, so a
junk `Bearer x` header bought an unthrottled path to every door. Now `auth.py::_validated_claims`
records the hash of a token AFTER it validates (signature, issuer, audience, expiry), for
_VALIDATED_TTL_S and never past the token's own expiry; a Bearer whose hash is recorded is exempt;
any other Bearer is charged to a per-IP class of its own (BEARER_BOOTSTRAP_RATE_PER_MIN). Its own
class, so an address whose anonymous budget is spent can still present a fresh session token.

WHY THE TIER IS KEYED ON A VALIDATED HASH AND NOT ON THE SCHEME. Exempting (or elevating) on the
mere PRESENCE of an Authorization header would let an unauthenticated attacker opt out of the anon
throttle by sending a junk `Basic` header, silently disabling the bound on credential-probing AND
data egress. So the middleware — which necessarily runs BEFORE auth, since an unauthenticated flood
must be bounded before it reaches the app — cannot decide on the header alone. Instead it consults a
short-TTL cache of sha256 hashes that `auth.py::_pat_user` populates ONLY after proving, against the
DB, that the token exists, is unrevoked, is unexpired (DB clock), and has a live owner. The cache is
keyed by the hash of the secret, so membership cannot be reached without possessing the plaintext
token. A junk, unknown, revoked, or expired credential can never test positive; every failure mode
of the lookup yields the ANON bucket. Auth itself is still enforced downstream on every request —
this only decides which bucket a request is charged to, never whether it is allowed.

The authenticated bucket is keyed per-TOKEN rather than per-IP on purpose: one PAT gets one budget
globally, so rotating source IPs — the standard amplification move — buys an attacker nothing. The
corollary is that one PAT shared across several machines shares one budget; mint one per client.

Revocation lag is bounded at _VALIDATED_TTL_S (60s) and is normally ONE request: a revoked token's
next request fails downstream, which evicts the entry. Access is not extended at all — a revoked PAT
401s immediately; only the bucket CLASSIFICATION can lag.

Tiles get a larger anon bucket than other reads (a single public-map pan fires many tile requests).

This is the TURNKEY default — no external dependency, no shared store. For a production internet-
facing deployment, a reverse proxy (nginx/traefik) rate limit in front of the api is the real
defense; this bounds load for a single-instance `docker compose up`. State is per-process (resets on
restart, not shared across replicas), which is fine at that scale. Tune via ANON_READ_RATE_PER_MIN /
ANON_TILE_RATE_PER_MIN / ANON_DOWNLOAD_RATE_PER_MIN / AUTH_RATE_PER_MIN (see .env.example).
"""
import base64
import hashlib
import re
import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import settings

log = structlog.get_logger(__name__)

_EXEMPT_PATHS = frozenset({"/health"})
_PRUNE_INTERVAL_S = 60.0   # how often to drop idle buckets
_IDLE_EVICT_S = 300.0      # a bucket idle this long is dropped (bounds memory)

# --- validated-PAT cache (the pre-auth/post-auth bridge) -------------------------------------
# Token shape lives HERE, not in auth.py, because the dependency runs middleware -> auth (auth.py
# imports this module; the reverse would be circular). auth.py is the consumer of record and imports
# PAT_SHAPE_RE so the two can never drift apart on what a PAT looks like.
PAT_SHAPE_RE = re.compile(r"^tpat_[A-Za-z0-9_\-]{20,64}$")

_VALIDATED_TTL_S = 60.0   # revocation bound — see module docstring + decision doc §5.3
_VALIDATED_MAX = 4096     # memory cap; only VALIDATED tokens create entries, so this is unreachable
                          # by an attacker (it bounds a legitimately huge deployment, not an abuser)
_MAX_BASIC_HEADER = 1024  # sanity guard before base64-decoding attacker-controlled bytes

_validated: dict[str, float] = {}   # sha256-hex of the PAT secret -> monotonic expiry

# The same bridge for Bearer sessions: sha256-hex of the token -> monotonic expiry. Written ONLY by
# note_validated_bearer (called by auth.py after jwt.decode succeeded), so membership cannot be
# reached without a token the sign-in server really issued.
_validated_bearer: dict[str, float] = {}
_MAX_BEARER_HEADER = 16 * 1024      # a real access token is 1–3 KB; never hash an arbitrarily long header


def _bearer_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()


def note_validated_bearer(token: str, exp: float | int | None) -> None:
    """Record that this Bearer token VALIDATED. `exp` is the token's own `exp` claim (epoch
    seconds): the entry never outlives it, and a token with no usable expiry is not recorded."""
    try:
        remaining = float(exp) - time.time()
    except (TypeError, ValueError):
        return
    if remaining <= 0:
        return
    now = time.monotonic()
    key = _bearer_hash(token)
    if key not in _validated_bearer and len(_validated_bearer) >= _VALIDATED_MAX:
        for k, e in list(_validated_bearer.items()):
            if e <= now:
                del _validated_bearer[k]
        if len(_validated_bearer) >= _VALIDATED_MAX:
            del _validated_bearer[min(_validated_bearer, key=lambda k: _validated_bearer[k])]
    _validated_bearer[key] = now + min(_VALIDATED_TTL_S, remaining)


def _is_validated_bearer(header: str, now: float) -> bool:
    """True iff `header` ("Bearer <token>") carries a token recorded as validated and not lapsed.
    Total and non-raising; an over-long header is simply "not validated"."""
    if len(header) > _MAX_BEARER_HEADER:
        return False
    key = _bearer_hash(header[7:].strip())
    exp = _validated_bearer.get(key)
    if exp is None:
        return False
    if exp <= now:
        del _validated_bearer[key]
        return False
    return True


def note_validated_pat(secret_hash: str) -> None:
    """Record that this PAT hash resolved to a LIVE token owned by a LIVE user. Called by
    auth.py::_pat_user on the success path ONLY — this is the sole writer, and the reason the
    elevated bucket is unreachable without a proven-live credential."""
    now = time.monotonic()
    if secret_hash not in _validated and len(_validated) >= _VALIDATED_MAX:
        for k, exp in list(_validated.items()):   # expired entries first
            if exp <= now:
                del _validated[k]
        if len(_validated) >= _VALIDATED_MAX:     # still full: drop the soonest-to-expire
            del _validated[min(_validated, key=lambda k: _validated[k])]
    _validated[secret_hash] = now + _VALIDATED_TTL_S


def forget_validated_pat(secret_hash: str) -> None:
    """Drop a hash the moment it fails validation (revoked / expired / unknown / disabled owner).
    Called by auth.py::_pat_user on EVERY rejection path. This is what collapses the revocation
    window from the TTL to a single request; the TTL is only the backstop."""
    _validated.pop(secret_hash, None)


def _is_validated(secret_hash: str, now: float) -> bool:
    exp = _validated.get(secret_hash)
    if exp is None:
        return False
    if exp <= now:
        del _validated[secret_hash]
        return False
    return True


def _pat_hash_from_basic(header: str) -> str | None:
    """sha256-hex of the PAT-shaped half of a `Basic` credential, or None if there isn't one.

    Total and non-raising by construction: every parse failure returns None, which lands the caller
    on the ANON bucket (fail-toward-throttled). Does no I/O. The plaintext is never stored or logged
    — only the hash leaves this function, which is the same representation already at rest in the DB.
    Mirrors auth.py::_pat_user's extraction (either half may carry the token: some clients send it as
    the username with an empty password)."""
    if len(header) > _MAX_BASIC_HEADER:
        return None
    try:
        userpart, _, passpart = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except Exception:  # noqa: BLE001 — any undecodable header is simply "not a validated PAT"
        return None
    secret = passpart if PAT_SHAPE_RE.fullmatch(passpart) else userpart if PAT_SHAPE_RE.fullmatch(userpart) else None
    if secret is None:
        return None
    return hashlib.sha256(secret.encode()).hexdigest()


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: float, now: float) -> None:
        self.tokens = capacity
        self.updated = now


class AnonRateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket per (key, kind). Anonymous: key = client IP, kind = 'tile'|'download'|'read'.
    Authenticated (validated PAT): key = 'pat:<sha256>', kind = 'auth'. The per-minute setting is
    BOTH the sustained refill rate and the burst capacity. _allow() has no awaits, so under asyncio's
    single thread its read-modify-write is atomic — no lock needed."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._last_prune = time.monotonic()

    def _client_ip(self, request: Request) -> str:
        # Trust X-Forwarded-For ONLY behind a configured trusted proxy (else it is spoofable).
        if settings.rate_limit_trust_forwarded_for:
            xff = request.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _prune(self, now: float) -> None:
        if now - self._last_prune < _PRUNE_INTERVAL_S:
            return
        self._last_prune = now
        stale = [k for k, b in self._buckets.items() if now - b.updated > _IDLE_EVICT_S]
        for k in stale:
            del self._buckets[k]

    def _allow(self, key_id: str, kind: str, per_min: int, now: float) -> bool:
        self._prune(now)
        capacity = float(per_min)
        refill_per_sec = per_min / 60.0
        key = (key_id, kind)
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(capacity, now)
            self._buckets[key] = b
        else:
            b.tokens = min(capacity, b.tokens + (now - b.updated) * refill_per_sec)
            b.updated = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True
        return False

    async def dispatch(self, request: Request, call_next) -> Response:
        # Exempt: limiter disabled, CORS preflight, health probe. Everything else is CLASSIFIED
        # below, never exempted on scheme alone.
        auth = request.headers.get("authorization", "")
        if (
            not settings.rate_limit_enabled
            or request.method == "OPTIONS"
            or request.url.path in _EXEMPT_PATHS
        ):
            return await call_next(request)
        now = time.monotonic()
        path = request.url.path
        # BEARER. A VALIDATED session token is exempt (rate-limited upstream at the IdP, short-lived).
        # A Bearer the api has not proven — junk, forged, expired, or simply the first request with a
        # freshly refreshed token — is charged to its own per-IP class. NOT on the presence of the
        # header: that is how a junk Bearer used to skip the limiter altogether (module docstring).
        # Stub mode (dev only, refused outside development) validates no tokens, so nothing could
        # ever be recorded; it keeps the old exemption.
        if auth[:7].lower() == "bearer ":
            if settings.auth_mode == "stub" or _is_validated_bearer(auth, now):
                return await call_next(request)
            ip = self._client_ip(request)
            if not self._allow(ip, "bearer", settings.bearer_bootstrap_rate_per_min, now):
                log.info("anon_rate_limited", ip=ip, kind="bearer", path=path)
                return JSONResponse(
                    {"detail": "rate limit exceeded — slow down"},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
            return await call_next(request)
        # AUTHENTICATED TIER. A `Basic` header is the personal-access-token path (interop I-1) — the
        # desktop clients (ArcGIS Pro, QGIS), which do the heaviest legitimate work in the product.
        # It gets the elevated per-TOKEN bucket ONLY if the presented secret's hash is in the
        # validated cache, i.e. auth.py proved it live against the DB within the last 60s. NOT on the
        # presence of the header: that would let an attacker opt out of the anon throttle with a junk
        # Basic credential (see module docstring). Miss — junk, unknown, revoked, expired, or simply
        # the first request of a session — falls through to the anon branch below, exactly as before.
        if auth[:6].lower() == "basic ":
            token_hash = _pat_hash_from_basic(auth)
            if token_hash is not None and _is_validated(token_hash, now):
                if not self._allow("pat:" + token_hash, "auth", settings.auth_rate_per_min, now):
                    # Per-token, so this is one credential exhausting its OWN budget — never a
                    # noisy neighbour, and never reachable by an unauthenticated caller.
                    log.info("auth_rate_limited", token=token_hash[:12], path=path)
                    return JSONResponse(
                        {"detail": "rate limit exceeded — slow down"},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
                return await call_next(request)
            if token_hash is not None:
                # BOOTSTRAP. A PAT-SHAPED credential that is not (yet) in the validated cache: either
                # the genuine first request of a desktop session, or a probe. It cannot go to the
                # `read` bucket, because then a shared IP whose anon budget is already spent — a NAT'd
                # office, or an anon flood — could NEVER bootstrap: the one request that would prove
                # the token live is throttled before auth can vouch for it, and the client is locked
                # out of the elevated tier permanently. (Found by scripts/verify-rate-limit-tiering.sh
                # leg B, which drains the anon bucket before presenting a valid PAT.)
                #
                # So it gets its own small per-IP class. This TIGHTENS credential probing rather than
                # loosening it: a PAT-shaped guess now buys 60/min instead of the 120/min `read`
                # budget it shares today. Legitimate clients need ONE such request per idle gap —
                # every validated request refreshes the cache entry — so 60/min is ample for a whole
                # office behind one address, while 60 guesses/min against a 190-bit secret is noise.
                kind, per_min = "pat", settings.pat_bootstrap_rate_per_min
                ip = self._client_ip(request)
                if not self._allow(ip, kind, per_min, now):
                    log.info("anon_rate_limited", ip=ip, kind=kind, path=path)
                    return JSONResponse(
                        {"detail": "rate limit exceeded — slow down"},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
                return await call_next(request)
        # ANONYMOUS TIER — unchanged. Three cost classes by path: tiles (bursty, large bucket),
        # full-set /export downloads (the heaviest compute/egress lever — a tight bucket, Talus 33),
        # and everything else (reads). Malformed/other schemes land here too (fail-toward-throttled).
        # The basemap archive rides the TILE class: the pmtiles client reads it as a burst of
        # small range requests (header + directories + tile batches per pan), the same access
        # profile that earned tiles the larger bucket (decision-basemap-provisioning.md).
        if "/tiles/" in path or "/tiles-dyn/" in path or path.endswith("/basemap/archive"):
            kind, per_min = "tile", settings.anon_tile_rate_per_min
        elif path.endswith("/export"):
            kind, per_min = "download", settings.anon_download_rate_per_min
        else:
            kind, per_min = "read", settings.anon_read_rate_per_min
        ip = self._client_ip(request)
        if not self._allow(ip, kind, per_min, now):
            log.info("anon_rate_limited", ip=ip, kind=kind, path=path)
            return JSONResponse(
                {"detail": "rate limit exceeded — slow down"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
