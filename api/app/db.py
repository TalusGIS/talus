from collections.abc import AsyncIterator

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from .config import settings


# Lifespan-managed singletons. Opened in main.py's lifespan; closed at shutdown.
#   pool       — the MAIN pool: least-privilege runtime role (talus_app). All request data traffic.
#   owner_pool — a SMALL pool on the OWNER role for the rare runtime DDL (layer provisioning, view
#                layers, geocoder index). A separate authenticated connection is the containment:
#                the main role literally cannot perform that DDL, so a SQL-injection on the data
#                path can't either (S-1a — decision-s1-least-privilege-db-role.md).
pool: AsyncConnectionPool | None = None
owner_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    global pool, owner_pool
    # `open=False` then `await open()` is the psycopg3 3.2+ pattern; it avoids
    # the deprecated implicit open during __init__.
    pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=10,
        open=False,
    )
    await pool.open()
    await pool.wait()
    # Owner pool: small (DDL is rare). Falls back to database_url when no separate owner DSN is
    # configured (pre-cutover both are the owner role, so behaviour is unchanged).
    owner_pool = AsyncConnectionPool(
        conninfo=settings.owner_database_url or settings.database_url,
        min_size=1,
        max_size=4,
        open=False,
    )
    await owner_pool.open()
    await owner_pool.wait()


async def close_pool() -> None:
    global pool, owner_pool
    if pool is not None:
        await pool.close()
        pool = None
    if owner_pool is not None:
        await owner_pool.close()
        owner_pool = None


def get_pool() -> AsyncConnectionPool:
    """The live pool for non-request callers (e.g. the lifespan startup seed). Raises if not yet open.

    ★ NON-REQUEST callers ONLY — never call this from a request path. A bare
    `async with get_pool().connection()` inside a FastAPI dependency was tried (2026-08-25, the
    public-sharing slice) to avoid holding a slot across the tile proxy fetch, and under a 20-40-way
    anonymous burst it LEAKED the entire pool: psycopg_pool 3.3.1 logged 13 connections given and
    only 3 returned per burst — the other 10 stayed checked out with no holding task, no exception
    and no cancellation ever observed, freezing every later acquire into 30s PoolTimeouts. The same
    endpoints through `Depends(get_db)` were clean at the same concurrency, repeatedly. Standalone
    reconstructions (plain asyncio, uvloop, anyio task groups) do NOT reproduce it — the trigger
    needs the live uvicorn/starlette middleware stack, so no unit test will save you. Request code
    takes `Depends(get_db)`, full stop."""
    if pool is None:
        raise RuntimeError("DB pool not initialized — call open_pool() in app lifespan")
    return pool


def get_owner_pool() -> AsyncConnectionPool:
    """The OWNER pool for non-request callers. Raises if not yet open."""
    if owner_pool is None:
        raise RuntimeError("DB owner pool not initialized — call open_pool() in app lifespan")
    return owner_pool


async def get_db() -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency. Yields a MAIN-pool (least-privilege) connection; returns it on exit."""
    if pool is None:
        raise RuntimeError("DB pool not initialized — call open_pool() in app lifespan")
    async with pool.connection() as conn:
        yield conn


async def get_owner_db() -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency for the FEW endpoints that perform runtime DDL (layer provisioning, view
    layers, geocoder index). Yields an OWNER-pool connection so that DDL — and any writes that must
    be atomic with it — run with owner privilege. The main data path uses get_db instead."""
    if owner_pool is None:
        raise RuntimeError("DB owner pool not initialized — call open_pool() in app lifespan")
    async with owner_pool.connection() as conn:
        yield conn
