#!/usr/bin/env bash
# Extract a local-area basemap from a Protomaps daily planet build into
# web/public/basemap.pmtiles (served by Vite via HTTP range requests).
#
# DEV FIXTURE ONLY. Production deployments get their basemap from the
# portal-provisioned archive, discovered at runtime via GET /basemap
# (decision-basemap-provisioning.md F2) — this script exists so a repo clone has
# something to render before anything is provisioned.
#
# Tooling: the protomaps/go-pmtiles Docker image (no binary to download/commit).
#
# The build URL is NOT hardcoded — Protomaps rotates daily builds and discourages
# hotlinking. Get the latest dated file from https://maps.protomaps.com/builds
# (e.g. 20260601.pmtiles) and pass either the date or the full URL as $1.
#
# Usage:
#   scripts/basemap/extract-basemap-pmtiles.sh 20260601
#   scripts/basemap/extract-basemap-pmtiles.sh https://build.protomaps.com/20260601.pmtiles
#
# Set BASEMAP_BBOX / BASEMAP_MAXZOOM to cover your own area, e.g.
#   BASEMAP_BBOX=-83.1,39.9,-82.8,40.1 scripts/basemap/extract-basemap-pmtiles.sh 20260601
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <YYYYMMDD | full-pmtiles-url>" >&2
  echo "  find the latest build at https://maps.protomaps.com/builds" >&2
  exit 1
fi

ARG="$1"
if [[ "$ARG" =~ ^https?:// ]]; then
  SRC="$ARG"
else
  SRC="https://build.protomaps.com/${ARG}.pmtiles"
fi

# No default area: name your own. west,south,east,north in WGS 84 degrees.
BBOX="${BASEMAP_BBOX:?set BASEMAP_BBOX=west,south,east,north (WGS 84 degrees) for the area to extract}"
MAXZOOM="${BASEMAP_MAXZOOM:-15}"

# Resolve repo root from this script's location, write into web/public.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${ROOT}/web/public"
mkdir -p "$OUT_DIR"

echo "Extracting ${SRC}"
echo "  bbox=${BBOX} maxzoom=${MAXZOOM} -> web/public/basemap.pmtiles"

docker run --rm -v "${OUT_DIR}:/out" protomaps/go-pmtiles \
  extract "${SRC}" /out/basemap.pmtiles \
  --bbox="${BBOX}" \
  --maxzoom="${MAXZOOM}"

echo "Done. web/public/basemap.pmtiles is ready (gitignored; not committed)."
