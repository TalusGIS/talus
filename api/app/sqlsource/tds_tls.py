"""A standard-library TLS channel for pytds (decision-sql-source-pull.md §8, F4 addendum — RULED C, 2026-10-01).

pytds encrypts the TDS stream only through pyOpenSSL, and every pyOpenSSL release caps `cryptography<47`
while this image pins `cryptography>=50`. This module is the same channel pytds implements in
`pytds/tls.py` (the TLS handshake carried inside TDS PRELOGIN packets, then a socket wrapper that runs
the rest of the stream through the TLS session), rebuilt on `ssl.MemoryBIO` + `ssl.SSLObject`, which the
standard library has had since 3.5. `install()` swaps the five symbols pytds's own code reaches through
`pytds.tls` at call time; nothing in pytds is edited.

★ Temporary scaffolding with a defined end: the day a pyOpenSSL release accepts the pinned
`cryptography`, delete this module, drop the pin, add pyOpenSSL back to requirements — pytds's own path
takes over unchanged (the dialect calls `install()` and would simply find nothing to do).

★ Care and feeding is confined to pytds version bumps: `PINNED` is the exact version this was written
against, `check_internals()` asserts every pytds attribute the shim touches, and
`api/tests/test_sql_source_tds_tls.py` drives a REAL handshake through a fake TDS reader/writer against a
stdlib TLS server. A bump that moves any of it fails the test suite, never a customer's connection.

Host-name checking is the standard library's (`check_hostname` + `server_hostname`), which is stricter
than pytds's hand-rolled SAN matcher; chain verification is `CERT_REQUIRED` against the bundle the caller
hands in, exactly as pytds's `create_context` did with pyOpenSSL.
"""
from __future__ import annotations

import importlib.metadata
import logging
import ssl
from typing import Any

import pytds.tds_base as tds_base
import pytds.tls as _ptls

log = logging.getLogger(__name__)

PINNED = "1.17.1"          # the pytds version these internals were read from (requirements.txt pins it exactly)
BUFSIZE = 65536
_INSTALLED = False
# pytds's own channel source, captured before install() replaces it (so check_internals() stays honest afterwards).
_ORIGINAL_SRC = ""
try:
    import inspect as _inspect
    _ORIGINAL_SRC = _inspect.getsource(_ptls.establish_channel) + _inspect.getsource(_ptls.revert_to_clear)
except Exception:  # noqa: BLE001
    pass


class TdsInternalsChanged(RuntimeError):
    """pytds no longer looks like the version this shim was written against — do not guess."""


def check_internals() -> None:
    """Every pytds attribute the shim reaches. Called by `install()` and by the unit test."""
    v = importlib.metadata.version("python-tds")
    if v != PINNED:
        raise TdsInternalsChanged(f"python-tds {v} is installed; this shim was written against {PINNED} (requirements.txt pins it)")
    from pytds.tds_reader import _TdsReader
    from pytds.tds_session import _TdsSession
    from pytds.tds_writer import _TdsWriter
    login = tds_base._TdsLogin()
    for a in ("tls_ctx", "server_name", "validate_host", "enc_flag", "server_enc_flag", "enc_login_only", "cafile"):
        if not hasattr(login, a):
            raise TdsInternalsChanged(f"_TdsLogin has no '{a}'")
    for mod, names in ((_ptls, ("OPENSSL_AVAILABLE", "create_context", "establish_channel", "revert_to_clear", "EncryptedSocket")),
                       (_TdsWriter, ("begin_packet", "write", "flush")),
                       (_TdsReader, ("begin_response", "stream_finished", "recv"))):
        for n in names:
            if not hasattr(mod, n):
                raise TdsInternalsChanged(f"{getattr(mod, '__name__', mod)} has no '{n}'")
    import inspect
    # The attribute paths the shim walks are the ones pytds's OWN channel walks: read them from its source,
    # so the invariant is "pytds still reaches the session this way", not a guess about its constructor.
    src = inspect.getsource(_ptls.establish_channel) + inspect.getsource(_ptls.revert_to_clear) \
        if _ptls.establish_channel is not establish_channel else _ORIGINAL_SRC
    for n in ("tds_sock._writer", "tds_sock._reader", "tds_sock.conn._login", "tds_sock.conn.sock",
              "_writer._transport", "_reader._transport", "login.tls_ctx", "login.server_name", "login.validate_host"):
        if n not in src:
            raise TdsInternalsChanged(f"pytds.tls no longer reaches '{n}'")
    assert _TdsSession  # imported for the rename-guard above
    import pytds
    sig = inspect.signature(getattr(pytds._connect, "__wrapped__", pytds._connect))
    for n in ("host", "port", "sock"):
        if n not in sig.parameters:
            raise TdsInternalsChanged(f"pytds._connect has no '{n}' parameter")
    csrc = inspect.getsource(getattr(pytds._connect, "__wrapped__", pytds._connect))
    if "sock=None" not in csrc or "route" not in csrc:
        raise TdsInternalsChanged("pytds._connect no longer reconnects on a route with sock=None — re-read the routing guard")
    if tds_base.PacketType.PRELOGIN != 18:
        raise TdsInternalsChanged("PRELOGIN packet type changed")
    if not hasattr(tds_base, "TransportProtocol"):
        raise TdsInternalsChanged("TransportProtocol missing")


# ---------------------------------------------------------------------------------------------
# The five symbols, on the standard library
# ---------------------------------------------------------------------------------------------
def create_context(cafile: str) -> ssl.SSLContext:
    """What pytds's `create_context` did: TLS client context, chain verified against `cafile`.
    Host-name checking is decided per handshake from `login.validate_host` (see `establish_channel`)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    ctx.load_verify_locations(cafile=cafile)
    return ctx


class EncryptedSocket(tds_base.TransportProtocol):
    """The transport pytds reads and writes after the handshake: bytes go through the SSLObject and
    its memory BIOs, then over the real socket. Same method surface as pytds's EncryptedSocket."""

    def __init__(self, transport: Any, sslobj: ssl.SSLObject, incoming: ssl.MemoryBIO, outgoing: ssl.MemoryBIO) -> None:
        super().__init__()
        self._transport = transport
        self._sslobj = sslobj
        self._incoming = incoming
        self._outgoing = outgoing

    def gettimeout(self) -> float | None:
        return self._transport.gettimeout()

    def settimeout(self, timeout: float | None) -> None:
        self._transport.settimeout(timeout)

    def _flush_out(self) -> None:
        data = self._outgoing.read()
        if data:
            self._transport.sendall(data)

    def sendall(self, data: Any, flags: int = 0) -> None:
        view = memoryview(bytes(data) if isinstance(data, bytearray) else data)
        while len(view):
            n = self._sslobj.write(view)
            self._flush_out()
            view = view[n:]

    def recv_into(self, buffer: bytearray | memoryview, size: int = 0, flags: int = 0) -> int:
        if size == 0:
            size = len(buffer)
        res = self.recv(size)
        buffer[0:len(res)] = res
        return len(res)

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        while True:
            try:
                return self._sslobj.read(bufsize)
            except ssl.SSLWantReadError:
                self._flush_out()
                chunk = self._transport.recv(BUFSIZE)
                if not chunk:
                    return b""
                self._incoming.write(chunk)
            except ssl.SSLZeroReturnError:
                return b""

    def _quiet_unwrap(self) -> None:
        # A closing connection never raises: the peer may already be gone (BrokenPipe on the flush).
        try:
            self._sslobj.unwrap()
        except (ssl.SSLError, OSError):
            pass
        try:
            self._flush_out()
        except OSError:
            pass

    def close(self) -> None:
        self._quiet_unwrap()
        try:
            self._transport.close()
        except OSError:
            pass

    def shutdown(self, how: int = 0) -> None:
        self._quiet_unwrap()


def establish_channel(tds_sock: Any) -> None:
    """pytds's `establish_channel` on the standard library: run the TLS handshake with the handshake
    bytes carried inside TDS PRELOGIN packets (the writer/reader), then replace the session's transport
    with the encrypted socket."""
    w = tds_sock._writer
    r = tds_sock._reader
    login = tds_sock.conn._login
    ctx: ssl.SSLContext | None = login.tls_ctx
    if ctx is None:
        raise tds_base.Error("login.tls_ctx is not set unexpectedly")
    host = login.server_name
    # pytds decides host-name checking per login (`validate_host`); the stdlib decides it per context.
    ctx.check_hostname = bool(login.validate_host)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    sslobj = ctx.wrap_bio(incoming, outgoing, server_side=False, server_hostname=host if login.validate_host else None)
    log.info("tds_tls: TLS handshake (stdlib ssl) with %s", host)
    while True:
        try:
            sslobj.do_handshake()
        except ssl.SSLWantReadError:
            req = outgoing.read()
            if req:
                w.begin_packet(tds_base.PacketType.PRELOGIN)
                w.write(req)
                w.flush()
            resp_meta = r.begin_response()
            if resp_meta.type != tds_base.PacketType.PRELOGIN:
                raise tds_base.Error(f"Invalid packet type was received from server, expected PRELOGIN(18) got {resp_meta.type}")
            while not r.stream_finished():
                incoming.write(r.recv(4096))
        except ssl.SSLCertVerificationError as exc:
            # The stdlib's own words are fine for a log; the dialect maps the class to a fixed sentence.
            raise tds_base.Error(f"Certificate verification failed for host name '{host}': {exc.verify_message if hasattr(exc, 'verify_message') else exc}") from None
        except ssl.SSLError as exc:
            raise tds_base.Error(f"TLS handshake failed: {type(exc).__name__}") from None
        else:
            # The handshake's last flight (client Finished) may still be sitting in the outgoing BIO.
            tail = outgoing.read()
            if tail:
                w.begin_packet(tds_base.PacketType.PRELOGIN)
                w.write(tail)
                w.flush()
            log.info("tds_tls: TLS handshake complete (%s)", sslobj.version())
            enc_sock = EncryptedSocket(transport=tds_sock.conn.sock, sslobj=sslobj, incoming=incoming, outgoing=outgoing)
            tds_sock.conn.sock = enc_sock
            tds_sock._writer._transport = enc_sock
            tds_sock._reader._transport = enc_sock
            return


def revert_to_clear(tds_sock: Any) -> None:
    """pytds's `revert_to_clear`: after an ENCRYPT_OFF login the stream goes back to clear text."""
    enc_conn = tds_sock.conn.sock
    if isinstance(enc_conn, EncryptedSocket):
        clear_conn = enc_conn._transport
        enc_conn.shutdown()
        tds_sock.conn.sock = clear_conn
        tds_sock._writer._transport = clear_conn
        tds_sock._reader._transport = clear_conn


# ---------------------------------------------------------------------------------------------
# The routing guard (security review 2026-10-01): a TDS ENVCHANGE routing token makes pytds close the
# pinned socket and connect AGAIN to a server-supplied host:port with `sock=None` — the one hop the
# address fence never saw. Talus follows no redirects: the guard refuses any `_connect` without the
# pre-connected socket. An availability-group listener that routes read-intent sessions is pointed at
# the readable replica directly instead.
# ---------------------------------------------------------------------------------------------
_ROUTING_INSTALLED = False
ROUTING_REFUSED = "routing refused"


def _guarded_connect(original):
    def guarded(*args, **kwargs):
        sock = kwargs.get("sock")
        if sock is None:
            host, port = kwargs.get("host"), kwargs.get("port")
            log.warning("tds_tls: routing redirect refused host=%s port=%s", host, port)
            raise tds_base.Error(f"{ROUTING_REFUSED}: the server asked to redirect the connection to {host}:{port}; "
                                 "Talus follows no redirects — point the connection at that host directly")
        return original(*args, **kwargs)
    guarded.__wrapped__ = original      # type: ignore[attr-defined]
    return guarded


def install_routing_guard() -> None:
    """Wrap `pytds._connect` (resolved as a module global at every call, including pytds's own
    redirect recursion) so a reconnect without the pinned socket is refused. Idempotent; TLS-independent."""
    global _ROUTING_INSTALLED
    if _ROUTING_INSTALLED:
        return
    import pytds
    check_internals()
    pytds._connect = _guarded_connect(pytds._connect)
    _ROUTING_INSTALLED = True
    log.info("tds_tls: routing guard installed into pytds %s", PINNED)


def install() -> bool:
    """Swap the five symbols into `pytds.tls` (idempotent). Returns True when the shim is active,
    False when pytds's own pyOpenSSL path is available (the shim's exit condition) and nothing was done."""
    global _INSTALLED
    if _INSTALLED:
        return True
    if getattr(_ptls, "OPENSSL_AVAILABLE", False) and getattr(_ptls, "establish_channel", None) is not establish_channel:
        log.info("tds_tls: pyOpenSSL is available — pytds's own TLS path stays; the shim is not needed")
        return False
    check_internals()
    _ptls.create_context = create_context      # type: ignore[assignment]
    _ptls.establish_channel = establish_channel
    _ptls.revert_to_clear = revert_to_clear
    _ptls.EncryptedSocket = EncryptedSocket     # type: ignore[assignment,misc]
    _ptls.OPENSSL_AVAILABLE = True
    _INSTALLED = True
    log.info("tds_tls: stdlib TLS channel installed into pytds %s", PINNED)
    return True
