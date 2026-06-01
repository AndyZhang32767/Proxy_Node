"""VLESS share-link parser.

Format: vless://uuid@host:port?query#name

Query parameters:
- type: transport (tcp, ws, grpc, kcp, quic)
- path: WebSocket/gRPC path
- host: WebSocket host header
- encryption: "none" for VLESS
- security: "tls" or "reality"
- sni: TLS SNI
- alpn: ALPN
- fp: TLS fingerprint
- flow: flow control (xtls-rprx-vision, xtls-rprx-vision-udp443, etc.)
- pbk: Reality public key
- sid: Reality short ID
- spx: Reality spider X
- allowInsecure: skip TLS verification
- serviceName: gRPC service name
- headerType: obfuscation header type
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

from models.node import ProxyNode, ProxyType
from .base import strip_fragment, parse_common_params


def parse_vless_link(link: str) -> Optional[ProxyNode]:
    """Parse a VLESS share link (vless://)."""
    link = link.strip()
    if not link.startswith("vless://"):
        return None

    # Extract fragment manually
    raw = link[8:]
    raw_no_frag, name = strip_fragment(raw)

    parse_url = f"vless://{raw_no_frag}"

    try:
        parsed = urlparse(parse_url)
    except Exception:
        return None

    node = ProxyNode(proxy_type=ProxyType.VLESS, name=name)

    # User ID (UUID)
    node.uuid = unquote(parsed.username or "")

    # Host & port
    node.host = parsed.hostname or ""
    node.port = parsed.port or 443

    # Parse query parameters
    if parsed.query:
        params = parse_common_params(parsed.query)
        qs = parse_qs(parsed.query)
        _apply_params(node, params, qs)

    if not node.name:
        node.name = f"VLESS-{node.host}"

    return node


def _apply_params(node: ProxyNode, params: dict, qs: dict) -> None:
    """Apply parsed transport and VLESS-specific params."""
    # Transport
    if "network" in params:
        node.network = params["network"]
    if "ws_path" in params:
        node.ws_path = params["ws_path"]
    if "ws_host" in params:
        node.ws_host = params["ws_host"]
    if "tls" in params:
        node.tls = params["tls"]
    if "sni" in params:
        node.sni = params["sni"]
    if "alpn" in params:
        node.alpn = params["alpn"]
    if "fingerprint" in params:
        node.fingerprint = params["fingerprint"]
    if "allow_insecure" in params:
        node.allow_insecure = params["allow_insecure"]

    # VLESS-specific: security = tls / reality
    if "security" in qs:
        sec = qs["security"][0].lower()
        if sec in ("tls", "reality"):
            node.tls = True
        if sec == "reality":
            node.reality = True

    # Reality params
    if "pbk" in qs:
        node.reality_pbk = qs["pbk"][0]
    if "sid" in qs:
        node.reality_sid = qs["sid"][0]
    if "spx" in qs:
        node.reality_spx = qs["spx"][0]

    # Flow control
    if "flow" in qs:
        node.flow = qs["flow"][0]

    # gRPC service name (may override ws_path)
    if "serviceName" in qs:
        node.ws_path = qs["serviceName"][0]
