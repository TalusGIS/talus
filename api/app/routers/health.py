import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection

from ..db import get_db

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(conn: AsyncConnection = Depends(get_db)) -> dict[str, str]:
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1;")
            await cur.fetchone()
    except Exception as exc:
        # This door is anonymous and exempt from the rate limiter. The database's own error text
        # (host names, role names, the driver's wording) goes to the log, never to the caller
        # (once-over A20). Admin → Health shows the detail to an administrator.
        log.error("health_db_ping_failed", error=exc.__class__.__name__, detail=str(exc)[:300])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="db ping failed",
        )
    return {"status": "ok", "db": "connected"}
