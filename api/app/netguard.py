"""Outbound address guard — the ONE classifier for where Talus may open a server-side connection.

Core (`decision-code-boundary.md` §4 row 9; moved out of `paid/api/reports/egress.py` on
2026-10-01 for the SQL-source pull, `decision-sql-source-pull.md` Licence). A security fence
must exist once: the report-delivery webhooks (paid) and the SQL-source connections (core)
both judge a destination here, so the two can never drift apart.

This module is a LEAF: stdlib only, nothing from `app/` or `paid/`. A destination reaches it
only from its caller — an admin-typed host, already stored — and the module can never grow a
path that hands it an address from anywhere else (`security-operating-doc.md` A.1: naming an
input untrusted, then trusting a value out of it).

Two steps, every time, at create AND at every use (DNS rebinding):

1. `is_denied_address` — deny-by-default. Anything that is not plain global unicast is refused,
   and any v6 form that WRAPS a v4 address (IPv4-mapped, NAT64, 6to4, Teredo) is judged by the
   address it wraps. `ipaddress` calls multicast, NAT64, `fec0::` and `2001:20::` *global*, so
   those are explicit.
2. `resolve_host` — resolve the name, then refuse unless EVERY returned address is allowed;
   return the single address the caller pins its socket to. An IP literal never touches DNS.

`allow_private` relaxes ONLY the private classes — RFC 1918, ULA, CGNAT — for an on-prem
destination (a webhook receiver behind the delivery allow-list switch, or a database on the
City LAN). Loopback, link-local (the cloud metadata address lives there), unspecified,
multicast, broadcast and reserved/documentation ranges are refused regardless.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable

Resolver = Callable[[str, int], Awaitable[list[str]]]


class EgressRefused(ValueError):
    """The destination failed the fence. `reason` is a stable code for callers/tests; the message
    is safe to show an admin (host-only, never a path or a secret)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------
# 1. Classify
# ---------------------------------------------------------------------------------------------
def is_denied_address(ip_text: str, *, allow_private: bool = False) -> bool:
    """True when an address must never be connected to. See the module docstring for the rule;
    a wrapped v6 address is judged by its inner address under the same rule."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True

    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is None and ip.sixtofour is not None:
            inner = ip.sixtofour
        if inner is None and ip.teredo is not None:
            server, client = ip.teredo
            return (is_denied_address(str(server), allow_private=allow_private)
                    or is_denied_address(str(client), allow_private=allow_private))
        if inner is not None:
            return is_denied_address(str(inner), allow_private=allow_private)

    # The classes the switch never opens.
    if ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in n for n in _V6_DENY):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and (ip in _BROADCAST or any(ip in n for n in _V4_NEVER)):
        return True
    # The classes the switch may open: RFC 1918 / ULA / CGNAT.
    if any(ip in n for n in _PRIVATE_CLASSES):
        return not allow_private
    if ip.is_private:
        return True
    return not ip.is_global


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# What `allow_private` may open — and nothing else.
_PRIVATE_CLASSES = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                          "100.64.0.0/10", "fc00::/7"))
# IPv4 ranges `is_private` covers that the switch must NOT open (documentation/test/"this" networks).
_V4_NEVER = tuple(ipaddress.ip_network(n) for n in ("0.0.0.0/8", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15",
                                                    "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4"))
# Non-routable v6 ranges `ipaddress` calls global (delivery-targets security review, f6):
# deprecated site-local and ORCHIDv2.
_V6_DENY = (ipaddress.ip_network("fec0::/10"), ipaddress.ip_network("2001:20::/28"))
_BROADCAST = ipaddress.ip_network("255.255.255.255/32")


# ---------------------------------------------------------------------------------------------
# 2. Resolve, then deny
# ---------------------------------------------------------------------------------------------
async def default_resolver(host: str, port: int) -> list[str]:
    """The production DNS seam: the event loop's getaddrinfo, TCP only, both families."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if addr not in out:
            out.append(addr)
    return out


async def resolve_host(host: str, port: int, *, resolver: Resolver = default_resolver,
                       allow_private: bool = False, what: str = "destination") -> str:
    """Resolve `host` and refuse unless EVERY address is allowed. Returns the single address to
    pin the connection to. An IP literal never touches DNS. `what` names the destination kind in
    the admin-facing message ("delivery target", "database host") — never the path or a secret."""
    if is_ip_literal(host):
        if is_denied_address(host, allow_private=allow_private):
            raise EgressRefused("address_denied", f"'{host}' is not a public address")
        return host

    try:
        addrs = await resolver(host, port)
    except (socket.gaierror, OSError, asyncio.TimeoutError) as exc:
        raise EgressRefused("dns_failed", f"'{host}' did not resolve: {type(exc).__name__}")
    if not addrs:
        raise EgressRefused("dns_failed", f"'{host}' did not resolve to any address")

    for addr in addrs:
        if is_denied_address(addr, allow_private=allow_private):
            # Any private/special answer refuses the whole name — never "pick the public one".
            raise EgressRefused(
                "address_denied",
                f"'{host}' resolves to a private, local or reserved address and cannot be a {what}",
            )
    return addrs[0]
