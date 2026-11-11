"""Who is really asking, and over what, when the API sits behind reverse proxies.

A proxy says who it passed a request on for in X-Forwarded-For, and whether the
browser used HTTPS in X-Forwarded-Proto. Anyone can send those headers, so they're
believed only from the proxies named in TRUSTED_PROXY_IPS, and X-Forwarded-For is
read from the right: each trusted proxy appends the address it saw, so the first
address from the right that isn't one of our proxies is the client. Whatever a
client wrote at the left end itself is never reached.

Everything after this middleware (sign-in throttling, the access-request limit,
first-run setup's "this machine only", the HTTPS rule, logs) sees the result as
``request.client`` and ``request.url.scheme``. Run uvicorn with --no-proxy-headers
so this is the only place that decides.
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=8)
def _networks(entries: tuple[str, ...]) -> tuple[Network, ...]:
    found = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry.strip(), strict=False)
        except ValueError:
            continue  # configuration_problems refuses to start with one of these
        if network.prefixlen:
            found.append(network)
    return tuple(found)


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = text.strip().strip('"')
    if text.startswith("["):  # [v6]:port
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:  # v4:port
        text = text.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def trusted(host: str | None, entries: tuple[str, ...]) -> bool:
    address = _address(host or "")
    return address is not None and any(address in network for network in _networks(entries))


def resolve(peer: str | None, forwarded_for: str, forwarded_proto: str, scheme: str,
            entries: tuple[str, ...]) -> tuple[str | None, str]:
    """(client address, scheme) for a connection from ``peer`` carrying these headers."""
    if not entries or not trusted(peer, entries):
        return peer, scheme
    hops = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    protos = [part.strip().lower() for part in forwarded_proto.split(",") if part.strip()]
    client, index = peer, None
    for position in range(len(hops) - 1, -1, -1):
        if _address(hops[position]) is None:
            # Garbage where an address should be: stop, and use the last proxy we trust.
            break
        client, index = str(_address(hops[position])), position
        if not trusted(hops[position], entries):
            break
    if protos:
        # Each proxy appends what it received. Aligned with the addresses, the entry for
        # the client's own hop is the one that counts; otherwise the first proxy's.
        chosen = protos[index] if index is not None and len(protos) == len(hops) else protos[0]
        if chosen in ("http", "https"):
            scheme = chosen
    return client, scheme


class ProxyHeaders:
    """Pure ASGI, so it runs before anything reads the client or the scheme."""

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        entries = tuple(self.settings.trusted_proxy_ips)
        if scope["type"] in ("http", "websocket") and entries:
            headers = {}
            for name, value in scope.get("headers", []):
                if name in (b"x-forwarded-for", b"x-forwarded-proto"):
                    # Repeated headers are one list, in order.
                    key = name.decode("latin-1")
                    headers[key] = (headers[key] + "," if key in headers else "") + value.decode("latin-1")
            peer = scope.get("client")
            client, scheme = resolve(peer[0] if peer else None, headers.get("x-forwarded-for", ""),
                                     headers.get("x-forwarded-proto", ""), scope.get("scheme", "http"), entries)
            if client is not None and (peer is None or client != peer[0]):
                scope = {**scope, "client": (client, 0)}
            if scheme != scope.get("scheme"):
                scope = {**scope, "scheme": scheme if scope["type"] == "http" else ("wss" if scheme == "https" else "ws")}
        await self.app(scope, receive, send)
