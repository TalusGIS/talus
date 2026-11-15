# Datum correction files

These files let Talus convert coordinates between North American datums properly. Without them a
coordinate library still converts, but it falls back to "no correction", and older data lands in the
wrong place: tens of metres for NAD27, up to about a metre for original NAD83.

**What is here (61 files, about 1.4 MB):**

- **NAD27 to NAD83** (7 files): `us_noaa_conus.tif`, `us_noaa_alaska.tif`, `us_noaa_hawaii.tif`,
  `us_noaa_prvi.tif`, `us_noaa_stgeorge.tif`, `us_noaa_stlrnc.tif`, `us_noaa_stpaul.tif`.
- **NAD83 (1986) to NAD83(HARN)** (54 files): one per state or region (`us_noaa_alhpgn.tif`, …) and five
  older regional files (`us_noaa_FL.tif`, `MD`, `TN`, `WI`, `WO`).

Also here: `withdraw_nad83_shifts.py`, which marks three operations in the coordinate library's reference
database as deprecated so that a layer on NAD83(HARN) or on Canada's original NAD83(CSRS) is drawn with layers on
the other datums (it says why at its top; the database's copy is `db/proj/proj.db`), and `check.py`, which fails
the api image's build unless both of the image's coordinate libraries give the known answers.

**Where they come from.** They are the National Geodetic Survey's published grids, repackaged in GeoTIFF form
by the PROJ project and downloaded unchanged from the PROJ project's file server (`https://cdn.proj.org/`)
on 2026-11-14. `us_noaa_README.txt` is that project's own description of them and states the licence of each:
public domain. `SHA256SUMS` records each file's checksum as downloaded.

**Who reads them.**

- The database: the compose file mounts this folder, read-only, where the coordinate library inside the
  PostGIS image looks for extra files.
- The api: its image copies them into the data folder of each of the two coordinate libraries it carries,
  and the image build fails if either library cannot use them (`check.py`).

Both must use the same files, or the database and the api will disagree about where the same point is.
`scripts/verify-invariants/inv56-datum-conversions.sh` checks that they agree, on a running system.

**What this means for data.** A layer is converted as what it says it is. Data on a plain NAD83 or NAD27
system is treated as that era's coordinates and receives the corresponding correction when it meets
latitude/longitude. Data known to be on NAD83(HARN) or NAD83(2011) should be given that system.

Not included: the newer "NADCON5" grids, geoid (height) grids, and grids for other countries. Nothing is
fetched at run time; the libraries' network access stays off.
