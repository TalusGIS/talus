"""HTTP security response headers (Talus 32 — web-edge hardening).

A pure-ASGI middleware that stamps a baseline of security headers on every HTTP
response: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and a
strict `Content-Security-Policy`. This is the APP-LEVEL baseline (defense-in-
depth) — so a deployment that runs the bare api WITHOUT the bundled reverse proxy
is still hardened. The shipped Caddy front (compose `edge` profile) sets the full
set INCLUDING HSTS + the MapLibre-tuned CSP on the SPA; see proxy/Caddyfile and
docs/security-overview.md.

Deliberately pure-ASGI (a `send`-wrapper that mutates the `http.response.start`
headers), NOT `BaseHTTPMiddleware` — it only needs to touch response headers, and
the send-wrapper never buffers the streaming tile/media/export response bodies a
`BaseHTTPMiddleware` body-pass can stall.

HSTS is intentionally NOT set here: `Strict-Transport-Security` must only ever go
out over a TLS origin (the proxy), never from the plain-HTTP dev app — else it
would wrongly pin browsers to HTTPS for localhost.

The CSP here is the STRICT, JSON-appropriate policy: API responses reference no
sub-resources, so `default-src 'none'` is safe and maximally locked down. The
interactive docs pages (`/docs`, `/redoc`) — dev-only, gated off in prod (F4) —
are exempt from the CSP because Swagger UI / ReDoc load CDN assets + inline
scripts a `default-src 'none'` policy would blank; the other three headers still
apply to them.
"""

# Applied to every response, in ASGI raw form (lowercase b"name", b"value").
_BASE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)

# Strict CSP for API/JSON responses (they load nothing). `frame-ancestors 'none'`
# is the modern superset of `X-Frame-Options: DENY` (the latter kept for old UAs).
_CSP_HEADER = b"content-security-policy"
_CSP = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

# The interactive docs UIs render HTML that pulls Swagger/ReDoc assets + inline
# scripts — a `default-src 'none'` CSP blanks them. Dev-only (prod gates them off),
# so simply exempt them from the CSP; the other headers still apply.
_CSP_EXEMPT_PATHS = frozenset({"/docs", "/redoc"})


class SecurityHeadersMiddleware:
    """Pure-ASGI: append the security-header baseline to every HTTP response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        send_csp = scope.get("path") not in _CSP_EXEMPT_PATHS

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend(_BASE_HEADERS)
                if send_csp:
                    headers.append((_CSP_HEADER, _CSP))
            await send(message)

        await self.app(scope, receive, send_with_headers)
