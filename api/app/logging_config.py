"""Centralized structlog + stdlib logging for the Talus API.

One pipeline for everything: structlog-native loggers AND stdlib loggers
(uvicorn.error, uvicorn.access, anything using logging.getLogger) are funneled
through structlog's ProcessorFormatter, so every line renders consistently —
colored key/value in dev, JSON in prod. This is what prevents the half-JSON /
half-plaintext problem where uvicorn's access/startup lines stay unstructured.

Call configure_logging() once, as early as possible at process start.
"""
import logging
from collections import deque
import re
import sys
import uuid

import structlog

from .config import settings

REQUEST_ID_HEADER = b"x-request-id"

# Credential-bearing query params, redacted from every rendered log line (decision-geocoder-
# token-locators §Security T3, defense-in-depth). uvicorn.access logs the FULL request line —
# query string included — so without this, any URL-borne secret (e.g. a locator pointed at a
# host that shares this log stream) lands in the logs verbatim. No Talus endpoint accepts
# credentials via query param (PATs ride Basic/Bearer headers), so redacting these loses nothing.
_SENSITIVE_QS = re.compile(
    r"\b(token|access_token|apikey|api_key|client_secret|password|secret)=[^&\s\"']+",
    re.IGNORECASE,
)


def _scrub_query_secrets(logger, method_name, event_dict):
    ev = event_dict.get("event")
    if isinstance(ev, str) and "=" in ev:
        event_dict["event"] = _SENSITIVE_QS.sub(r"\1=***", ev)
    return event_dict


def _add_service_context(logger, method_name, event_dict):
    """Static service metadata on every record."""
    event_dict["service"] = "talus-api"
    event_dict["version"] = settings.service_version
    event_dict["environment"] = settings.environment
    return event_dict


# --- recent-warnings ring (a generic operational facility, consumed by Admin-facing readers) -----
# The last RECENT_MAX records at WARNING or above, kept in process memory as small dicts: the
# api cannot read its own container log (no docker socket, by design), so this is the only way a
# "what went wrong recently" reader can exist without a log shipper. Bounded on both axes (count and
# per-field length); runs AFTER _scrub_query_secrets so the T3 redaction applies here too. Core owns
# it and knows nothing about who reads it (the one-way rule).
RECENT_MAX = 200
RECENT_FIELD_CHARS = 500
# This process's identity, so the persisted copy of the ring (V114, app/recent_warnings_store.py) can tell
# "what memory lost" (rows from an earlier boot) from what the ring still holds.
BOOT_ID = uuid.uuid4().hex
_RECENT_SKIP = frozenset({"timestamp", "level", "logger", "event", "service", "version", "environment", "exception",
                          "exc_info", "stack", "_record", "_from_structlog"})
_recent: deque = deque(maxlen=RECENT_MAX)
_seq = 0   # monotonic per process; the flusher's watermark (never reused, never exposed in a snapshot)


def _capture_recent(logger, method_name, event_dict):
    global _seq
    level = str(event_dict.get("level") or method_name or "").lower()
    if level in ("warning", "warn", "error", "critical", "exception"):
        fields = {k: str(v)[:RECENT_FIELD_CHARS] for k, v in event_dict.items()
                  if k not in _RECENT_SKIP and not str(k).startswith("_")}
        _seq += 1
        _recent.append({
            "seq": _seq,
            "timestamp": event_dict.get("timestamp"),
            "level": {"exception": "error", "warn": "warning"}.get(level, level),
            "logger": event_dict.get("logger"),
            "event": str(event_dict.get("event"))[:RECENT_FIELD_CHARS],
            "fields": fields,
        })
    return event_dict


def _public(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "seq"}


def recent_warnings() -> list[dict]:
    """Oldest first. A snapshot copy — callers may mutate freely."""
    return [_public(r) for r in _recent]


def recent_warnings_since(seq: int) -> list[tuple[int, dict]]:
    """(seq, record) pairs newer than `seq`, oldest first — the persisting flusher's read."""
    return [(r["seq"], _public(r)) for r in _recent if r["seq"] > seq]


def oldest_recent_seq() -> int | None:
    """The seq of the oldest record memory still holds (None when the ring is empty)."""
    return _recent[0]["seq"] if _recent else None


def _shared_processors() -> list:
    """Enrichment applied to BOTH structlog-native and stdlib (foreign) records."""
    return [
        structlog.contextvars.merge_contextvars,      # pulls request_id from contextvars
        structlog.stdlib.add_logger_name,             # -> "logger"
        structlog.stdlib.add_log_level,               # -> "level"
        structlog.stdlib.PositionalArgumentsFormatter(),  # supports legacy log.x("%s", a)
        _scrub_query_secrets,                         # redact token=…/secret=… from any line (T3)
        _add_service_context,                         # service / version / environment
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # -> "timestamp" (UTC ISO-8601)
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _capture_recent,                              # the recent-warnings ring (after scrub + timestamp)
    ]


def configure_logging() -> None:
    """Idempotent: clears existing handlers and installs one structlog-backed handler."""
    shared = _shared_processors()

    # structlog-native loggers run `shared`, then hand the event dict to the
    # stdlib ProcessorFormatter (wrap_for_formatter MUST be last here).
    structlog.configure(
        processors=shared + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_json_format
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    # ProcessorFormatter renders structlog records AND foreign (stdlib/uvicorn)
    # records. foreign_pre_chain enriches the foreign ones with the same fields.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Reclaim uvicorn's loggers: drop their private handlers and let them
    # propagate to root (our formatter). This is the key to consistent output.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    # httpx logs every outbound request URL at INFO — INCLUDING query strings, so a secured
    # locator's token= would land in the logs on every geocode (decision-geocoder-token-locators
    # §Security T3). Quiet both to WARNING; our own structured events carry what's needed.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class RequestIDMiddleware:
    """Pure-ASGI middleware: bind a request_id to structlog contextvars for the
    duration of each request, so every log line emitted while handling it carries
    request_id automatically. Reuses an inbound X-Request-ID, else generates one,
    and echoes it back on the response.

    Pure ASGI (not BaseHTTPMiddleware) so the contextvar binding is guaranteed
    visible to the endpoint in the same execution context.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        inbound = headers.get(REQUEST_ID_HEADER)
        request_id = inbound.decode() if inbound else str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].append((REQUEST_ID_HEADER, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.clear_contextvars()
