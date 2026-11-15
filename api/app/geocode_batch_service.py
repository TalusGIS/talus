"""Batch geocoding — DB service: provision the output layer + enqueue, and the worker's job processing.

No FastAPI here (host-testable). `routers/geocode_batch.py` calls `create_batch_job` / `get_job` /
`cancel_job`; the worker loop (`geocode_batch_worker.py`) calls `claim_next_job` / `process_job`.

Provisioning mirrors `publisher._create_spatial_layer` (OpenFGA-owner-FIRST, then provision + registry
in one transaction) and extends it with the output field defs + the pending feature rows + the job row.
The output layer's features ARE the work queue (F5): each input row is inserted up front as a null-geom
`pending` feature carrying its assembled `geocode_input`; the worker fills geometry + match fields in
chunks, committing per chunk (the commit boundary = the resume granularity). No secret is cached — the
worker re-resolves the locator (decrypting its token) from `platform.geocoders` each chunk.
"""
from __future__ import annotations

import secrets as _secrets

import structlog
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from . import geocode, geocode_batch
from .authz import _openfga_write
from .geocoders import active_geocoder_configs

log = structlog.get_logger(__name__)

_AUDIT_CLIENT = "geocode-batch"
# Features per worker transaction (commit boundary = resume granularity). The external adapter chunks
# its own HTTP calls at the locator's SuggestedBatchSize underneath this. 25, not 500: `processed_rows`
# only moves at the commit, so the chunk IS the progress tick and the Cancel latency — 500 rows on a
# county-scale internal locator sat at "0 of 371" for the whole run and read as a hang
# (decision-batch-geocoding-county-scale.md F1). The per-chunk overhead (locator re-resolve, one
# statement, one commit) is milliseconds; 25 keeps the tick under a second on every path measured.
_CHUNK = 25

_MATCH_STATUS = geocode_batch.MATCH_FIELDS[0][0]      # "match_status"
_GEOCODE_INPUT = geocode_batch.GEOCODE_INPUT_FIELD[0]  # "geocode_input"


# --- provisioning + enqueue (the run endpoint's fast, synchronous work) --------------------------


async def _fresh_layer_id(conn: AsyncConnection) -> str:
    """A collision-free `tal_<8hex>` id (mirrors publisher._fresh_layer_id)."""
    async with conn.cursor() as cur:
        for _ in range(5):
            cand = f"tal_{_secrets.token_hex(4)}"
            await cur.execute("SELECT 1 FROM platform.layers WHERE layer_id = %s", (cand,))
            if await cur.fetchone() is None:
                return cand
    raise RuntimeError("could not allocate a layer id")


async def _set_audit(cur, actor_id) -> None:
    """The transaction-local GUCs the V003/V020 audit triggers stamp (audit_context, but from a bare
    actor id — the worker has no User object)."""
    await cur.execute("SELECT set_config('app.current_user_id', %s, true)", (str(actor_id),))
    await cur.execute("SELECT set_config('app.current_client', %s, true)", (_AUDIT_CLIENT,))
    await cur.execute("SELECT set_config('app.current_actor_type', %s, true)", ("human",))


def _output_fields(headers: list[str]) -> list[tuple[str, str, str, str | None]]:
    """The output layer's field defs in display order: geocode_input, the source columns (as text), then
    the match-metadata fields. Returns (field_key, label, data_type, numeric_subtype)."""
    gi = geocode_batch.GEOCODE_INPUT_FIELD
    out: list[tuple[str, str, str, str | None]] = [(gi[0], gi[1], gi[2], None)]
    out += [(k, lbl, dt, None) for k, lbl, dt in geocode_batch.map_columns(headers)]
    out += [(k, lbl, dt, "double" if dt == "number" else None) for k, lbl, dt in geocode_batch.MATCH_FIELDS]
    return out


async def create_batch_job(
    conn: AsyncConnection, actor_id, display_name: str, locator_name: str, threshold: float,
    credit_consuming: bool, headers: list[str], rows: list[dict[str, str]], columns: list[str],
    options: dict | None = None,
) -> dict:
    """Provision the output point layer + its fields, insert every input row as a pending null-geom
    feature, and enqueue the job — one transaction after the OpenFGA owner grant (fail-safe: a mid-failure
    leaves at most an orphan owner tuple, never an unowned layer). Returns {jobId, outputLayerId, totalRows}."""
    layer_id = await _fresh_layer_id(conn)
    # Owner grant FIRST (fail-closed, like every on-demand layer).
    await _openfga_write([(f"user:{actor_id}", "owner", f"layer:{layer_id}")])

    field_defs = _output_fields(headers)
    header_to_key = {h: kf[0] for h, kf in zip(headers, geocode_batch.map_columns(headers))}

    tbl = sql.Identifier("assets", layer_id)
    async with conn.cursor() as cur:
        await _set_audit(cur, actor_id)
        await cur.execute("SELECT platform.provision_spatial_layer(%s, %s, %s)", (layer_id, "point", 4326))
        # A geocoding output legitimately holds UNLOCATED rows (misses / pending / skipped) with no
        # geometry — the ESRI "unmatched records stay in the output" model (F3). The provisioned point
        # table is geom NOT NULL, so drop that on THIS layer: a null geom means "not (yet) located".
        await cur.execute(sql.SQL("ALTER TABLE {tbl} ALTER COLUMN geom DROP NOT NULL").format(tbl=tbl))
        await cur.execute(
            "INSERT INTO platform.layers "
            "(layer_id, editable, display_name, status, geom_type, working_srid, native_storage) "
            "VALUES (%s, true, %s, 'ready', 'point', 4326, false)",
            (layer_id, display_name),
        )
        for sort_order, (key, label, dtype, subtype) in enumerate(field_defs):
            await cur.execute(
                "INSERT INTO platform.layer_fields "
                "(layer_id, field_key, label, data_type, required, sort_order, default_kind, "
                " numeric_subtype, created_by, updated_by) "
                "VALUES (%s, %s, %s, %s, false, %s, 'constant', %s, %s, %s)",
                (layer_id, key, label, dtype, sort_order, subtype, actor_id, actor_id),
            )

        insert = sql.SQL(
            "INSERT INTO {tbl} (geom, props, created_by, updated_by) "
            "VALUES (NULL, %(props)s, %(actor)s, %(actor)s)"
        ).format(tbl=tbl)
        params: list[dict[str, object]] = []
        for r in rows:
            addr = geocode_batch.assemble_address(r, columns)
            props: dict[str, object] = {_GEOCODE_INPUT: addr}
            for h in headers:
                v = r.get(h)
                props[header_to_key[h]] = "" if v is None else str(v)
            # A blank address is terminal-skipped from the start (never geocoded); everything else queues.
            props[_MATCH_STATUS] = geocode_batch.STATUS_PENDING if addr.strip() else geocode_batch.STATUS_SKIPPED
            params.append({"props": Jsonb(props), "actor": str(actor_id)})
        if params:
            await cur.executemany(insert, params)

        await cur.execute(
            "INSERT INTO platform.geocode_batch_jobs "
            "(status, locator_name, output_layer_id, threshold, credit_consuming, total_rows, options, "
            " created_by) "
            "VALUES ('pending', %s, %s, %s, %s, %s, %s, %s) RETURNING id::text",
            (locator_name, layer_id, threshold, credit_consuming, len(params),
             Jsonb(options) if options else None, actor_id),
        )
        job_id = (await cur.fetchone())[0]
    await conn.commit()
    log.info("geocode_batch_enqueued", job_id=job_id, layer_id=layer_id, rows=len(params), locator=locator_name)
    return {"jobId": job_id, "outputLayerId": layer_id, "totalRows": len(params)}


# --- worker-side processing ----------------------------------------------------------------------


async def claim_next_job(conn: AsyncConnection) -> dict | None:
    """The oldest job with work to do (FIFO), moved to 'running'. Called under the worker's advisory lock
    (so no SKIP LOCKED needed). Returns the job dict or None when the queue is empty."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text, locator_name, output_layer_id, threshold::float8, status, created_by::text, "
            "options "
            "FROM platform.geocode_batch_jobs WHERE status IN ('pending', 'running') "
            "ORDER BY created_at LIMIT 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        job = {
            "id": row[0], "locator_name": row[1], "output_layer_id": row[2],
            "threshold": row[3], "status": row[4], "created_by": row[5], "options": row[6],
        }
        if job["status"] == "pending":
            await cur.execute(
                "UPDATE platform.geocode_batch_jobs "
                "SET status = 'running', started_at = COALESCE(started_at, now()), updated_at = now() "
                "WHERE id = %s",
                (job["id"],),
            )
    await conn.commit()
    return job


async def _job_status(conn: AsyncConnection, job_id: str) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT status FROM platform.geocode_batch_jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    return row[0] if row else None


async def _next_pending_chunk(conn: AsyncConnection, tbl: sql.Identifier) -> list[tuple[str, str]]:
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL(
                "SELECT id::text, props ->> %s FROM {tbl} WHERE props ->> %s = %s ORDER BY id LIMIT %s"
            ).format(tbl=tbl),
            (_GEOCODE_INPUT, _MATCH_STATUS, geocode_batch.STATUS_PENDING, _CHUNK),
        )
        return list(await cur.fetchall())


async def _reconcile_progress(conn: AsyncConnection, cur, job_id: str, tbl: sql.Identifier) -> None:
    """Recompute processed/matched from the layer (idempotent — crash-safe, no double count on resume)."""
    await cur.execute(
        sql.SQL(
            "UPDATE platform.geocode_batch_jobs SET "
            "  processed_rows = (SELECT count(*) FROM {tbl} WHERE props ->> %(ms)s <> %(pending)s), "
            "  matched_count  = (SELECT count(*) FROM {tbl} WHERE props ->> %(ms)s = %(matched)s), "
            "  updated_at = now() "
            "WHERE id = %(id)s"
        ).format(tbl=tbl),
        {"ms": _MATCH_STATUS, "pending": geocode_batch.STATUS_PENDING,
         "matched": geocode_batch.STATUS_MATCHED, "id": job_id},
    )


async def process_job(conn: AsyncConnection, job: dict) -> None:
    """Geocode a job's pending features in chunks until none remain, then mark it completed. Honors cancel
    between chunks (marks the un-run rows `canceled`, keeps the partial layer). Fail-safe: any error marks
    the job `failed` with the message and returns — the worker loop never crashes on a bad job."""
    job_id = job["id"]
    tbl = sql.Identifier("assets", job["output_layer_id"])
    threshold = job["threshold"]
    actor = job["created_by"]
    try:
        while True:
            if await _job_status(conn, job_id) == "canceled":
                await _finalize_canceled(conn, job_id, tbl)
                return
            chunk = await _next_pending_chunk(conn, tbl)
            if not chunk:
                await _mark_terminal(conn, job_id, "completed")
                log.info("geocode_batch_completed", job_id=job_id)
                return
            configs = await active_geocoder_configs(conn)
            if not any(c.name == job["locator_name"] for c in configs):
                await _mark_terminal(conn, job_id, "failed",
                                     error=f"locator {job['locator_name']!r} is no longer configured")
                return
            ids = [c[0] for c in chunk]
            queries = [c[1] or "" for c in chunk]
            # The optional country-narrow (external locators only) rides job.options.country as a list of
            # ISO alpha-2 codes → a comma-separated string for sourceCountry / countrycodes.
            opts = job.get("options") or {}
            countries = opts.get("country") if isinstance(opts, dict) else None
            country = ",".join(countries) if countries else None
            results = await geocode.search_batch(
                configs, queries, job["locator_name"], conn=conn, country=country
            )
            await _write_chunk(conn, job_id, actor, tbl, ids, results, threshold, job["locator_name"])
    except Exception as exc:  # noqa: BLE001 — a bad job must never crash the worker loop
        try:
            await conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        await _mark_terminal(conn, job_id, "failed", error=str(exc)[:1000])
        log.warning("geocode_batch_failed", job_id=job_id, exc_info=True)


async def _write_chunk(
    conn: AsyncConnection, job_id: str, actor, tbl: sql.Identifier, ids: list[str],
    results: list[list[geocode.GeoCandidate]], threshold: float, locator_name: str,
) -> None:
    """Bucket each feature's candidates and write geometry + match fields; then reconcile progress. One
    transaction (the resume boundary). `props || patch` merges the match fields onto the existing props."""
    # Cast the NULL-check param (%(lon)s::float8) so a no-match row (lon=None) binds a TYPED NULL —
    # a bare `%(lon)s IS NULL` leaves $1 typeless and Postgres errors "could not determine data type".
    update = sql.SQL(
        "UPDATE {tbl} SET "
        "  geom = CASE WHEN %(lon)s::float8 IS NULL THEN geom "
        "              ELSE ST_SetSRID(ST_MakePoint(%(lon)s::float8, %(lat)s::float8), 4326) END, "
        "  props = props || %(patch)s::jsonb "
        "WHERE id = %(fid)s::uuid"
    ).format(tbl=tbl)
    params: list[dict[str, object]] = []
    for fid, cands in zip(ids, results):
        status, best = geocode_batch.bucket(cands, threshold)
        patch: dict[str, object] = {_MATCH_STATUS: status}
        lon = lat = None
        if best is not None:
            if best.score is not None:
                patch["match_score"] = round(float(best.score), 1)
            if best.label:
                patch["matched_address"] = best.label
            patch["matched_locator"] = locator_name
            if best.match_type:
                patch["match_type"] = best.match_type
            lon, lat = best.lon, best.lat
        params.append({"lon": lon, "lat": lat, "patch": Jsonb(patch), "fid": fid})
    async with conn.cursor() as cur:
        await _set_audit(cur, actor)
        await cur.executemany(update, params)
        await _reconcile_progress(conn, cur, job_id, tbl)
    await conn.commit()


async def _finalize_canceled(conn: AsyncConnection, job_id: str, tbl: sql.Identifier) -> None:
    """Cancel: keep the partial layer, mark the still-pending rows `canceled`, stamp finished_at."""
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("UPDATE {tbl} SET props = props || %s::jsonb WHERE props ->> %s = %s").format(tbl=tbl),
            (Jsonb({_MATCH_STATUS: geocode_batch.STATUS_CANCELED}), _MATCH_STATUS, geocode_batch.STATUS_PENDING),
        )
        await _reconcile_progress(conn, cur, job_id, tbl)
        await cur.execute(
            "UPDATE platform.geocode_batch_jobs SET finished_at = COALESCE(finished_at, now()), updated_at = now() "
            "WHERE id = %s",
            (job_id,),
        )
    await conn.commit()
    log.info("geocode_batch_canceled", job_id=job_id)


async def _mark_terminal(conn: AsyncConnection, job_id: str, status: str, error: str | None = None) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.geocode_batch_jobs "
            "SET status = %s, error = %s, finished_at = now(), updated_at = now() WHERE id = %s",
            (status, error, job_id),
        )
    await conn.commit()


# --- reads / cancel (router) ---------------------------------------------------------------------


async def get_job(conn: AsyncConnection, job_id: str) -> dict | None:
    """One job's status projection for the poll endpoint. Owner/admin gating is the router's job."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id::text, status, locator_name, output_layer_id, threshold::float8, credit_consuming, "
            "total_rows, processed_rows, matched_count, error, created_by::text, "
            "created_at, started_at, finished_at "
            "FROM platform.geocode_batch_jobs WHERE id = %s",
            (job_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "jobId": row[0], "status": row[1], "locator": row[2], "outputLayerId": row[3],
        "threshold": row[4], "creditConsuming": row[5], "totalRows": row[6],
        "processedRows": row[7], "matchedCount": row[8], "error": row[9], "createdBy": row[10],
        "createdAt": row[11], "startedAt": row[12], "finishedAt": row[13],
    }


# --- rematch: re-queue an existing output layer (F4) ---------------------------------------------

# The non-matched statuses "re-run all misses" targets. `skipped` (blank input) and `matched` are left
# alone; `pending` is already queued.
_REMATCH_MISS_STATUSES = [
    geocode_batch.STATUS_NO_MATCH, geocode_batch.STATUS_LOW,
    geocode_batch.STATUS_TIE, geocode_batch.STATUS_CANCELED,
]


async def latest_job_for_layer(conn: AsyncConnection, layer_id: str) -> dict | None:
    """The most recent job for a layer — the rematch's default locator/threshold/credit posture. None if
    the layer was never a batch-geocode output."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT locator_name, threshold::float8, credit_consuming, options "
            "FROM platform.geocode_batch_jobs WHERE output_layer_id = %s ORDER BY created_at DESC LIMIT 1",
            (layer_id,),
        )
        row = await cur.fetchone()
    return None if row is None else {
        "locator_name": row[0], "threshold": row[1], "credit_consuming": row[2], "options": row[3],
    }


async def requeue(
    conn: AsyncConnection, actor_id, layer_id: str, locator_name: str, threshold: float,
    credit_consuming: bool, feature_ids: list[str] | None = None, options: dict | None = None,
) -> dict:
    """Reset the targeted features to `pending` and enqueue a NEW job for the EXISTING layer (F4 re-run;
    the worker re-geocodes them). `feature_ids` → those rows (blank/skipped rows are left alone); else all
    'misses' (no_match / low_score / tie / canceled). Edited addresses are PATCHed via the normal feature
    endpoint BEFORE this call — requeue re-geocodes whatever `geocode_input` currently holds."""
    tbl = sql.Identifier("assets", layer_id)
    patch = Jsonb({_MATCH_STATUS: geocode_batch.STATUS_PENDING})
    async with conn.cursor() as cur:
        await _set_audit(cur, actor_id)
        if feature_ids:
            await cur.execute(
                sql.SQL(
                    "UPDATE {tbl} SET props = props || %s::jsonb "
                    "WHERE id = ANY(%s::uuid[]) AND props ->> %s <> %s"
                ).format(tbl=tbl),
                (patch, list(feature_ids), _MATCH_STATUS, geocode_batch.STATUS_SKIPPED),
            )
        else:
            await cur.execute(
                sql.SQL("UPDATE {tbl} SET props = props || %s::jsonb WHERE props ->> %s = ANY(%s)").format(tbl=tbl),
                (patch, _MATCH_STATUS, _REMATCH_MISS_STATUSES),
            )
        requeued = cur.rowcount
        await cur.execute(sql.SQL("SELECT count(*) FROM {tbl}").format(tbl=tbl))
        total = (await cur.fetchone())[0]
        await cur.execute(
            "INSERT INTO platform.geocode_batch_jobs "
            "(status, locator_name, output_layer_id, threshold, credit_consuming, total_rows, options, "
            " created_by) "
            "VALUES ('pending', %s, %s, %s, %s, %s, %s, %s) RETURNING id::text",
            (locator_name, layer_id, threshold, credit_consuming, total,
             Jsonb(options) if options else None, actor_id),
        )
        job_id = (await cur.fetchone())[0]
    await conn.commit()
    log.info("geocode_batch_requeued", job_id=job_id, layer_id=layer_id, requeued=requeued)
    return {"jobId": job_id, "outputLayerId": layer_id, "requeued": requeued}


async def cancel_job(conn: AsyncConnection, job_id: str) -> str | None:
    """Flag a job canceled (the worker finalizes between chunks). Terminal jobs are left as-is. Returns
    the resulting status, or None if the job doesn't exist."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE platform.geocode_batch_jobs SET status = 'canceled', updated_at = now() "
            "WHERE id = %s AND status IN ('pending', 'running') RETURNING status",
            (job_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            await conn.commit()
            return row[0]
        await cur.execute("SELECT status FROM platform.geocode_batch_jobs WHERE id = %s", (job_id,))
        existing = await cur.fetchone()
    return existing[0] if existing else None
