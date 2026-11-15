"""Batch (bulk) geocoding — the pure core (decision-batch-geocoding.md).

Deliberately DB-free and FastAPI-free so the tricky parts — CSV parsing, single/multi-column address
assembly (F2), per-row status bucketing (F4/F4a), and output-field naming — are unit-testable without a
database. The router (`routers/geocode_batch.py`) owns everything stateful: the preflight/credit gate,
provisioning the output point layer, and the bulk insert. This module only *consumes* `geocode` (the
adapters + `search_batch`); `geocode` never imports it, so there is no cycle.

Status buckets (F4): a row is `matched` (score ≥ the user threshold, F4a), `low_score` (below it but a
candidate exists), `tie` (top-2 within `TIE_DELTA` — ambiguous), `no_match` (no candidate), or `skipped`
(a blank address cell — never geocoded). The raw score is persisted so the threshold re-buckets live in
the review table with no re-geocode (F4a)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from .geocode import GeoCandidate

# --- status vocabulary (stored verbatim in the output layer's match_status field) ----------------
STATUS_PENDING = "pending"      # enqueued, not yet geocoded (the async worker's work queue, F5)
STATUS_MATCHED = "matched"
STATUS_LOW = "low_score"
STATUS_TIE = "tie"
STATUS_NO_MATCH = "no_match"
STATUS_SKIPPED = "skipped"      # blank address cell — never geocoded
STATUS_CANCELED = "canceled"    # job canceled before this row ran

# The wizard default cut (F4a); user-adjustable before the run and via the live review slider after.
DEFAULT_THRESHOLD = 85.0
# Two candidates whose scores sit within this on the 0–100 scale are an ambiguous tie (review, not auto-match).
TIE_DELTA = 5.0

# Per-locator-type sync row caps (F5). Keyed by GeocoderConfig.type.
ROW_CAPS = {"internal": 50_000, "arcgis": 5_000, "nominatim": 1_000}
# The floor for any type not in the map (defensive; the router validates the locator first).
DEFAULT_ROW_CAP = 1_000

# Reserved slot columns a field_key may never shadow (mirrors the V044 CHECK).
_RESERVED_KEYS = {
    "id", "geom", "notes", "props", "row_version", "ai_provenance",
    "created_at", "created_by", "updated_at", "updated_by",
}

# The assembled address query, materialized as a field on the output layer (F3): what the worker
# geocodes and what "edit the address" rematch edits — the geocode source of truth, decoupled from the
# raw columns. Minted first (before the originals) so it reads at the top of the attribute table.
GEOCODE_INPUT_FIELD = ("geocode_input", "Geocode input", "text")

# The match-metadata fields minted on every output layer, in display order. (key, label, data_type).
# `match_type` = ESRI Addr_type/Match_type when the external locator gives it (a rooftop-vs-approximate
# QA signal, per the doc review); null for the internal locator.
MATCH_FIELDS = [
    ("match_status", "Match status", "text"),
    ("match_score", "Match score", "number"),
    ("matched_address", "Matched address", "text"),
    ("matched_locator", "Matched locator", "text"),
    ("match_type", "Match type", "text"),
]
# Every reserved output-field key the source-column sanitizer must avoid colliding with.
_MATCH_KEYS = {k for k, _, _ in MATCH_FIELDS} | {GEOCODE_INPUT_FIELD[0]}


def cap_for_type(locator_type: str) -> int:
    return ROW_CAPS.get(locator_type, DEFAULT_ROW_CAP)


@dataclass
class BatchRow:
    """One input row: its 0-based index, the assembled address query ('' = blank → skipped), and the
    original column values to carry onto the output feature's props."""

    index: int
    address: str
    props: dict[str, str]


@dataclass
class RowResult:
    """The resolved outcome for one input row — what a review-table row and the output feature carry."""

    index: int
    status: str
    address: str
    score: float | None = None
    matched_address: str | None = None
    lon: float | None = None
    lat: float | None = None
    props: dict[str, str] | None = None


def parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Address CSV → (headers, rows-as-dicts), fully in-memory (the coord-less case the existing
    add-from-file path rejects — decision §2). Values are kept as strings; the caller assembles the
    address from the mapped columns. Raises ValueError on an unreadable/empty CSV (router → 422)."""
    reader = csv.DictReader(io.StringIO(text))
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    if not headers:
        raise ValueError("the CSV has no header row")
    rows: list[dict[str, str]] = []
    for raw in reader:
        # DictReader can yield None keys for ragged rows; keep only the declared headers, as strings.
        rows.append({h: ("" if raw.get(h) is None else str(raw.get(h))) for h in headers})
    return headers, rows


def assemble_address(row: dict[str, str], columns: list[str]) -> str:
    """The F2 address string for one row: single column verbatim, or N columns space-joined in the
    chosen order (the same `concat_ws(' ', …)` shape the internal locator uses on its own layer).
    Blank/whitespace parts are dropped; the result is whitespace-collapsed."""
    parts = [str(row.get(c) or "").strip() for c in columns]
    return " ".join(p for p in parts if p)


def build_rows(rows: list[dict[str, str]], columns: list[str]) -> list[BatchRow]:
    """Input dict-rows + the chosen address column(s) → BatchRows carrying the assembled address and
    ALL original columns (so the output feature preserves the full source row, not just the address)."""
    return [
        BatchRow(index=i, address=assemble_address(r, columns), props=dict(r))
        for i, r in enumerate(rows)
    ]


def bucket(
    candidates: list[GeoCandidate], threshold: float = DEFAULT_THRESHOLD, tie_delta: float = TIE_DELTA
) -> tuple[str, GeoCandidate | None]:
    """Best-first candidates → (status, best). `no_match` when empty; `tie` when a runner-up sits within
    `tie_delta` of a non-perfect best (ambiguous — send to review, don't auto-place); else `matched` at
    or above the threshold, `low_score` below it. Score None is treated as 0 (unknown confidence)."""
    if not candidates:
        return (STATUS_NO_MATCH, None)
    best = candidates[0]
    score = best.score if best.score is not None else 0.0
    if len(candidates) > 1 and candidates[1].score is not None and score < 100 \
            and (score - candidates[1].score) <= tie_delta:
        return (STATUS_TIE, best)
    if score >= threshold:
        return (STATUS_MATCHED, best)
    return (STATUS_LOW, best)


def resolve_rows(
    rows: list[BatchRow], candidates_by_index: list[list[GeoCandidate]], threshold: float
) -> list[RowResult]:
    """Fold the per-row candidate lists (aligned to `rows`) into RowResults. A blank address is
    `skipped` without consulting candidates (it was never geocoded)."""
    out: list[RowResult] = []
    for row in rows:
        if not row.address.strip():
            out.append(RowResult(index=row.index, status=STATUS_SKIPPED, address=row.address,
                                  props=row.props))
            continue
        cands = candidates_by_index[row.index] if row.index < len(candidates_by_index) else []
        status, best = bucket(cands, threshold)
        out.append(RowResult(
            index=row.index, status=status, address=row.address, props=row.props,
            score=(best.score if best else None),
            matched_address=(best.label if best else None),
            lon=(best.lon if best else None), lat=(best.lat if best else None),
        ))
    return out


def dedup_addresses(rows: list[BatchRow]) -> tuple[list[str], list[int]]:
    """The unique non-blank address set + a per-row index INTO that set (F7 — geocode each distinct
    address once on the rate-limited/credit-metered external path, then fan results back to every row
    that shares it). A blank row maps to -1 (never geocoded). Case-folded/whitespace-collapsed key so
    'Main St' and 'MAIN ST' collapse to one external call."""
    order: list[str] = []
    seen: dict[str, int] = {}
    row_to_unique: list[int] = []
    for row in rows:
        key = " ".join(row.address.split()).lower()
        if not key:
            row_to_unique.append(-1)
            continue
        if key not in seen:
            seen[key] = len(order)
            order.append(row.address)
        row_to_unique.append(seen[key])
    return order, row_to_unique


def fan_out(
    unique_results: list[list[GeoCandidate]], row_to_unique: list[int], n_rows: int
) -> list[list[GeoCandidate]]:
    """Map deduped unique-address results back to every input row (the inverse of `dedup_addresses`)."""
    out: list[list[GeoCandidate]] = []
    for i in range(n_rows):
        u = row_to_unique[i] if i < len(row_to_unique) else -1
        out.append(unique_results[u] if 0 <= u < len(unique_results) else [])
    return out


def sanitize_key(name: str, taken: set[str]) -> str:
    """A source column name → a valid, unique `layer_fields.field_key` (`^[a-z][a-z0-9_]{0,62}$`).
    Lowercases, maps non-alnum to `_`, ensures a leading letter (`f_` prefix), truncates to 63, and
    de-collides against `taken`, the reserved slot columns, and the match-metadata keys. Mutates
    `taken`."""
    k = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", name.strip().lower())).strip("_")
    if not k or not k[0].isalpha():
        k = "f_" + k
    k = k[:63]
    base = k
    n = 1
    while k in taken or k in _RESERVED_KEYS or k in _MATCH_KEYS:
        suffix = f"_{n}"
        k = base[: 63 - len(suffix)] + suffix
        n += 1
    taken.add(k)
    return k


def map_columns(headers: list[str]) -> list[tuple[str, str, str]]:
    """The output layer's field defs for the source columns: (field_key, label, data_type='text') per
    header, in order. All source columns are preserved as text (v1 keeps it simple — the output's value
    is the geometry + review; typed re-coercion is a successor). `label` keeps the original header."""
    taken: set[str] = set()
    return [(sanitize_key(h, taken), h, "text") for h in headers]
