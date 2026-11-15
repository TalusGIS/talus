# The database's coordinate reference database (`proj.db`)

`proj.db` here is the reference database of the PROJ coordinate library exactly as the pinned database image
ships it (`/usr/share/proj/proj.db`), with **three records marked deprecated**:

| EPSG code | Name | What it is |
|---|---|---|
| 1900 | NAD83(HARN) to WGS 84 (2) | a seven-parameter shift |
| 1901 | NAD83(HARN) to WGS 84 (3) | a seven-parameter shift |
| 1946 | NAD83(CSRS) to WGS 84 (2) | a seven-parameter shift |

`docker-compose.yml` mounts it read-only over the image's own copy.

## Why

For any conversion the library takes the registered operation with the best stated accuracy. For the
realizations of NAD83 used in the United States, the register's last step to WGS 84 is an operation that changes
nothing. For NAD83(HARN) the register also holds the first two shifts above, rated better than its do-nothing
operation (EPSG:1580), so the library took a shift for HARN and not for the others. A layer on a HARN system, a
State Plane zone on HARN for example, was then drawn about 0.6 m (2 ft) from layers on NAD27, original NAD83,
NAD83(2011) or GPS coordinates of the same place. Canada's original NAD83(CSRS) has the same arrangement (the third
shift above, against EPSG:1842), and a layer on it was drawn about 1.2 m (4 ft) from the same numbers on a later
CSRS version, on NAD83(2011) or on GPS.

With the three records deprecated the library ignores them and HARN and CSRS meet the others. Nothing else in the
register is changed. The api image carries two copies of the library and makes the same change to each when it is
built (`api/datum_grids/withdraw_nad83_shifts.py`); `scripts/verify-invariants/inv56-datum-conversions.sh` holds the
three to the same answers.

What is still not modelled, before and after: the few centimetres between HARN and NAD83(2011), and the
difference between NAD83 and WGS 84 themselves (about a metre): on Talus's maps a NAD83 position and a WGS 84
position with the same numbers are drawn at the same place.

## Where the file comes from, and keeping it current

`scripts/derive-proj-db.sh` reads the file out of the database image pinned in `docker-compose.yml`, marks the three
records, and writes `proj.db` and `SHA256SUMS` here. `scripts/derive-proj-db.sh --check` proves the file is that
image's own with those three rows changed and nothing else.

**The file belongs to one version of the library.** When the database image's pin moves, run the script again
and commit the result. `--check` and inv56 fail until that is done.

## What is in it

The library's own tables and the EPSG Geodetic Parameter Dataset, as distributed with PROJ. The dataset is owned by
IOGP and is used under its terms of use; `NOTICES` gives their address. The three records above are changed by
Talus and are not EPSG's data as published; every other record is unchanged.
