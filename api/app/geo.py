"""Shared geo helpers for the feature-reading routers (layer_query, geocode, …).

Extracted when the second consumer appeared: every list endpoint takes the identical
EPSG:4326 bbox query param and parses it the same way. One place to validate, every
router imports it.
"""
from fastapi import HTTPException, status


def parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    """Parse 'lon_min,lat_min,lon_max,lat_max' (EPSG:4326) -> a validated 4-tuple.

    Raises 400 on the wrong arity, non-numeric values, out-of-range degrees, or a
    non-strictly-increasing box.
    """
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bbox must be 'lon_min,lat_min,lon_max,lat_max'",
        )
    try:
        lon_min, lat_min, lon_max, lat_max = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bbox values must be numeric",
        )
    if not (-180 <= lon_min <= 180 and -180 <= lon_max <= 180):
        raise HTTPException(status_code=400, detail="longitude must be in [-180, 180]")
    if not (-90 <= lat_min <= 90 and -90 <= lat_max <= 90):
        raise HTTPException(status_code=400, detail="latitude must be in [-90, 90]")
    if lon_min >= lon_max or lat_min >= lat_max:
        raise HTTPException(status_code=400, detail="bbox min must be strictly less than max")
    return lon_min, lat_min, lon_max, lat_max
