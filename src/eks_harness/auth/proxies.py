from __future__ import annotations

import ipaddress
import socket

from starlette.requests import HTTPConnection

from eks_harness.config import Config

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def trusted_networks(config: Config) -> list[Network]:
    found = []
    for item in config["server.trustedProxies"] or []:
        try:
            found.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return found


def is_trusted(address: str | None, networks: list[Network]) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def client_ip(connection: HTTPConnection, config: Config) -> str | None:
    peer = connection.client.host if connection.client else None
    networks = trusted_networks(config)
    if not is_trusted(peer, networks):
        return peer
    if config["server.cloudflare"]:
        cf = (connection.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            try:
                return str(ipaddress.ip_address(cf))
            except ValueError:
                pass
    forwarded = connection.headers.get("x-forwarded-for") or ""
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if not is_trusted(hop, networks):
            return hop
    return peer


def client_scheme(connection: HTTPConnection, config: Config) -> str:
    peer = connection.client.host if connection.client else None
    if is_trusted(peer, trusted_networks(config)):
        proto = (connection.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto in ("http", "https"):
            return proto
    return connection.url.scheme


def lan_addresses() -> list[str]:
    found: list[str] = []

    def add(address: str) -> None:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or address in found:
            return
        found.append(address)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return found
