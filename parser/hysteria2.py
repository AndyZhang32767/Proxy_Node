"""Hysteria2 share-link parser.

Format: hysteria2://password@host:port?query#name
Also accepts: hy2://password@host:port?query#name

Query parameters:
- insecure: 0/1 — skip TLS verification
- sni: TLS SNI
- alpn: ALPN protocols
- upmbps: upload bandwidth in Mbps
- downmbps: download bandwidth in Mbps
- obfs: obfuscation password
- obfs-password: same as obfs
- pinSHA256: TLS certificate pin
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

from models.node import ProxyNode, ProxyType
from .base import strip_fragment, parse_common_params


def parse_hysteria2_link(link: str) -> Optional[ProxyNode]:
    """Parse a Hysteria2 share link (hysteria2:// or hy2://)."""
    link = link.strip()
    if link.startswith("hy2://"):
        link = "hysteria2://" + link[len("hy2://"):]
    if not link.startswith("hysteria2://"):
        return None

    raw = link[len("hysteria2://"):]
    raw_no_frag, name = strip_fragment(raw)
    parse_url = f"hysteria2://{raw_no_frag}"

    try:
        parsed = urlparse(parse_url)
    except Exception:
        return None

    node = ProxyNode(proxy_type=ProxyType.HYSTERIA2, name=name)

    # Password: everything before @ is the password
    # URL format: hysteria2://PASSWORD@host:port?query
    at_pos = raw_no_frag.find("@")
    if at_pos > 0:
        node.password = unquote(raw_no_frag[:at_pos])

    # Host & port
    node.host = parsed.hostname or ""
    node.port = parsed.port or 443

    # Always TLS for Hysteria2
    node.tls = True

    # Parse query parameters
    if parsed.query:
        qs = parse_qs(parsed.query)
        params = parse_common_params(parsed.query)
        _apply_params(node, params, qs)

    if not node.name:
        node.name = f"HY2-{node.host}"

    return node


def _apply_params(node: ProxyNode, params: dict, qs: dict) -> None:
    """Apply Hysteria2-specific params."""
    # Common transport
    if "tls" in params:
        node.tls = params["tls"]
    if "sni" in params:
        node.sni = params["sni"]
    if "alpn" in params:
        node.alpn = params["alpn"]
    if "allow_insecure" in params:
        node.allow_insecure = params["allow_insecure"]

    # Bandwidth
    for key in ("upmbps", "up"):
        if key in qs:
            try:
                node.up_mbps = int(qs[key][0])
            except (ValueError, TypeError):
                pass
    for key in ("downmbps", "down"):
        if key in qs:
            try:
                node.down_mbps = int(qs[key][0])
            except (ValueError, TypeError):
                pass

    # Obfuscation
    for key in ("obfs", "obfs-password"):
        if key in qs:
            node.obfs = qs[key][0]

    # Insecure
    if "insecure" in qs:
        node.allow_insecure = qs["insecure"][0] in ("1", "true")
