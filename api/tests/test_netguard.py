"""The outbound address guard (`app/netguard.py`) — the ONE SSRF fence, core since 2026-10-01
(`decision-sql-source-pull.md` Licence; `decision-code-boundary.md` §4 row 9). These cases moved
from `paid/api/tests/test_report_egress.py` with the code; that file keeps exercising the same
functions through `egress`'s re-exports, so a drift between the two would show in both suites.

Run:   cd api && python -m pytest tests/test_netguard.py -q
"""
from __future__ import annotations

import ast
import asyncio
import socket
from pathlib import Path

import pytest

from app import netguard

PUBLIC = "93.184.216.34"
_NETGUARD = Path(netguard.__file__)


def _resolver(*ips: str):
    """A stub for the guard's DNS seam: every name resolves to exactly these addresses."""
    async def resolve(host: str, port: int) -> list[str]:
        return list(ips)
    return resolve


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------------------------
# 1. Classification — the whole SSRF fence.
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.8.8",                # loopback
    "10.1.2.3", "172.16.5.5", "172.31.255.255", "192.168.1.1",   # RFC 1918
    "169.254.169.254", "169.254.1.1",        # link-local + cloud metadata
    "100.64.0.1",                            # CGNAT (not private per ipaddress, not global either)
    "0.0.0.0", "0.1.2.3",                    # unspecified / "this" network
    "224.0.0.1", "239.255.255.250",          # multicast (reads as GLOBAL in ipaddress — explicit)
    "255.255.255.255",                       # broadcast
    "203.0.113.5", "192.0.2.1", "198.51.100.1",   # documentation ranges (reserved)
    "::1", "::",                             # v6 loopback / unspecified
    "fd00:ec2::254", "fc00::1",              # ULA + AWS v6 metadata
    "fe80::1",                               # v6 link-local
    "ff02::1",                               # v6 multicast
    "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",   # IPv4-mapped
    "64:ff9b::a00:1",                        # NAT64 wrapping 10.0.0.1 (reads as GLOBAL — explicit)
    "64:ff9b::7f00:1",                       # NAT64 wrapping 127.0.0.1
    "2002:0a00:0001::1",                     # 6to4 wrapping 10.0.0.1
    "fec0::1", "2001:20::1",                 # deprecated site-local + ORCHIDv2 (read as GLOBAL — explicit)
    "not-an-ip", "",                         # garbage is denied, never allowed by accident
])
def test_is_denied_address(ip):
    assert netguard.is_denied_address(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700::1111", "2001:4860:4860::8888"])
def test_is_allowed_address(ip):
    assert netguard.is_denied_address(ip) is False


# `allow_private` relaxes ONLY the private classes, and nothing else.
PRIVATE_OK_WITH_SWITCH = ["10.1.2.3", "172.16.5.5", "192.168.1.1", "100.64.0.1", "fd00::1", "fc00::1",
                          "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:0a00:0001::1", "172.18.0.3"]
STILL_DENIED_WITH_SWITCH = ["127.0.0.1", "127.8.8.8", "169.254.169.254", "169.254.1.1", "0.0.0.0", "0.1.2.3",
                            "224.0.0.1", "255.255.255.255", "203.0.113.5", "192.0.2.1", "::1", "::", "fe80::1",
                            "ff02::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::7f00:1",
                            "fec0::1", "2001:20::1"]


@pytest.mark.parametrize("ip", PRIVATE_OK_WITH_SWITCH)
def test_switch_allows_private_classes(ip):
    assert netguard.is_denied_address(ip) is True                       # default: refused
    assert netguard.is_denied_address(ip, allow_private=True) is False  # switch: allowed


@pytest.mark.parametrize("ip", STILL_DENIED_WITH_SWITCH)
def test_switch_never_opens_loopback_linklocal_metadata_multicast(ip):
    assert netguard.is_denied_address(ip, allow_private=True) is True


def test_is_ip_literal():
    assert netguard.is_ip_literal("10.0.0.1") and netguard.is_ip_literal("::1")
    assert not netguard.is_ip_literal("sql01.city.local") and not netguard.is_ip_literal("")


# ---------------------------------------------------------------------------------------------
# 2. Resolve, then deny.
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fd00:ec2::254", "172.18.0.3"])
def test_resolve_refuses_denied(ip):
    with pytest.raises(netguard.EgressRefused) as exc:
        _run(netguard.resolve_host("sql01.city.local", 1433, resolver=_resolver(ip)))
    assert exc.value.reason == "address_denied"
    assert "sql01.city.local" in str(exc.value)


def test_resolve_refuses_if_ANY_address_is_denied():
    """A name with one public and one private A record is refused outright — never 'pick the
    public one' (an attacker controls which record a resolver returns first)."""
    with pytest.raises(netguard.EgressRefused):
        _run(netguard.resolve_host("hooks.example.org", 443, resolver=_resolver(PUBLIC, "10.0.0.7")))


def test_resolve_refuses_no_addresses_and_dns_failure():
    with pytest.raises(netguard.EgressRefused) as exc:
        _run(netguard.resolve_host("hooks.example.org", 443, resolver=_resolver()))
    assert exc.value.reason == "dns_failed"

    async def boom(host, port):
        raise socket.gaierror(8, "nodename nor servname provided")
    with pytest.raises(netguard.EgressRefused) as exc:
        _run(netguard.resolve_host("hooks.example.org", 443, resolver=boom))
    assert exc.value.reason == "dns_failed"


def test_resolve_ip_literal_is_checked_without_dns():
    """A private IP literal never reaches DNS; a public one resolves to itself."""
    with pytest.raises(netguard.EgressRefused) as exc:
        _run(netguard.resolve_host("10.0.0.5", 443, resolver=_resolver(PUBLIC)))   # resolver must NOT rescue it
    assert exc.value.reason == "address_denied"
    assert _run(netguard.resolve_host(PUBLIC, 443, resolver=_resolver("10.0.0.1"))) == PUBLIC


def test_resolve_ip_literal_private_with_switch():
    """The SQL-source pull's normal case: a LAN database host, allowed only with the switch."""
    with pytest.raises(netguard.EgressRefused):
        _run(netguard.resolve_host("192.168.10.20", 1433))
    assert _run(netguard.resolve_host("192.168.10.20", 1433, allow_private=True)) == "192.168.10.20"
    with pytest.raises(netguard.EgressRefused):   # loopback stays shut even with the switch
        _run(netguard.resolve_host("127.0.0.1", 1433, allow_private=True))


@pytest.mark.parametrize("name", ["openfga", "postgres", "api", "keycloak", "localhost", "talusgis-postgres"])
def test_compose_service_names_are_refused(name):
    """The compose network names resolve to 172.x on the stack — the decision's named negative.
    Even with the private switch on, the api's own siblings must be refused by NAME upstream
    (the SQL-source pull's never-list); here the address class alone refuses them switch-off."""
    with pytest.raises(netguard.EgressRefused):
        _run(netguard.resolve_host(name, 5432, resolver=_resolver("172.18.0.3")))


def test_resolve_passes_public():
    assert _run(netguard.resolve_host("hooks.example.org", 443, resolver=_resolver(PUBLIC))) == PUBLIC


def test_message_names_the_destination_kind_only():
    with pytest.raises(netguard.EgressRefused) as exc:
        _run(netguard.resolve_host("db.city.local", 1433, resolver=_resolver("10.0.0.7"), what="database host"))
    assert str(exc.value).endswith("cannot be a database host")


def test_default_resolver_uses_getaddrinfo_seam():
    """The production resolver is the event loop's getaddrinfo — the test seam replaces only it."""
    assert callable(netguard.default_resolver)
    assert _run(netguard.default_resolver("localhost", 80))  # resolves to something, both families welcome


# ---------------------------------------------------------------------------------------------
# 3. The module is a leaf — stdlib only.
# ---------------------------------------------------------------------------------------------
def test_netguard_is_a_stdlib_leaf():
    tree = ast.parse(_NETGUARD.read_text())
    allowed = {"__future__", "asyncio", "ipaddress", "socket", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            assert node.module.split(".")[0] in allowed, node.module
