"""Trojan share-link parser.

Format: trojan://password@host:port?query#name

Query parameters:
- type: transport type (tcp, ws, grpc)
- path: WebSocket path
- host: WebSocket host header
- tls/security: always TLS for Trojan
- sni: TLS SNI
- alpn: ALPN protocols
- fp: TLS fingerprint
- allowInsecure: skip TLS verification
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, unquote

from models.node import ProxyNode, ProxyType
from .base import strip_fragment, parse_common_params


def parse_trojan_link(link: str) -> Optional[ProxyNode]:
    """Parse a Trojan share link (trojan://)."""
    link = link.strip()
    if not link.startswith("trojan://"):
        return None

    # Extract fragment manually (urlparse doesn't handle # well in some cases)
    raw = link[9:]
    raw_no_frag, name = strip_fragment(raw)

    # Reconstruct URL for parsing
    parse_url = f"trojan://{raw_no_frag}"

    try:
        parsed = urlparse(parse_url)
    except Exception:
        return None

    node = ProxyNode(proxy_type=ProxyType.TROJAN, name=name)

    # Password
    node.password = unquote(parsed.username or "")

    # Host & port
    node.host = parsed.hostname or ""
    node.port = parsed.port or 443

    # Trojan always uses TLS
    node.tls = True

    # Parse query parameters
    if parsed.query:
        params = parse_common_params(parsed.query)
        _apply_params(node, params)

    if not node.name:
        node.name = f"Trojan-{node.host}"

    return node


def _apply_params(node: ProxyNode, params: dict) -> None:
    """Apply parsed transport params to node."""
    if "network" in params:
        node.network = params["network"]
    if "ws_path" in params:
        node.ws_path = params["ws_path"]
    if "ws_host" in params:
        node.ws_host = params["ws_host"]
    if "tls" in params:
        node.tls = params["tls"]  # Override default if specified
    if "sni" in params:
        node.sni = params["sni"]
    if "alpn" in params:
        node.alpn = params["alpn"]
    if "fingerprint" in params:
        node.fingerprint = params["fingerprint"]
    if "allow_insecure" in params:
        node.allow_insecure = params["allow_insecure"]
