"""The standard-library TLS channel for pytds (`app/sqlsource/tds_tls.py` — F4 addendum, RULED C).

What is proven WITHOUT a SQL Server: a REAL TLS handshake runs through the shim's `establish_channel`
over a fake TDS reader/writer (the PRELOGIN framing stripped, the bytes forwarded raw) against a
standard-library TLS server with a self-signed certificate; the encrypted transport round-trips data;
a host-name mismatch and an untrusted CA both refuse; `install()` is idempotent and `check_internals()`
holds on the pinned pytds — the guard that turns a pytds bump that moves an internal into a test
failure here instead of a customer's connection.

Run:   cd api && python -m pytest tests/test_sql_source_tds_tls.py -q
"""
from __future__ import annotations

import datetime as dt
import importlib.metadata
import os
import socket
import ssl
import threading

import pytest
import pytds.tds_base as tds_base

from app.sqlsource import tds_tls


# ---------------------------------------------------------------------------------------------
# a self-signed certificate (its own CA) for sql01.city.local
# ---------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    d = tmp_path_factory.mktemp("tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sql01.city.local")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("sql01.city.local")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = d / "server.pem", d / "server.key"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    # an unrelated CA, for the "untrusted" leg
    key2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name2 = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Some Other CA")])
    other = (x509.CertificateBuilder().subject_name(name2).issuer_name(name2).public_key(key2.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
             .not_valid_after(now + dt.timedelta(days=2))
             .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key2, hashes.SHA256()))
    otherfile = d / "other.pem"
    otherfile.write_bytes(other.public_bytes(serialization.Encoding.PEM))
    return {"cert": str(certfile), "key": str(keyfile), "other_ca": str(otherfile)}


# ---------------------------------------------------------------------------------------------
# a fake TDS reader/writer: PRELOGIN framing stripped, bytes forwarded raw over a socketpair
# ---------------------------------------------------------------------------------------------
class _Meta:
    def __init__(self, type_: int):
        self.type = type_


class FakeWriter:
    def __init__(self, sock: socket.socket):
        self._transport = sock
        self._buf = bytearray()
        self.packets: list[int] = []

    def begin_packet(self, packet_type: int) -> None:
        self.packets.append(packet_type)

    def write(self, data: bytes) -> None:
        self._buf += data

    def flush(self) -> None:
        self._transport.sendall(bytes(self._buf))
        self._buf.clear()


class FakeReader:
    def __init__(self, sock: socket.socket):
        self._transport = sock
        self._pending = b""

    def begin_response(self):
        self._pending = self._transport.recv(65536)      # one server flight per PRELOGIN response
        return _Meta(tds_base.PacketType.PRELOGIN)

    def stream_finished(self) -> bool:
        return not self._pending

    def recv(self, size: int) -> bytes:
        out, self._pending = self._pending[:size], self._pending[size:]
        return out


class FakeConn:
    def __init__(self, sock, login):
        self.sock = sock
        self._login = login


class FakeSession:
    def __init__(self, sock: socket.socket, login):
        self._writer = FakeWriter(sock)
        self._reader = FakeReader(sock)
        self.conn = FakeConn(sock, login)


def _server(certs, server_sock: socket.socket, result: dict) -> None:
    """A stdlib TLS server on its end of the socketpair: handshake, echo one message upper-cased."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certs["cert"], certs["key"])
    try:
        with ctx.wrap_socket(server_sock, server_side=True) as s:
            s.settimeout(5)
            data = s.recv(4096)
            s.sendall(data.upper())
            result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"


def _login(cafile: str, host: str = "sql01.city.local", validate: bool = True):
    login = tds_base._TdsLogin()
    login.server_name = host
    login.validate_host = validate
    login.tls_ctx = tds_tls.create_context(cafile)
    return login


def _handshake(certs, cafile, host="sql01.city.local", validate=True):
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    result: dict = {}
    t = threading.Thread(target=_server, args=(certs, b, result), daemon=True)
    t.start()
    sess = FakeSession(a, _login(cafile, host, validate))
    tds_tls.establish_channel(sess)
    return sess, t, result


# ---------------------------------------------------------------------------------------------
def test_pin_and_internals():
    assert importlib.metadata.version("python-tds") == tds_tls.PINNED
    tds_tls.check_internals()


def test_install_is_idempotent_and_patches_pytds():
    import pytds.tls as ptls
    assert tds_tls.install() is True
    assert ptls.establish_channel is tds_tls.establish_channel
    assert ptls.create_context is tds_tls.create_context
    assert ptls.OPENSSL_AVAILABLE is True
    assert tds_tls.install() is True      # second call: no-op, still active
    tds_tls.check_internals()             # still honest after the swap (reads the captured original source)


def test_real_handshake_and_round_trip_through_the_fake_tds_framing(certs):
    sess, t, result = _handshake(certs, certs["cert"])
    enc = sess.conn.sock
    assert isinstance(enc, tds_tls.EncryptedSocket)
    assert sess._writer._transport is enc and sess._reader._transport is enc
    assert all(p == tds_base.PacketType.PRELOGIN for p in sess._writer.packets) and len(sess._writer.packets) >= 1
    enc.sendall(bytearray(b"ping over tds"))         # pytds hands bytearrays to the transport
    got = enc.recv(4096)
    assert got == b"PING OVER TDS"
    t.join(2)
    assert result.get("ok"), result
    assert enc.recv(4096) == b""                      # the server closed: a clean EOF, not an exception
    enc.close()                                       # and closing after the peer is gone never raises


def test_host_name_mismatch_is_refused(certs):
    with pytest.raises(tds_base.Error) as exc:
        _handshake(certs, certs["cert"], host="not-the-server.example")
    assert "host name" in str(exc.value).lower() or "verification failed" in str(exc.value).lower()


def test_untrusted_ca_is_refused(certs):
    with pytest.raises(tds_base.Error):
        _handshake(certs, certs["other_ca"])


def test_require_mode_skips_the_host_name_check_but_still_verifies_the_chain(certs):
    sess, t, result = _handshake(certs, certs["cert"], host="whatever.internal", validate=False)
    assert isinstance(sess.conn.sock, tds_tls.EncryptedSocket)
    with pytest.raises(tds_base.Error):
        _handshake(certs, certs["other_ca"], host="whatever.internal", validate=False)


def test_revert_to_clear_restores_the_plain_transport(certs):
    sess, t, result = _handshake(certs, certs["cert"])
    plain = sess.conn.sock._transport
    tds_tls.revert_to_clear(sess)
    assert sess.conn.sock is plain and sess._writer._transport is plain and sess._reader._transport is plain


def test_shim_is_a_stdlib_leaf():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(tds_tls.__file__).read_text())
    allowed = {"__future__", "importlib", "inspect", "logging", "ssl", "typing", "pytds"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in allowed, node.module


# ---------------------------------------------------------------------------------------------
# the routing guard (security review 2026-10-01)
# ---------------------------------------------------------------------------------------------
def test_routing_guard_refuses_a_reconnect_without_the_pinned_socket():
    calls = []
    def original(**kw):
        calls.append(kw)
        return "connected"
    guarded = tds_tls._guarded_connect(original)
    assert guarded(host="sql01", port=1433, sock=object()) == "connected"      # the pinned first hop passes through
    with pytest.raises(tds_base.Error) as exc:
        guarded(host="keycloak-postgres", port=5432, sock=None)                # pytds's route recursion
    assert tds_tls.ROUTING_REFUSED in str(exc.value) and len(calls) == 1


def test_routing_guard_installs_into_pytds_and_is_idempotent():
    import pytds
    tds_tls.install_routing_guard()
    first = pytds._connect
    assert getattr(first, "__wrapped__", None) is not None
    tds_tls.install_routing_guard()
    assert pytds._connect is first
    tds_tls.check_internals()      # the guard keeps the internals check honest (reads __wrapped__)


def test_routing_refusal_maps_to_address_denied_without_the_route_target():
    from app.sqlsource import dialects as d
    info = d.ConnInfo("sql01.city.local", 1433, "citydata", "talus_ro", "pw")
    out = d._ms_error(tds_base.Error(f"{tds_tls.ROUTING_REFUSED}: the server asked to redirect the connection to 169.254.169.254:80"), info)
    assert out.reason == "address_denied" and "169.254" not in str(out)
