"""The transform-point door answers 422, not 500, for a point the target system cannot hold.

Current versions of the coordinate library refuse a point far outside a projection's domain
("Point outside of projection domain") where older ones returned a meaningless number. The door let
that database error through as a 500. It is a property of the caller's point, so the answer is a plain
refusal — and a non-finite result, which older libraries could return, is refused the same way.

Pure logic with a fake connection, in the style of test_geoservices_device_credential.py.
"""
import asyncio

import pytest
from fastapi import HTTPException
from psycopg import errors as pg_errors

from app.routers import publisher


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        text = str(sql)
        if "spatial_ref_sys" in text:
            self._rows = [(s,) for s in params[0]]
            return
        if self._conn.exc is not None:
            raise self._conn.exc
        self._rows = [self._conn.point]

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, *, exc=None, point=(1.0, 2.0)):
        self.exc = exc
        self.point = point
        self.rolled_back = False

    def cursor(self):
        return _Cursor(self)

    async def rollback(self):
        self.rolled_back = True


def _call(conn, lon=0.0, lat=0.0, srid=6465, from_srid=4326):
    return asyncio.run(publisher.transform_point(lon=lon, lat=lat, srid=srid, from_srid=from_srid, conn=conn, user=None))


def test_a_point_outside_the_projection_domain_is_a_422_and_the_connection_is_rolled_back():
    conn = _Conn(exc=pg_errors.InternalError_("transform: Point outside of projection domain (2050)"))
    with pytest.raises(HTTPException) as e:
        _call(conn)
    assert e.value.status_code == 422
    assert "outside the area that coordinate system EPSG:6465 covers" in e.value.detail
    assert conn.rolled_back is True


@pytest.mark.parametrize("point", [(float("inf"), 1.0), (1.0, float("nan")), (None, None)])
def test_a_meaningless_result_is_refused_the_same_way(point):
    with pytest.raises(HTTPException) as e:
        _call(_Conn(point=point))
    assert e.value.status_code == 422


def test_an_ordinary_point_still_comes_back():
    """The control: the refusals above are for the error and the non-finite result, nothing else."""
    assert _call(_Conn(point=(1613065.9033, 582957.3186))) == {"x": 1613065.9033, "y": 582957.3186, "srid": 6465}
