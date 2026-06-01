"""Base parser and protocol detection for share-link imports."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

from models.node import ProxyNode, ProxyType


# Known protocol schemes
SCHEME_MAP: dict[str, ProxyType] = {
    "ss": ProxyType.SS,
    "vmess": ProxyType.VMESS,
    "trojan": ProxyType.TROJAN,
    "vless": ProxyType.VLESS,
    "hysteria2": ProxyType.HYSTERIA2,
    "hy2": ProxyType.HYSTERIA2,
    "tuic": ProxyType.TUIC,
}

# Private IP ranges (CIDR)
PRIVATE_IP_RANGES = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",       # Multicast
    "::1/128",            # IPv6 loopback
    "fc00::/7",           # IPv6 ULA
    "fe80::/10",          # IPv6 link-local
]


def detect_protocol(link: str) -> Optional[ProxyType]:
    """Detect the protocol of a share link by its scheme prefix."""
    link = link.strip()
    for scheme, ptype in SCHEME_MAP.items():
        if link.startswith(f"{scheme}://"):
            return ptype
    return None


def is_valid_share_link(link: str) -> bool:
    """Quick check if a string looks like a share link."""
    return detect_protocol(link.strip()) is not None


def parse_common_params(query_string: str) -> dict:
    """Parse common transport parameters from query string.

    Common params across protocols:
    - type/network: tcp, ws, grpc, h2, quic
    - path: WebSocket/gRPC path
    - host: WebSocket host header
    - tls/security: TLS setting
    - sni: TLS SNI
    - alpn: ALPN protocols
    - fp: TLS fingerprint
    - allowInsecure/allowinsecure: Skip TLS verification
    """
    params = parse_qs(query_string)
    result: dict = {}

    # Network type
    for key in ("type", "network"):
        if key in params:
            result["network"] = params[key][0]

    # WebSocket path
    for key in ("path", "serviceName"):
        if key in params:
            result["ws_path"] = params[key][0]

    # WebSocket host header
    if "host" in params:
        result["ws_host"] = params["host"][0]

    # TLS
    for key in ("tls", "security"):
        if key in params:
            val = params[key][0].lower()
            result["tls"] = val in ("1", "true", "tls", "reality")

    # SNI
    for key in ("sni", "peer", "serverName"):
        if key in params:
            result["sni"] = params[key][0]

    # ALPN
    if "alpn" in params:
        result["alpn"] = params["alpn"][0].split(",")

    # Fingerprint
    if "fp" in params:
        result["fingerprint"] = params["fp"][0]

    # Allow insecure
    for key in ("allowInsecure", "allowinsecure", "verify"):
        if key in params:
            val = params[key][0].lower()
            if val in ("0", "false"):
                result["allow_insecure"] = False
            elif val in ("1", "true"):
                result["allow_insecure"] = True

    return result


def strip_fragment(link: str) -> tuple[str, str]:
    """Extract #fragment (node name) from a link. Returns (link_without_fragment, name)."""
    # URL-decode then re-encode to handle encoded # (%23)
    if "#" in link:
        base, fragment = link.rsplit("#", 1)
        return base, unquote(fragment)
    return link, ""


def try_decode_base64(s: str) -> str:
    """Try to base64-decode a string, falling back to original."""
    import base64
    try:
        # Add padding if needed
        missing = len(s) % 4
        if missing:
            s += "=" * (4 - missing)
        decoded = base64.urlsafe_b64decode(s)
        return decoded.decode("utf-8", errors="replace")
    except Exception:
        return s
