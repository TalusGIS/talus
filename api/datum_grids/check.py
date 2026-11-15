"""Fail the image build unless BOTH coordinate libraries in this image use the datum correction files.

The api carries two separate copies of the PROJ library (one inside pyproj, one inside rasterio), each with
its own data folder. The Dockerfile copies the files into both. This proves it worked, with five
conversions whose answers are known and which the database must also give
(scripts/verify-invariants/inv56-datum-conversions.sh):

  * NAD27 -> NAD83 at a point in the central United States needs us_noaa_conus.tif;
  * latitude/longitude -> a plain NAD83 State Plane zone (EPSG:3418) needs that state's HARN file
    (us_noaa_iahpgn.tif): the HARN adjustment is applied;
  * latitude/longitude -> the same zone on NAD83(2011) (EPSG:6465) must NOT be adjusted;
  * a NAD83(HARN) position converted to WGS 84 must come back unchanged, as a NAD83(2011) one does:
    the two shifting operations the register holds for HARN are withdrawn (withdraw_nad83_shifts.py),
    in both copies here and in the database's own (db/proj/proj.db). Without that a HARN layer is
    drawn about 0.6 m from every other layer of the same place;
  * a NAD83(CSRS) position converted to WGS 84, at a point in Canada, must come back unchanged too:
    the register's one shift for it is withdrawn the same way. Without that a layer on it is drawn
    about 1.2 m from the same numbers on a later CSRS version, on NAD83(2011) or on GPS.
"""
import sys
import warnings

warnings.simplefilter("ignore")

EXPECTED = (
    # (label, source, target, x in, y in, x out, y out, tolerance)
    ("NAD27 -> NAD83", 4267, 4269, -98.5, 39.5, -98.5003411, 39.5000104, 2e-7),
    ("lat/long -> plain NAD83 State Plane, ftUS (EPSG:3418)", 4326, 3418, -93.6, 41.6, 1613065.4125, 582957.6562, 2e-3),
    ("lat/long -> NAD83(2011) State Plane, ftUS (EPSG:6465)", 4326, 6465, -93.6, 41.6, 1613065.9033, 582957.3186, 2e-3),
    ("NAD83(HARN) lat/long -> WGS 84, unchanged", 4152, 4326, -98.5, 39.5, -98.5, 39.5, 1e-9),
    ("NAD83(CSRS) lat/long -> WGS 84, unchanged", 4617, 4326, -100.0, 55.0, -100.0, 55.0, 1e-9),
)


def _pyproj(src: int, dst: int, x: float, y: float) -> tuple[float, float]:
    from pyproj import Transformer

    return Transformer.from_crs(src, dst, always_xy=True).transform(x, y)


def _rasterio(src: int, dst: int, x: float, y: float) -> tuple[float, float]:
    from rasterio.warp import transform

    xs, ys = transform(f"EPSG:{src}", f"EPSG:{dst}", [x], [y])
    return xs[0], ys[0]


def main() -> int:
    bad = 0
    for name, fn in (("pyproj", _pyproj), ("rasterio", _rasterio)):
        for label, src, dst, x, y, ex, ey, tol in EXPECTED:
            gx, gy = fn(src, dst, x, y)
            ok = abs(gx - ex) <= tol and abs(gy - ey) <= tol
            print(f"  {'ok  ' if ok else 'FAIL'} {name:8} {label}: ({gx:.7f}, {gy:.7f})")
            bad += 0 if ok else 1
    if bad:
        print(f"datum conversions: {bad} wrong — a library is not using the files in datum_grids/ (were they "
              "copied into its data folder?), or still holds a shift for NAD83(HARN) or NAD83(CSRS) "
              "(withdraw_nad83_shifts.py)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
