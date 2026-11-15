from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class GeocoderConfig(BaseModel):
    """One external geocoding service Talus *consumes* (Talus 18c).

    Talus never builds or operates a geocoder (permanent non-goal,
    product-spec.md:137) — this is only the connection info for a locator the
    deployment already runs: a county/org ArcGIS REST GeocodeServer, or a
    self-hosted Nominatim. Set via the GEOCODERS env var as a JSON array; see
    .env.example for a worked example.
    """

    name: str  # unique id; surfaced to the client as the candidate `source`
    type: Literal["arcgis", "nominatim", "internal"]
    # Base service URL (GeocodeServer root, or the Nominatim base). None ONLY for type=internal
    # (Mode B, decision-own-data-geocoder.md) — an internal locator has no service URL; it geocodes
    # against a hosted layer in PostGIS. Internal locators are DB-configured only (never via the
    # GEOCODERS env var), so external env entries keep requiring a url exactly as before.
    url: str | None = None
    # SR requested from the service. The v1 contract is EPSG:4326 (no server-side
    # reprojection — the adapter trusts x=lon/y=lat), so leave this at 4326.
    out_sr: int = 4326
    token: str | None = None  # static ArcGIS token for a secured service (Q7); proxy-injected
    order: int = 0  # ordered-list fallback: lower = tried first (point locator before street)
    # Optional fixed search extent [xmin, ymin, xmax, ymax] in EPSG:4326 — results are
    # restricted to it (e.g. the county boundary), so a bare "100 Main" doesn't match
    # worldwide. A per-request viewport bbox overrides this when supplied.
    extent: list[float] | None = None
    # Mode B (type=internal) only: the hosted address-point layer + the ordered list of its
    # address fields concatenated for matching/display (decision-own-data-geocoder.md §3 F2).
    layer_id: str | None = None
    address_fields: list[str] | None = None
    # Mode B only: the ordered locality fields (city, ZIP …) — tiebreak + label, never scored or
    # indexed (decision-batch-geocoding-county-scale.md F3a; V107). Empty = none.
    locality_fields: list[str] | None = None

    @field_validator("extent")
    @classmethod
    def _check_extent(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 4:
            raise ValueError("extent must be [xmin, ymin, xmax, ymax] (4 numbers, EPSG:4326)")
        return v

    @model_validator(mode="after")
    def _url_required_for_external(self) -> "GeocoderConfig":
        """Keep the pre-Mode-B fail-fast guarantee: an external (http-transport) entry without a
        url refuses to LOAD — at startup for env GEOCODERS entries — instead of surfacing later as
        an AttributeError inside the request walk (which is not in the fail-soft catch set)."""
        if self.type != "internal" and not self.url:
            raise ValueError(f"geocoder '{self.name}': url is required for type '{self.type}'")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://talus:changeme@localhost:5432/talusgis"
    # S-1a (decision-s1-least-privilege-db-role.md): the API's MAIN pool connects as a
    # least-privilege role (talus_app) that cannot tamper with the audit trail. The rare runtime
    # DDL (layer provisioning, view layers, geocoder index) runs on a separate OWNER connection.
    # Unset => fall back to database_url (pre-cutover, where both are the owner role).
    owner_database_url: str | None = None
    auth_mode: str = "keycloak"  # fail-closed default; dev opts into 'stub' via .env

    # Optional extension modules (decision-story-maps.md §4 / decision-paid-module-home-entitlement.md
    # Part A): a comma-separated list of import paths, each exposing a module-level `router`
    # (APIRouter), mounted alongside the product routers at startup. Core deliberately knows
    # NOTHING about what the modules are — this is the generic seam that lets paid/ code mount
    # without core ever importing from paid/ (the one-way rule, paid/README.md). Empty (the
    # default) => no extra code path exists at all, which is the open-core promise held
    # structurally: a free install has zero paid-aware branches.
    talus_extra_modules: str = ""

    # Public / anonymous access (Talus 23) — the deploy-posture MASTER switch. Default OFF: an
    # instance is not internet-facing unless an operator deliberately enables it. When OFF, the
    # "Public Access" share tier is rejected server-side (no object can be made public) and the
    # anonymous read path is inert. This is a DEPLOYMENT decision (is this instance internet-
    # exposed?), not an admin web-form toggle — hence config/env, not org_settings. Turn on with
    # PUBLIC_ANON_ACCESS_ENABLED=true.
    public_anon_access_enabled: bool = False

    # Open Data (Talus 33) — the publishing organization's name, used as the `publisher` in the
    # DCAT-US feed + schema.org JSON-LD (data.gov federation shows it). Set per deployment to the
    # municipality's name; the default is a harmless placeholder so the feed is well-formed out of box.
    open_data_publisher_name: str = "Talus GIS"

    # Rate limiting (Talus 23) — applies ONLY to anonymous (token-less) requests, per client IP;
    # authenticated traffic (field crew etc.) is never limited. Tiles are chatty (a single public
    # map pan fires many), so they get a larger budget than the heavier feature/metadata reads.
    # Defaults suit a typical public-facing municipal deployment; raise them for a high-traffic
    # public site (these are the documented "for larger orgs" knobs). A reverse proxy is the
    # production-grade defense — this app limiter is the turnkey default. Token-bucket: the value is
    # both the per-minute refill AND the burst capacity.
    rate_limit_enabled: bool = True
    anon_read_rate_per_min: int = 120
    anon_tile_rate_per_min: int = 600
    # Anon full-set /export downloads (Talus 33 Open Data) — the heaviest compute/egress per request,
    # so a deliberately tight bucket, distinct from the read class. Raise for a busy open-data site.
    anon_download_rate_per_min: int = 10
    # AUTHENTICATED tier (decision-desktop-rate-limit-tiering.md). A `Basic` personal-access-token
    # request whose secret was VALIDATED against the DB downstream gets this per-TOKEN ceiling
    # instead of the per-IP anon bucket — desktop clients (ArcGIS Pro, QGIS) do the heaviest
    # legitimate work in the product and were being throttled to 2 features/sec on the anon 120/min.
    # 3000/min (50/sec) sits comfortably above the 27/sec measured on the rig, so it never bites real
    # work, while still bounding egress: "authenticated" must not mean "unlimited" (a compromised PAT
    # is a real egress vector). Per-token keying means IP rotation cannot amplify it. Lower it if you
    # want a tighter bound on what one leaked token can pull.
    auth_rate_per_min: int = 3000
    # BOOTSTRAP tier: a PAT-SHAPED `Basic` credential not yet in the validated cache — the genuine
    # first request of a desktop session, or a probe. Its own small per-IP bucket, so a shared address
    # whose anon budget is spent can still get one request through to PROVE the token live (without
    # this, a NAT'd office or an anon flood locks desktop clients out of the authenticated tier
    # entirely). Note this TIGHTENS credential probing: a PAT-shaped guess buys 60/min here instead of
    # the 120/min anon read budget it shares today. 60 guesses/min against a 190-bit secret is noise.
    pat_bootstrap_rate_per_min: int = 60
    # A `Bearer` token the api has NOT (yet) validated: per-IP, its own class. A validated session is
    # exempt as before; this bounds everything else that merely says "Bearer" (once-over A7). Its own
    # class for the reason the PAT bootstrap has one — an address whose anonymous budget is spent must
    # still get a fresh session token through to be proven. Sized for an office behind one address:
    # every browser re-presents an unproven token after each ~5-minute refresh, a page load being a
    # burst of a few dozen requests before the first one validates.
    bearer_bootstrap_rate_per_min: int = 600
    # Only trust X-Forwarded-For when a trusted reverse proxy sets it (else a client could spoof its
    # IP to evade the limit). Default False = use the direct peer IP (correct with no proxy).
    rate_limit_trust_forwarded_for: bool = False

    # Observability / logging.
    environment: str = "development"        # development | staging | production
    service_version: str = "0.1.0"          # surfaced on every log line
    # None => derive from environment (JSON unless development). Override via LOG_JSON_FORMAT.
    log_json_format: bool | None = None

    # Interactive API docs exposure (Talus 32 — web-edge hardening). FastAPI serves /docs, /redoc,
    # and /openapi.json UNAUTHENTICATED by default — a reconnaissance surface (the analog of
    # disabling a services directory in production). None => derive from environment: ON in
    # dev/test, OFF otherwise (staging/production). Override via EXPOSE_API_DOCS. When OFF, main.py
    # builds FastAPI with docs_url=redoc_url=openapi_url=None so all three 404. A service-account
    # developer who needs the machine-readable contract flips this on in a controlled environment
    # or reads it from a dev/staging instance (a running SA never needs the live spec).
    expose_api_docs: bool | None = None

    # Keycloak: only consulted when auth_mode == "keycloak".
    keycloak_issuer: str = "http://keycloak:8080/realms/talus"
    keycloak_audience: str = "talus-api"
    keycloak_jwks_url: str = "http://keycloak:8080/realms/talus/protocol/openid-connect/certs"
    keycloak_jwks_cache_ttl_seconds: int = 3600

    # Keycloak Admin API (Talus 19 c4) — server-side provisioning of service-account clients.
    # The portal authenticates AS the `talus-provisioner` confidential client (client-
    # credentials) and calls the Admin API to create / rotate / disable SA clients and assign
    # their realm roles. Least-privilege realm-management roles are granted to it by
    # scripts/bootstrap-19c-provisioner.sh. Internal Docker network; never browser-exposed.
    keycloak_admin_base_url: str = "http://keycloak:8080"
    keycloak_realm: str = "talus"
    keycloak_provisioner_client_id: str = "talus-provisioner"
    # Read from .env via the TALUS_-prefixed name the bootstrap script also uses (one secret,
    # one source). Empty => the SA-admin endpoints fail closed (502) rather than mis-provision.
    keycloak_provisioner_client_secret: str = Field(
        default="", validation_alias="TALUS_PROVISIONER_CLIENT_SECRET"
    )
    # The shared client scope (oidc-audience-mapper aud=talus-api) the provisioner attaches to
    # each minted SA client so its token passes auth.py's audience check (replaces the inline
    # per-client mapper). Created by bootstrap-19c-provisioner.sh.
    keycloak_sa_audience_scope: str = "talus-api-audience"
    # The realm role a READ-WRITE service account gets — never publisher/admin (no god tokens;
    # decision-19 Q4/Q8). A read-only SA gets NO edit role.
    keycloak_data_editor_role: str = "talus-data-editor"

    # The RESOURCE-SERVER client (this api's own audience), used for exactly one thing:
    # POST /me/password re-authenticates the caller with a direct-access grant before changing
    # their password (decision-first-ten-minutes.md F3). Deliberately NOT the provisioner —
    # widening that privileged admin client's grant surface to accept passwords is the
    # expensive option. `talus-api` is publicClient:false, standardFlowEnabled:false,
    # serviceAccountsEnabled:false, no redirect URIs: the secret confers the password-grant
    # capability and nothing else, and it is strictly less powerful than the provisioner
    # secret this process already holds. Empty => self-service change fails closed (503).
    keycloak_api_client_id: str = "talus-api"
    keycloak_api_client_secret: str = Field(
        default="", validation_alias="TALUS_API_CLIENT_SECRET"
    )

    # CORS — browser origins allowed to call the API. Comma-separated env
    # (CORS_ALLOW_ORIGINS); production can add its origin without a code change.
    cors_allow_origins: str = "http://localhost:5173"

    # pg_tileserv base URL (internal Docker network). The tile-proxy route fetches
    # MVT from here; pg_tileserv is never published to the host.
    tileserv_url: str = "http://pg_tileserv:7800"
    # Admin → System's certificate probe: the api opens one
    # TLS handshake to the edge INSIDE the compose network, with SNI = the portal's own hostname, and reads the
    # served chain's expiry. TALUS_HOSTNAME is the key install.sh / firstboot already write for Caddy; the
    # compose file now hands the same value to the api. Empty (a hand-run stack without the edge profile) =
    # the probe is not shown. Never a caller input (no SSRF surface).
    talus_hostname: str = ""
    edge_tls_target: str = "caddy:443"

    # OpenFGA (Layer 7b — the per-layer access key). Internal Docker network; the
    # store + model ids are written into .env by scripts/load-openfga-model.sh.
    # An empty store/model id means the model-load step hasn't run — authz fails
    # closed (503) rather than silently allowing.
    openfga_api_url: str = "http://openfga:8080"
    openfga_store_id: str = ""
    openfga_model_id: str = ""

    # Storage seam root (decision-object-store-build.md B3): the volume-mounted directory the
    # filesystem backend lives under. The three bucket-name settings below become its subtrees
    # (/storage/talus-media etc.); object keys map 1:1 to relative paths — the deliberate
    # migration boundary (any later S3 move is a plain sync, no key rewriting).
    storage_root: str = "/storage"
    # The other services' ERROR/WARN log files (decision-ai-substrate.md §13, item 6): the `servicelogs` volume,
    # mounted READ-ONLY here, with a directory per service (caddy/ postgres/ keycloak/) made by the logs-init
    # one-shot. app/service_logs.py reads the tail; never the Docker socket.
    service_logs_dir: str = "/servicelogs"
    # The media subtree name under storage_root (Talus 16; kept as a "bucket" name so an
    # off-host S3 mirror of the same keys stays 1:1). The api streams photo bytes through
    # the /media proxy — the store is never browser-exposed.
    media_bucket: str = "talus-media"
    # Feature attachments (decision-attachments.md A-1). The bytes live in the media_bucket under an
    # `attachments/` prefix. Two fences (the third is the content-type allowlist + magic-byte sniff in
    # app/attachments.py): a per-file byte cap (over → 413) and a per-feature count cap (over → 409). The
    # real bloat guard is the byte cap + allowlist (no video); the count cap stops runaway/accidental loops.
    # The ceiling on ANY request body, in MiB (app/body_limit.py; once-over A8). Doors whose own
    # documented cap is larger (raster, import, basemap and field-project uploads, the KML table)
    # are listed there with that cap; everything else — every JSON door, every Esri form — is
    # refused with 413 past this, before the body is buffered.
    request_body_max_mb: int = 32
    attachment_max_bytes: int = 25 * 1024 * 1024   # 25 MB/attachment
    attachment_count_cap: int = 50                 # attachments per feature
    # Field projects (V085, decision-field-projects.md F5): the .qgz upload ceiling. Layer COUNT
    # never grows a project (layers are URL references — KBs of XML each); only data embedded in
    # the zip does (an offline raster basemap, a hand-packaged gpkg). 100 MB covers any live
    # project with generous headroom; an org shipping big offline basemaps raises this, not code.
    field_project_max_bytes: int = 100 * 1024 * 1024
    # Raster store & serve (V045+): the subtree holding uploaded source COGs (+ thumbnails);
    # rio-tiler range-reads them straight off the volume. No pre-rendered pyramid (dynamic
    # tiling), so this subtree is ALL source-of-truth user data.
    raster_bucket: str = "talus-rasters"
    # Hard ceiling on a single raster upload (fail-closed on absurd input). Generous so citywide ortho
    # COGs fit; the upload streams THROUGH the api to the store (internal-only, never
    # browser-exposed), so this also bounds per-ingest disk. Over the cap -> 413.
    raster_max_upload_mb: int = 5120
    # 9d bulk-import door (decision-9d-bulk-import.md §3): jailed per-job staging + honest
    # server-side ceilings — B7 probes whatever we claim here. Staging dir empty => a
    # talus-import-jobs dir under the container tempdir.
    import_staging_dir: str = ""
    import_max_upload_mb: int = 2048     # staged bytes as received -> 413 past this
    import_max_unpacked_mb: int = 4096   # archive decompressed budget (zip-bomb fence) -> 413
    import_max_features: int = 2_000_000  # worker-enforced per-job row ceiling
    import_error_sample_max: int = 50    # bounded error sample on the job row (county-scale honesty)
    # Raster export / clip-download (decision-raster-export.md): the megapixel ceiling on a single
    # /export. A drawn bbox read at native resolution over this many output pixels is REFUSED (413,
    # reporting the native dims) unless the caller opts into downsampling via &maxDim. A clip reads
    # only the overviews it needs, so this bounds the decode/write/egress cost of one synchronous
    # export. 64 MP ~= an 8000x8000 GeoTIFF — generous for a field-area clip, well short of a citywide
    # ortho (which belongs to the deferred async/mosaic path).
    raster_max_export_megapixels: int = 64

    # Vector export (layer_query.export_features): a ceiling on what one export may hold in memory
    # while it is built (once-over 2026-11-15 K2). The row cap (250,000) bounds rows, not bytes; a layer
    # of large polygons could make one export several gigabytes, and the door serves public layers
    # to anyone. Counted while the rows arrive; past it the export answers 413 and asks for a filter.
    export_max_bytes: int = 256 * 1024 * 1024

    # Secrets-at-rest (decision-geocoder-token-locators.md §3 F1/F1a) — the ONE Fernet key
    # app/secrets.py encrypts DB-stored credentials with (first consumer: geocoder tokens, V073;
    # next: T43 off-host backup creds). Deliberately GENERIC, not per-feature: one key, one .env
    # line, one DR story. Unset is fine until an admin stores a secret (air-gapped installs never
    # need it) — the write path then 422s with the generation one-liner. Key + ciphertext are the
    # secret only TOGETHER: a DB dump/backup alone is safe; losing the key = re-enter secrets.
    talus_encryption_key: str | None = None

    # Geocoding (Talus 18c) — external locator services Talus consumes (never builds).
    # JSON array in GEOCODERS, ordered by `order`. Empty => only the locators kept in the portal
    # (platform.geocoders) are used; with none there either, the /geocode proxy returns empty
    # results and the map still works (fail-soft, §6/§8).
    # NoDecode + the validator below own the JSON parse: `GEOCODERS=` (empty/whitespace, a
    # natural thing for an operator to leave in .env) means "none", instead of the settings
    # source dying on json.loads("") before any validator can run (A5 defect #3, 2026-07-27).
    geocoders: Annotated[list[GeocoderConfig], NoDecode] = []

    @field_validator("geocoders", mode="before")
    @classmethod
    def _parse_geocoders(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return []
            import json
            return json.loads(v)
        return v

    # Self-maintenance & housekeeping (Talus 39) — the app-internal scheduler that auto-expires CRUFT
    # (decision-housekeeping.md). Config-driven, not an admin web form (v1): these are sensible knobs an
    # operator rarely tunes. The scheduler runs INSIDE the api process (a lifespan-managed asyncio loop),
    # guarded by a Postgres advisory lock so exactly one replica runs a job even at N replicas.
    #   - maintenance_enabled: deploy switch (default ON — self-tidying is the default). OFF disables the
    #     in-app loop entirely (e.g. during a migration window, or to run jobs out-of-band).
    #   - tick_seconds: how often the loop wakes to check what's due (cheap when nothing is due).
    #   - run_interval_seconds: how often each job is allowed to run (daily). The DB maintenance_runs
    #     history is the authority on "is this job due?", so cross-replica there's no double-run.
    #   - *_retention_days / *_grace_days: the per-job N-day cruft thresholds (HARD FENCE — cruft only).
    maintenance_enabled: bool = True
    maintenance_tick_seconds: int = 3600          # 1h: check-due cadence (not the run cadence)
    maintenance_run_interval_seconds: int = 86400  # 24h: a given job runs at most once per this window
    # Job (a): prune platform.applied_op_keys (V036) older than N days — transient offline-replay window
    # rows, never authoritative. The V036 migration's own TODO suggested ~30 days.
    maintenance_idempotency_key_retention_days: int = 30
    # Job (b): delete an orphaned media blob (no live media.photos.object_key) only if it is ALSO older
    # than this grace window — so an in-flight upload whose DB row hasn't committed yet is never swept.
    maintenance_orphaned_media_grace_days: int = 7

    # Batch geocoding async worker (decision-batch-geocoding.md F5). A SEPARATE lifespan loop from
    # maintenance (own advisory-lock key; it legitimately writes features, outside the cruft fence).
    #   - enabled: deploy switch (default ON). OFF pauses batch-geocode processing (jobs still enqueue).
    #   - tick_seconds: how often the worker wakes to claim the next queued job (short — jobs should
    #     start promptly; a tick when the queue is empty is one cheap indexed SELECT).
    geocode_worker_enabled: bool = True
    geocode_worker_tick_seconds: int = 3
    # 9d bulk-import worker (decision-9d-bulk-import.md §4) — same lifecycle switches.
    import_worker_enabled: bool = True
    import_worker_tick_seconds: int = 3
    # SQL-source pull worker (decision-sql-source-pull.md §3) — the fourth job class, same switches.
    sql_pull_worker_enabled: bool = True
    sql_pull_worker_tick_seconds: int = 3
    # The recent-warnings persister (V114, app/recent_warnings_store.py): copies the in-process WARNING+
    # ring to platform.recent_warnings so a restart does not empty the evidence. Best effort; its own
    # failures log below WARNING (a WARNING would land in the ring it drains). OFF keeps memory only.
    recent_warnings_persist_enabled: bool = True
    recent_warnings_flush_seconds: int = 5

    # Disaster recovery — backup / retention / restore (Talus 40, decision-dr-backup-restore.md). A second
    # consumer of the T39 scheduler seam: a scheduled_backup job (pg_dump -Fc -> object storage + an
    # incremental append-only copy of the media bucket(s)) + a prune_old_backups retention job. The api
    # shells out to pg_dump (postgresql-client-16 in the image); restore is a documented runbook, not a button.
    #   - backup_enabled: register the DR jobs at all (default ON). Independent of maintenance_enabled, which
    #     is the scheduler-loop master switch — but the loop must be running for SCHEDULED backups (manual
    #     "Back up now" works regardless).
    #   - *_interval_seconds: how often a scheduled backup runs (daily).
    #   - retention_days / retention_min_keep: prune dump artifacts older than N days, but NEVER below the M
    #     most-recent (a non-zero floor so a misconfig can't prune the only backup away). HARD FENCE: old
    #     backups only — never live data, never the audit log.
    backup_enabled: bool = True
    backup_interval_seconds: int = 86400          # 24h: scheduled-backup cadence
    backup_retention_days: int = 30               # prune dump artifacts older than this...
    backup_retention_min_keep: int = 7            # ...but always keep at least this many most-recent (the floor)
    # The backups subtree name (local default) / bucket name (off-host S3) — see the off-host seam below.
    backup_bucket: str = "talus-backups"
    # Source object buckets whose blobs the object-store half backs up. Comma-separated; parsed by
    # backup_object_buckets_list. EMPTY (the default) derives [media_bucket, raster_bucket], so the
    # backup always follows the ACTUAL bucket names — a hardcoded default here silently missed the
    # rasters on any box whose RASTER_BUCKET diverged (the talus-raster/talus-rasters papercut,
    # found 2026-08-23: those boxes' raster backups were empty). Rasters are included because with
    # dynamic tiling there is no regenerable pyramid — every object is irreplaceable user data.
    backup_object_buckets: str = ""
    # Off-host backup target (the honest-DR seam) — the ENV DEFAULTS; an admin-set V090 override
    # (Admin -> Backups, decision-backup-destination.md) supersedes the whole trio at runtime.
    # EMPTY => backups land on the local storage
    # volume under backup_bucket (a same-host copy, honestly labeled — not DR). Set ALL THREE to
    # ship backups off-host (AWS S3 / Backblaze B2 / a NAS's S3 door) with NO code change —
    # same-host backups don't survive host loss; off-host is the real protection. A PARTIAL
    # config raises loudly (storage.build_backup_storage) rather than silently writing local.
    backup_s3_endpoint: str = ""
    backup_s3_access_key: str = ""
    backup_s3_secret_key: str = ""

    # Client-side backup encryption (Talus 40.1, decision-dr-encryption.md). Comma-separated `age` PUBLIC
    # keys (age1…). EMPTY => OFF (artifacts stored plaintext, as T40 ships). Set => every artifact (dump +
    # media blobs) is `age`-encrypted to ALL listed recipients BEFORE upload, so an off-host/cloud target
    # never holds plaintext. ASYMMETRIC: the api holds only the public key and CANNOT decrypt its own
    # backups; the matching PRIVATE key is kept OFF-HOST and used only at restore. List ≥2 recipients (a
    # primary + a break-glass key held by a second custodian) so a single lost key isn't lost backups.
    backup_age_recipients: str = ""

    # Full-system DR (Talus 42, decision-dr-completeness.md). Talus is a THREE-key system — Postgres (data) +
    # Keycloak (identity) + OpenFGA (authz), each its own DB. A backup must dump ALL THREE or a restore loses
    # logins + the entire sharing graph. These are the read DSNs for the OTHER two databases; compose defaults
    # them to the internal keycloak-postgres / openfga-postgres so full backups are turnkey. EMPTY => that DB
    # is SKIPPED (a deployment running identity/authz externally) — an honest "talusgis + media only" backup,
    # NOT an error. The api gains DB-read creds here; that's incremental (it already provisions Keycloak users
    # and makes every OpenFGA decision), and a least-privilege read-only role is the documented hardening.
    backup_keycloak_db_url: str = ""
    backup_openfga_db_url: str = ""

    # "Lifeboat" backup — include the literal `.env` (Talus 45, decision-dr-env-lifeboat.md). T42 set the
    # stance "`.env` is NEVER backed up" (it holds live, reusable infra creds — the restore master key: OpenFGA
    # store/model IDs + every service secret). This REVERSES that CONDITIONALLY: the `.env` is written into a
    # backup ONLY `age`-encrypted, and ONLY when backup_include_env AND encryption (≥1 recipient) are BOTH on
    # (the fail-closed runtime AND — see backup.run_backup). The win: an operator then safeguards ONE off-host
    # thing (the `age` PRIVATE key, which opens everything) instead of two (the key AND the `.env`); and the
    # `.env` + DB dumps share one snapshot, so the OpenFGA IDs match the restored tuples. The PRIVATE key is
    # NEVER backed up (unchanged from 40.1), so encryption is real even with `.env` inside.
    #   - backup_include_env: the env DEFAULT for the include-`.env` toggle (overridable per backup_settings,
    #     COALESCE). Default OFF — opt-in, gated on encryption.
    #   - backup_env_path: where the api reads the literal `.env` from — a read-only compose mount
    #     (./.env:/secrets/.env:ro). Reconstructing from settings is lossy (misses comments/unmodelled vars),
    #     so the lifeboat is the LITERAL file.
    backup_include_env: bool = False
    backup_env_path: str = "/secrets/.env"

    @model_validator(mode="after")
    def _derive_log_format(self) -> "Settings":
        if self.log_json_format is None:
            self.log_json_format = self.environment.lower() not in ("development", "dev", "local")
        return self

    @model_validator(mode="after")
    def _derive_docs_exposure(self) -> "Settings":
        # Default the interactive docs ON only in non-prod environments (same set as the stub-auth
        # guard below). Staging/production => OFF, secure-by-default. An explicit EXPOSE_API_DOCS
        # always wins (e.g. flip on in a controlled staging box to read the contract).
        if self.expose_api_docs is None:
            self.expose_api_docs = self.environment.lower() in (
                "development",
                "dev",
                "local",
                "test",
            )
        return self

    @model_validator(mode="after")
    def _forbid_stub_outside_dev(self) -> "Settings":
        """Fail closed: stub auth authorizes every request, so it must never be the
        mode on a non-dev deployment. Refuses to start rather than ship open.
        (Relies on `environment` being set; pairs with keeping AUTH_MODE explicit.)
        """
        if self.auth_mode == "stub" and self.environment.lower() not in (
            "development",
            "dev",
            "local",
            "test",
        ):
            raise ValueError(
                "auth_mode='stub' authorizes every request and is forbidden outside "
                f"development (environment={self.environment!r}); set AUTH_MODE=keycloak"
            )
        return self

    @model_validator(mode="after")
    def _forbid_dev_posture_in_prod(self) -> "Settings":
        """C8 (partial — see the ★ note): a staging/production deployment must not run with a
        dev-known client secret. Refuses to start.

        Fail-closed and at STARTUP, mirroring `_forbid_stub_outside_dev` above: a published
        placeholder secret is silently fine until the day it is catastrophic, and a stack that
        refuses to boot is strictly safer than one that boots forgeable.

        ★ C8 also names "the `talus-dev` realm" and "a `localhost` issuer". Those legs are NOT
        implemented, deliberately:
          * the `localhost` issuer is correct for a single-box install (generate-secrets.sh writes
            it for every plain install), and install.sh already warns loudly about localhost
            reachability on the --edge path;
          * the realm leg was unimplementable while the product SHIPPED a realm named `talus-dev`
            (it did until 2026-09-09; gating on it refused every clean install). The realm is now
            `talus` (decision-realm-rename.md), so the leg COULD fire only on an install that
            predates the rename — a staleness signal, not a dev-posture one — so it stays out.
            Realm agreement between this config, Keycloak and the SPA is asserted by
            inv33-realm-consistency instead.
        Gating on either would refuse real installs while catching nothing — and the operator's
        only escape would be ENVIRONMENT=development, which silently un-gates the interactive docs,
        `_forbid_stub_outside_dev`, and this check. A guard whose failure mode is "turn off three
        controls" is worse than no guard.

        The secret leg has no such problem: generate-secrets.sh mints random hex, so it never fires
        on a real install — it fires exactly when someone has copied .env.example into place, which
        is the mistake worth catching.
        """
        if self.environment.lower() in ("development", "dev", "local", "test"):
            return self

        # `not-for-production` is the repo-wide marker for a checked-in placeholder (.env.example);
        # verify-clean-install.sh greps for the same string, so the two cannot drift apart on what
        # "dev secret" means.
        problems: list[str] = []
        for label, value in (
            ("TALUS_API_CLIENT_SECRET", self.keycloak_api_client_secret),
            ("TALUS_PROVISIONER_CLIENT_SECRET", self.keycloak_provisioner_client_secret),
        ):
            if value and "not-for-production" in value:
                problems.append(f"{label} is the published placeholder value from .env.example")

        if problems:
            raise ValueError(
                f"refusing to start: environment={self.environment!r} but "
                + "; ".join(problems)
                + ". These values are published in this repo and are known to anyone who has read "
                "it. Generate real secrets with scripts/generate-secrets.sh."
            )
        return self

    @property
    def cors_allow_origins_list(self) -> list[str]:
        """CORS_ALLOW_ORIGINS parsed into a list (comma-separated, trimmed)."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def backup_object_buckets_list(self) -> list[str]:
        """The source buckets the object-store half of a backup copies (and the storage report walks).
        BACKUP_OBJECT_BUCKETS parsed comma-separated when set; EMPTY derives [media_bucket,
        raster_bucket] so the list always tracks the real bucket names (see the field comment)."""
        explicit = [b.strip() for b in self.backup_object_buckets.split(",") if b.strip()]
        return explicit if explicit else [self.media_bucket, self.raster_bucket]

    @property
    def backup_target_is_offhost(self) -> bool:
        """True when an explicit off-host BACKUP_S3_ENDPOINT is configured (drives the honest-DR label
        shown in the catalog/panel: same-host backups don't survive host loss)."""
        return bool(self.backup_s3_endpoint)

    @property
    def backup_age_recipients_list(self) -> list[str]:
        """BACKUP_AGE_RECIPIENTS parsed into a list of age public keys (comma-separated, trimmed)."""
        return [r.strip() for r in self.backup_age_recipients.split(",") if r.strip()]

    @property
    def backup_encryption_enabled(self) -> bool:
        """True when at least one age recipient is configured => artifacts are encrypted before upload."""
        return bool(self.backup_age_recipients_list)


settings = Settings()
