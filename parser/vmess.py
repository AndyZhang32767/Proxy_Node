"""VMess share-link parser.

Format: vmess://BASE64(json)

The JSON structure:
{
    "v": "2",                     // version
    "ps": "name",                 // node name
    "add": "host",                // address
    "port": "443",                // port
    "id": "uuid",                 // user ID
    "aid": "0",                   // alter ID
    "scy": "auto",                // security
    "net": "tcp",                 // network: tcp, ws, grpc, h2, quic
    "type": "none",               // obfuscation type (deprecated)
    "host": "",                   // ws host / h2 host
    "path": "",                   // ws path / h2 path / quic key
    "tls": "",                    // "tls" if TLS enabled
    "sni": "",                    // TLS SNI
    "alpn": "",                   // ALPN
    "fp": ""                      // fingerprint
}
"""

from __future__ import annotations

import json
from typing import Optional

from models.node import ProxyNode, ProxyType
from .base import try_decode_base64


def parse_vmess_link(link: str) -> Optional[ProxyNode]:
    """Parse a VMess share link (vmess://)."""
    link = link.strip()
    if not link.startswith("vmess://"):
        return None

    # Remove scheme
    raw = link[8:]
    if not raw:
        return None

    # Decode base64
    decoded = try_decode_base64(raw)

    try:
        data = json.loads(decoded)
    except (json.JSONDecodeError, ValueError):
        return None

    node = ProxyNode(proxy_type=ProxyType.VMESS)

    # Basic fields
    node.name = data.get("ps", "") or data.get("add", "VMess")
    node.host = data.get("add", "")
    node.uuid = data.get("id", "")

    # Port
    try:
        node.port = int(data.get("port", 443))
    except (ValueError, TypeError):
        node.port = 443

    # Security / encryption
    node.security = data.get("scy", "auto")

    # Network / transport
    net = data.get("net", "tcp")
    if net:
        node.network = net

    # Transport type (deprecated, mapped to network)
    type_ = data.get("type", "")
    if type_ == "http" or net == "h2":
        node.network = "h2"

    # WebSocket / HTTP/2 / gRPC settings
    node.ws_path = data.get("path", "")
    node.ws_host = data.get("host", "")

    # TLS
    tls_val = data.get("tls", "")
    if tls_val in ("tls", "1", "true"):
        node.tls = True
    elif tls_val == "reality":
        node.tls = True

    # SNI
    node.sni = data.get("sni", "")

    # ALPN
    alpn = data.get("alpn", "")
    if alpn:
        node.alpn = [a.strip() for a in alpn.split(",") if a.strip()]

    # Fingerprint
    node.fingerprint = data.get("fp", "")

    # Allow insecure
    allow = data.get("allowInsecure", "") or data.get("allowinsecure", "")
    if allow in ("1", "true"):
        node.allow_insecure = True

    # Service name for gRPC
    if net == "grpc" and data.get("path"):
        node.ws_path = data["path"]  # gRPC service name stored in path

    if not node.name:
        node.name = f"VMess-{node.host}"

    return node
