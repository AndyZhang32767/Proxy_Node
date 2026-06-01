"""Shadowsocks share-link parser.

Formats supported:
- Legacy: ss://BASE64(method:password@host:port)#name
- SIP002: ss://BASE64(method:password)@host:port?plugin=...#name
- SIP002 with query: ss://BASE64(method:password)@host:port?plugin=obfs-local%3Bobfs%3Dhttp#name
"""

from __future__ import annotations

import base64
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

from models.node import ProxyNode, ProxyType
from .base import strip_fragment, try_decode_base64, parse_common_params


def parse_ss_link(link: str) -> Optional[ProxyNode]:
    """Parse a Shadowsocks share link (ss://).

    Two main formats:
    1. Legacy: ss://BASE64(method:password@host:port)#name
       The entire URI after ss:// is base64 encoded.
    2. SIP002: ss://BASE64(method:password)@host:port?query#name
       Only the userinfo part is base64 encoded.
    """
    link = link.strip()
    if not link.startswith("ss://"):
        return None

    # Remove scheme
    raw = link[5:]
    if not raw:
        return None

    # Extract fragment (name)
    raw_no_frag, name = strip_fragment(raw)

    node = ProxyNode(proxy_type=ProxyType.SS, name=name)

    # Detect format: SIP002 uses userinfo@host:port format
    # Legacy format is entirely base64-encoded
    if "@" in raw_no_frag:
        # SIP002 format: BASE64(method:password)@host:port?query
        userinfo, _, hostpart = raw_no_frag.partition("@")
        userinfo = try_decode_base64(userinfo)

        if ":" in userinfo:
            node.method, node.password = userinfo.split(":", 1)

        # Parse host:port
        if "?" in hostpart:
            hostport, query = hostpart.split("?", 1)
            params = parse_common_params(query)
            _apply_params(node, params)

            # Plugin (obfs-local, v2ray-plugin, etc.)
            qs = parse_qs(query)
            if "plugin" in qs:
                plugin_str = unquote(qs["plugin"][0])
                _parse_plugin(node, plugin_str)
        else:
            hostport = hostpart

        if ":" in hostport:
            node.host = hostport.rsplit(":", 1)[0].strip("[]")
            try:
                node.port = int(hostport.rsplit(":", 1)[1])
            except ValueError:
                node.port = 8388
        else:
            node.host = hostport
            node.port = 8388
    else:
        # Legacy format: everything is base64-encoded
        decoded = try_decode_base64(raw_no_frag)

        # Pattern: method:password@host:port
        match = re.match(
            r"^([a-zA-Z0-9\-_.]+):(.+?)@(.+?):(\d+)$", decoded
        )
        if match:
            node.method = match.group(1)
            node.password = match.group(2)
            node.host = match.group(3).strip("[]")
            try:
                node.port = int(match.group(4))
            except ValueError:
                node.port = 8388

    if not node.name:
        node.name = f"SS-{node.host}"

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
        node.tls = params["tls"]
    if "sni" in params:
        node.sni = params["sni"]
    if "alpn" in params:
        node.alpn = params["alpn"]
    if "fingerprint" in params:
        node.fingerprint = params["fingerprint"]
    if "allow_insecure" in params:
        node.allow_insecure = params["allow_insecure"]


def _parse_plugin(node: ProxyNode, plugin_str: str) -> None:
    """Parse plugin string like 'obfs-local;obfs=http;obfs-host=example.com'"""
    parts = plugin_str.split(";")
    for part in parts:
        if "=" in part:
            key, val = part.split("=", 1)
            if key == "obfs":
                if val == "tls":
                    node.tls = True
            elif key == "obfs-host":
                node.ws_host = val
            elif key == "path":
                node.ws_path = val
            elif key == "host":
                node.ws_host = val
            elif key == "tls" and val.lower() in ("true", "1"):
                node.tls = True
