"""The other services' error logs, as a diagnostic read (decision-ai-substrate.md §13: errors only).

The api's own ring (`recent_warnings`) cannot say why sign-in failed at 10:15 — that lives in Keycloak's log — nor
why the edge refused a certificate (Caddy's), nor that Postgres stopped accepting connections (its own). Those three
services now write their ERROR/WARN-level logs to files on one shared volume (`servicelogs`, per-service directories
made by the `logs-init` one-shot), each with its own native rotation; the api mounts the volume READ-ONLY and this
module reads the tail. NEVER the Docker socket: the api holds no docker client and no socket — that line is the
decision's §13.2 and it does not move.

What is read is bounded three ways: the last `TAIL_BYTES` of the current file only (seek from the end — a corrupt or
hostile file can produce at most that much), the last `LINES_PER_SERVICE` lines of that, each cut to `LINE_CHARS`.
What is kept is an ALLOWLIST per service, decided by what each service writes (verified on the dev stack 2026-10-21):

  caddy     JSON error entries — keep ts, level, logger, msg, status, duration, request.{method, host, uri without
            its query string}, tls.server_name. Dropped: remote_ip / client_ip (a visitor's address), every header
            (Authorization and Cookie live there), resp_headers, anything unknown. There is NO access log: the edge
            writes `level ERROR` only (§13.8), so a visitor never appears unless a request failed at the edge.
  postgres  the text log — keep LOG / WARNING / ERROR / FATAL / PANIC lines; DROP STATEMENT / DETAIL / QUERY /
            CONTEXT / HINT lines entirely (a statement can carry a feature's attributes); `host=…` → `host=***`.
            The role name in `for user "…"` stays: talus / talus_app / talus_tiles are infrastructure names.
  keycloak  the Quarkus text log — keep WARN / ERROR / FATAL lines and an exception's own first line; DROP stack
            frames; username= / userId= / ipAddress= / email= values → ***.

Then the snapshot's generic redaction (`paid/api/ai/snapshot.py::redact`) runs over every string like every other
section — this module is core and knows nothing about who reads it (the one-way rule; register row in
decision-code-boundary.md §4). OpenFGA and pg_tileserv log to stdout only and have no file: they are reported as
"not available", never as an error. A missing volume, directory or file is "not available" too — a diagnostic read
must never be the thing that fails.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import settings

SERVICES: tuple[str, ...] = ("caddy", "postgres", "keycloak", "openfga", "pg_tileserv")
FILE_SINKS: dict[str, str] = {"caddy": "caddy/caddy.log", "postgres": "postgres", "keycloak": "keycloak/keycloak.log"}
STDOUT_ONLY_NOTE = "not available (this service logs to its console only)"
UNAVAILABLE_NOTE = "not available"

TAIL_BYTES = 64 * 1024        # the most of any file ever read
LINES_PER_SERVICE = 50
LINE_CHARS = 500
SECTION_CHARS = 12 * 1024     # the whole section's budget; oldest lines go first

_CADDY_KEEP = ("ts", "level", "logger", "msg", "status", "duration")
_PG_DROP = re.compile(r"^\S+ \S+ \S+ \[\d+\] (STATEMENT|DETAIL|QUERY|CONTEXT|HINT):")
_PG_KEEP = re.compile(r"^\S+ \S+ \S+ \[\d+\] (LOG|WARNING|ERROR|FATAL|PANIC):")
_PG_HOST = re.compile(r"\bhost=\S+")
_KC_LEVEL = re.compile(r"\b(WARN|ERROR|FATAL)\b")
_KC_FRAME = re.compile(r"^\s+(at |\.\.\. \d+ more$)")
_KC_IDENT = re.compile(r"\b(username|userId|user_id|ipAddress|ip_address|email)=\S+")


def tail_lines(path: Path, max_bytes: int = TAIL_BYTES, n: int = LINES_PER_SERVICE) -> list[str]:
    """The last `n` complete lines of the last `max_bytes` of a file. A file larger than the window loses its first
    (partial) line; nothing before the window is ever read."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            data = fh.read(max_bytes)
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        else:
            data = fh.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return [l[:LINE_CHARS] for l in lines[-n:] if l.strip()]


def scrub_caddy(line: str) -> str | None:
    if not line.startswith("{"):
        return line[:LINE_CHARS]
    try:
        d = json.loads(line)
    except ValueError:
        return line[:LINE_CHARS]
    if not isinstance(d, dict):
        return None
    out: dict[str, Any] = {k: d[k] for k in _CADDY_KEEP if k in d}
    req = d.get("request")
    if isinstance(req, dict):
        uri = str(req.get("uri", ""))
        out["request"] = {k: v for k, v in (("method", req.get("method")), ("host", req.get("host")),
                                            ("uri", uri.split("?", 1)[0])) if v}
    tls = d.get("tls")
    if isinstance(tls, dict) and tls.get("server_name"):
        out["tls"] = {"server_name": tls["server_name"]}
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)[:LINE_CHARS]


def scrub_postgres(line: str) -> str | None:
    if _PG_DROP.match(line) or not _PG_KEEP.match(line):
        return None
    return _PG_HOST.sub("host=***", line)[:LINE_CHARS]


def scrub_keycloak(line: str) -> str | None:
    if _KC_FRAME.match(line):
        return None
    if not _KC_LEVEL.search(line) and not line.startswith("Caused by:"):
        return None
    return _KC_IDENT.sub(r"\1=***", line)[:LINE_CHARS]


SCRUBS = {"caddy": scrub_caddy, "postgres": scrub_postgres, "keycloak": scrub_keycloak}


def _current_file(root: Path, service: str) -> Path | None:
    sink = FILE_SINKS[service]
    p = root / sink
    if p.is_file():
        return p
    if p.is_dir():  # postgres writes postgresql-<Day>.log; the newest is the current one
        files = sorted((f for f in p.glob("*.log") if f.is_file()), key=lambda f: f.stat().st_mtime)
        return files[-1] if files else None
    return None


def read_service_logs(root: str | Path | None = None) -> list[dict[str, Any]]:
    """[{service, lines[], note}] for the five services, in a fixed order; bounded; never raises."""
    base = Path(root or settings.service_logs_dir)
    out: list[dict[str, Any]] = []
    for svc in SERVICES:
        if svc not in FILE_SINKS:
            out.append({"service": svc, "lines": [], "note": STDOUT_ONLY_NOTE})
            continue
        try:
            path = _current_file(base, svc)
            if path is None:
                out.append({"service": svc, "lines": [], "note": UNAVAILABLE_NOTE})
                continue
            scrub = SCRUBS[svc]
            kept = [s for s in (scrub(l) for l in tail_lines(path)) if s]
            out.append({"service": svc, "file": path.name, "lines": kept, "note": None})
        except Exception:  # noqa: BLE001 — a diagnostic read never fails the snapshot
            out.append({"service": svc, "lines": [], "note": UNAVAILABLE_NOTE})
    return _bound(out)


def _bound(entries: list[dict[str, Any]], budget: int = SECTION_CHARS) -> list[dict[str, Any]]:
    """Keep the section under its budget by dropping the OLDEST line of the longest service until it fits."""
    def size() -> int:
        return len(json.dumps(entries, ensure_ascii=False))
    while size() > budget:
        longest = max(entries, key=lambda e: sum(len(l) for l in e["lines"]))
        if not longest["lines"]:
            break
        longest["lines"].pop(0)
        longest["note"] = "oldest lines dropped to fit the snapshot"
    return entries
