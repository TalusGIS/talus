"""`/health` is anonymous: when the database is down it says so and nothing more. (Once-over A20.)"""
import asyncio

import pytest
from fastapi import HTTPException

from app.routers.health import health

SECRET_DETAIL = 'connection to server at "postgres" (172.18.0.5), port 5432 failed: FATAL: role "talus_app"'


class _DownConn:
    def cursor(self):
        raise RuntimeError(SECRET_DETAIL)


class _Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql):
        return None

    async def fetchone(self):
        return (1,)


class _UpConn:
    def cursor(self):
        return _Cursor()


def test_database_error_text_never_reaches_the_caller():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(health(_DownConn()))
    assert exc.value.status_code == 503
    assert exc.value.detail == "db ping failed"
    for leak in ("postgres", "172.18", "talus_app", "FATAL", "5432"):
        assert leak not in str(exc.value.detail)


def test_healthy_answer_is_unchanged():
    assert asyncio.run(health(_UpConn())) == {"status": "ok", "db": "connected"}
