# Scripting against the Talus API

Talus exposes a **token-authenticated REST API** so you can read and edit your GIS data
from your *own* environment — a Jupyter notebook, a Python script, a cron job — without a
browser login and without hardcoding a person's password. Talus does **not** run your code;
it authenticates your calls to the same stateless API the portal uses. Bring your own
compute.

The public, versioned contract is served under **`/api/v1`**. The interactive API reference —
Swagger UI at **`/docs`** and the OpenAPI schema at **`/openapi.json`** — is **off on an installed
server** (it is unauthenticated and describes every route, so production keeps it closed), and the
web edge does not forward those two addresses. This release has no setting that turns it on for an
installed server; a running script never needs it.

> The surface is an OGC-API-Features-*style* layer-query + feature-CRUD REST API (it is
> Talus's own shape, not strict OGC API Features — the standards-exact OGC API — Features and
> Esri FeatureServer doors are documented in `connect-desktop.md`). `/health` is unversioned
> (liveness).

---

## 1. Get a service account

Ask your Talus administrator for a **service account** — a non-interactive machine identity.
You receive a `client_id` and a `client_secret`. The administrator scopes the account to one or
more **groups** (Admin → Service accounts), and it sees exactly the layers shared to those groups —
read-only, or read-write if the administrator also grants it the editor role — and nothing else. A
service account:

- is **scoped** — it can only touch layers shared to its groups (everything else is denied), and it
  can never hold publisher or administrator rights;
- is **attributable** — its edits are recorded in the audit trail as the service account,
  not as a person;
- is **revocable** — the admin can disable or rotate it at any time.

Keep the `client_secret` like a password. Do not commit it to source control.

## 2. Get an access token (OAuth client-credentials)

Exchange the `client_id` + `client_secret` for a short-lived bearer token at your Keycloak
realm's token endpoint:

```bash
curl -s -X POST "$KEYCLOAK/realms/$REALM/protocol/openid-connect/token" \
  -d grant_type=client_credentials \
  -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" | jq -r .access_token
```

The token is short-lived (minutes). Scripts hold the *secret* and fetch a fresh token as
needed — never store long-lived bearer tokens.

## 3. Call the API

Send the token as `Authorization: Bearer <token>` against `/api/v1`:

```python
import requests

KEYCLOAK = "https://<sign-in-host>"   # the identity server — your deployment's sign-in address
REALM    = "talus"                     # the Talus realm
TALUS    = "https://<portal-host>"     # the portal / API base URL
CLIENT_ID, CLIENT_SECRET = "my-service-account", "..."   # from your admin

def token() -> str:
    r = requests.post(
        f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]

hdr = {"Authorization": f"Bearer {token()}"}

# Which layers can this account see? Layer ids are server-generated (tal_…) — always
# discover them here rather than hardcoding.
layers = requests.get(f"{TALUS}/api/v1/me/layers", headers=hdr, timeout=10).json()
layer_id = layers["layers"][0]["id"]   # e.g. "tal_1a2b3c4d"

# Read features (server-side filter / sort / paginate). Filters accept real columns and
# owner-defined fields alike, by bare name.
feats = requests.get(
    f"{TALUS}/api/v1/layers/{layer_id}/features",
    headers=hdr,
    params={
        "bbox": "-98.55,29.35,-98.35,29.55",  # lon_min,lat_min,lon_max,lat_max (EPSG:4326)
        "filter": "condition_code:eq:poor",   # repeatable 'field:op:value'; op in eq/ne/gt/gte/lt/lte/in/notin/like/ilike
        "limit": 1000,
        "offset": 0,
    },
    timeout=30,
).json()

# Edit (requires a read-write grant). Writes are attributed to this service account.
# `notes` is a built-in field; owner-defined attribute values ride in "props". Point layers
# take lon/lat; line and polygon layers take a GeoJSON "geometry" instead.
requests.post(
    f"{TALUS}/api/v1/layers/{layer_id}/features",
    headers={**hdr, "Content-Type": "application/json"},
    json={"lon": -98.49, "lat": 29.42, "notes": "added via API",
          "props": {"condition_code": "good"}},
    timeout=10,
).raise_for_status()
```

## 4. Useful endpoints

| Method & path | Purpose |
|---|---|
| `GET /api/v1/me/layers` | Layers this account can view, with per-layer view/edit flags |
| `GET /api/v1/layers/{id}/columns` | A layer's full field schema (name / type / coded domain) |
| `GET /api/v1/layers/{id}/features` | Query features — `bbox`, repeatable `filter`, `limit`, `offset` |
| `POST/PATCH/DELETE /api/v1/layers/{id}/features[/{fid}]` | Create / edit / delete features on a hosted layer |

The table above is what this guide covers. The full list, with request and response schemas, is the
OpenAPI document, which an installed server does not serve (see the top of this guide).

## 5. What you can and can't do

- **Scope is least-privilege.** A read-only account gets `403` on any write; any account gets
  `403` on a layer it wasn't granted. There are no "god" tokens.
- **Errors are fail-closed.** An unmapped, expired, or revoked token is rejected (`401`); a
  request beyond your grant is denied (`403`), and the authorization service being
  unreachable denies (`503`) — never a silent allow.
- **No server-side code.** Talus authenticates your calls; it does not host notebooks, run
  jobs, or schedule tasks. The compute is yours.
