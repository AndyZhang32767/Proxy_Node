"""TUIC share-link parser.

Format: tuic://uuid:password@host:port?query#name

Query parameters:
- congestion_control: bbr / cubic / new_reno
- alpn: ALPN protocols
- sni: TLS SNI
- insecure: 0/1 — skip TLS verification
- disable_sni: 0/1
- udp_relay_mode: native / quic
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

from models.node import ProxyNode, ProxyType
from .base import strip_fragment, parse_common_params


def parse_tuic_link(link: str) -> Optional[ProxyNode]:
    """Parse a TUIC share link (tuic://)."""
    link = link.strip()
    if not link.startswith("tuic://"):
        return None

    raw = link[len("tuic://"):]
    raw_no_frag, name = strip_fragment(raw)
    parse_url = f"tuic://{raw_no_frag}"

    try:
        parsed = urlparse(parse_url)
    except Exception:
        return None

    node = ProxyNode(proxy_type=ProxyType.TUIC, name=name)

    # Userinfo: uuid:password — everything before @
    at_pos = raw_no_frag.find("@")
    userinfo = unquote(raw_no_frag[:at_pos]) if at_pos > 0 else ""
    if ":" in userinfo:
        node.uuid, node.tuic_password = userinfo.split(":", 1)
    else:
        node.uuid = userinfo

    # Host & port
    node.host = parsed.hostname or ""
    node.port = parsed.port or 443

    # Always TLS for TUIC
    node.tls = True

    # Parse query parameters
    if parsed.query:
        qs = parse_qs(parsed.query)
        params = parse_common_params(parsed.query)
        _apply_params(node, params, qs)

    if not node.name:
        node.name = f"TUIC-{node.host}"

    return node


def _apply_params(node: ProxyNode, params: dict, qs: dict) -> None:
    """Apply TUIC-specific params."""
    # TLS
    if "tls" in params:
        node.tls = params["tls"]
    if "sni" in params:
        node.sni = params["sni"]
    if "alpn" in params:
        node.alpn = params["alpn"]
    if "allow_insecure" in params:
        node.allow_insecure = params["allow_insecure"]

    # Congestion control
    if "congestion_control" in qs:
        node.congestion_control = qs["congestion_control"][0]

    # Insecure
    if "insecure" in qs:
        node.allow_insecure = qs["insecure"][0] in ("1", "true")
