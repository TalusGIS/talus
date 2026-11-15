# Pinned to bookworm (Debian 12): the bare `slim` tag tracks the newest Debian (now trixie), and the DR
# slice's PGDG repo line + comments target bookworm. Pinning keeps the base deterministic across rebuilds.

# Basemap provisioning (decision-basemap-provisioning.md F3): the go-pmtiles binary — a single static
# Go executable — copied out of the official image so the api can run bounded `pmtiles extract`
# subprocesses server-side (argv-only, no shell, no docker-in-docker). Pinned by tag AND digest
# (supply-chain hygiene, same spirit as the pinned Python base below): this is the exact image the
# slice's live acceptance ran against. Bump deliberately — retest an extract after any bump.
FROM protomaps/go-pmtiles:v1.31.2@sha256:06574f01f55a78f78f887bc7ebf729a5c093c0d6e17d9876300cfcb0758b59d3 AS go-pmtiles

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Disaster recovery (Talus 40): pg_dump / pg_restore for the scheduled-backup job + the restore runbook.
# Debian bookworm ships postgresql-client 15, which REFUSES to dump a v16 server, so we pull
# postgresql-client-16 from the PostgreSQL APT (PGDG) repo. Baked into the image -> air-gapped RUNTIME is
# unaffected (only the image BUILD needs network, as every apt install already does). ca-certificates also
# serves the runtime: the backup target's S3 client (BACKUP_S3_*) may speak TLS to an off-host endpoint.
# `age` (Talus 40.1): optional client-side encryption of backup artifacts before upload (bookworm ships it;
# tiny, streaming, battle-tested). Installed from the base repo alongside the PGDG client below.
# libexpat1 (raster slice, V045+): rasterio's manylinux wheel bundles GDAL/PROJ/GEOS but links the
# SYSTEM libexpat (libexpat.so.1), which slim-bookworm strips — without it `import rasterio` fails. One
# tiny runtime lib is the whole system-package cost of embedded rio-tiler (no apt GDAL needed).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg age libexpat1; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    # The key arrives over TLS; pin its fingerprint so a swapped key fails the build instead of vouching for
    # whatever apt then installs (the release source fetcher pins the same one: third_party_notices/sources.py).
    gpg --batch --show-keys --with-colons --with-fingerprint /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        | grep -Eq '^fpr(:[^:]*){8}:B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:' \
        || { echo "FAIL: the PostgreSQL apt key does not have the pinned fingerprint" >&2; exit 1; }; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-16; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*
# pg_dump/pg_restore 16 land in /usr/lib/postgresql/16/bin (not on PATH by default).
ENV PATH="/usr/lib/postgresql/16/bin:${PATH}"

# The go-pmtiles static binary (~15 MB). Air-gapped runtime unaffected: fetch simply isn't called
# there (upload is the air-gapped path); only the image build needs network, as with apt above.
COPY --from=go-pmtiles /go-pmtiles /usr/local/bin/pmtiles

# Reports (decision-scheduled-reports.md F3): the Typst PDF renderer — one static musl binary,
# Apache-2.0, run as a bounded argv-only subprocess on a per-job temp dir (no shell, no network
# use: the house template imports no packages; CI greps for that). Pinned by sha256 per arch.
# Liberation Sans 2.1.5 (OFL-1.1) is the report face; --ignore-system-fonts at compile time.
# (.xz is unpacked via python's tarfile — slim ships no xz-utils and one stdlib call beats an apt package.)
# Third-party notices land in /usr/share/doc/talus-third-party/ (licence audit, bundled binaries).
ARG TARGETARCH
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) T=x86_64;  SHA=a6d077d0a95eed5a2eba715b2dae06be954f624ccbf85758a03f389ded33118c ;; \
      arm64) T=aarch64; SHA=5aa8d74a3d906e60ea12a66ac2f37f8eef1b14cbad7182a745e393a10c23dcee ;; \
      *) echo "unsupported arch $TARGETARCH"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/typst.tar.xz "https://github.com/typst/typst/releases/download/v0.15.1/typst-${T}-unknown-linux-musl.tar.xz"; \
    echo "${SHA}  /tmp/typst.tar.xz" | sha256sum -c -; \
    python3 -c "import tarfile; tarfile.open('/tmp/typst.tar.xz').extractall('/tmp', filter='data')"; \
    install -m 0755 /tmp/typst-${T}-unknown-linux-musl/typst /usr/local/bin/typst; \
    install -d /usr/share/doc/talus-third-party/typst; \
    cp /tmp/typst-${T}-unknown-linux-musl/LICENSE /tmp/typst-${T}-unknown-linux-musl/NOTICE /usr/share/doc/talus-third-party/typst/; \
    curl -fsSL -o /tmp/fonts.tar.gz "https://github.com/liberationfonts/liberation-fonts/files/7261482/liberation-fonts-ttf-2.1.5.tar.gz"; \
    echo "7191c669bf38899f73a2094ed00f7b800553364f90e2637010a69c0e268f25d0  /tmp/fonts.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/fonts.tar.gz -C /tmp; \
    install -d /usr/share/talus-fonts /usr/share/doc/talus-third-party/liberation-fonts; \
    install -m 0644 /tmp/liberation-fonts-ttf-2.1.5/LiberationSans-*.ttf /usr/share/talus-fonts/; \
    cp /tmp/liberation-fonts-ttf-2.1.5/LICENSE /usr/share/doc/talus-third-party/liberation-fonts/; \
    rm -rf /tmp/typst* /tmp/fonts.tar.gz /tmp/liberation-fonts-ttf-2.1.5; \
    /usr/local/bin/typst --version

COPY requirements.txt .
RUN pip install -r requirements.txt

# Third-party software: the image checks itself, against third_party_notices/inventory.toml. A new native
# library bundled in a wheel, a changed copyleft library, newly statically linked copyleft code, a new
# copyleft Python package, or a new executable in /usr/local/bin fails the build HERE — so it fails the
# release, which builds this image. Each is a licence event, not a build detail: the inventory says what the
# component is, its licence and where its exact source comes from. The inventory pins one architecture's
# bytes; on any other, a release build (TALUS_RELEASE=1) fails and every other build only reports.
# Then the image writes its own NOTICES (and NOTICES.json) to /usr/share/doc/talus/ — generated from what is
# installed, never maintained by hand; TALUS_VERSION names the release in it.
# Rebuilding with a component you replaced (your right under the LGPL — docs/replacing-lgpl-components.md)?
# Build with --build-arg TALUS_THIRD_PARTY_CHECK=report: the check reports instead of failing, and NOTICES
# opens with MODIFIED BUILD and the differences. A release build ignores it.
ARG TALUS_RELEASE=0
ARG TALUS_VERSION=dev
ARG TALUS_THIRD_PARTY_CHECK=enforce
COPY third_party_notices/ ./third_party_notices/
RUN export TALUS_RELEASE="$TALUS_RELEASE" TALUS_THIRD_PARTY_CHECK="$TALUS_THIRD_PARTY_CHECK" \
 && python3 -m third_party_notices check \
 && TALUS_VERSION="$TALUS_VERSION" python3 -m third_party_notices generate --out /usr/share/doc/talus

# Datum correction files (datum_grids/README.md): the National Geodetic Survey's NAD27 and HARN grids,
# public domain. This image carries TWO copies of the PROJ library, one inside pyproj and one inside
# rasterio, each with its own data folder and neither with any United States grid; a folder named in
# PROJ_DATA does not work for both (one then loses its own catalog). So the files are copied into each
# data folder, and check.py fails the build unless both libraries then give the known answers. The
# database reads the same files through a mount (docker-compose.yml); the two must agree.
# In the same pass the three shifting NAD83 -> WGS 84 operations (two for NAD83(HARN), one for
# NAD83(CSRS)) are withdrawn from each copy's reference database (withdraw_nad83_shifts.py says why);
# the database gets the same change through db/proj/proj.db. check.py proves that too.
COPY datum_grids/ /usr/share/talus/datum_grids/
RUN set -eux; \
    for d in "$(python3 -c 'import pyproj; print(pyproj.datadir.get_data_dir())')" \
             "$(python3 -c 'import os, rasterio; print(os.path.join(os.path.dirname(rasterio.__file__), "proj_data"))')"; do \
        test -f "$d/proj.db"; \
        cp /usr/share/talus/datum_grids/*.tif "$d"/; \
        python3 /usr/share/talus/datum_grids/withdraw_nad83_shifts.py "$d/proj.db"; \
    done; \
    python3 /usr/share/talus/datum_grids/check.py

COPY app/ ./app/

# S-3 prod-profile hardening (decision-s3-prod-profile.md): the api runs as a NON-ROOT user.
# ★ uid/gid 10001 is a SHARED CONSTANT with install.sh, which chowns the bind-mounted ./.env to
# it (the file keeps mode 600 — only its owner changes, and root, which the install paths run as,
# reads it regardless). Two places holding the same number is a drift risk, so both name this doc.
#
# ★ /storage is created HERE, owned by talus, on purpose: Docker seeds an EMPTY named volume from
# the image's content AND OWNERSHIP at the mount path, so a fresh `storagedata` comes out
# talus-owned and a fresh install needs no chown at all. A pre-existing (non-empty) volume gets no
# such copy — that is the upgrade case, and it is why this landed during the free-tier freeze,
# before an installed base made it a permanent upgrade-runbook step.
#
# /app stays root-owned and is only READ by the app (PYTHONDONTWRITEBYTECODE=1, no runtime writes),
# so a non-root process cannot modify its own code — a small bonus of the same change.
RUN set -eux; \
    groupadd --gid 10001 talus; \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin talus; \
    install -d -o talus -g talus /storage
USER talus

EXPOSE 8000

# The DEV overlay (the gitignored docker-compose.override.yml) bind-mounts ./api/app over
# /app/app so branch code is live without a rebuild; the api is restarted to pick up an edit (the
# shipped compose's `command:` does not pass --reload). Without the overlay (a customer install) the
# code is the image's own. Deps are baked either way, so a dependency change still needs
# `docker compose build api`.
# --timeout-keep-alive 65 (default 5s): desktop OGC clients (QGIS's Qt network stack) hold
# keep-alive connections across multi-second processing pauses between page fetches; a 5s server
# close races the next request into a mid-flight TCP reset — an invisible-to-access-logs truncated
# response ("Failed to read GeoJSON data", found live on the QGIS 3.44 rig, interop I-1). 65s sits
# above the ~60s idle window desktop HTTP stacks assume. Keep the compose dev override in step.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "65"]
