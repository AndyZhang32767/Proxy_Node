"""Share-link parser: auto-detect protocol and parse.

Handles: ss://, vmess://, trojan://, vless://
"""

from __future__ import annotations

from typing import Optional

from models.node import ProxyNode, ProxyType
from .base import detect_protocol, is_valid_share_link
from .ss import parse_ss_link
from .vmess import parse_vmess_link
from .trojan import parse_trojan_link
from .vless import parse_vless_link
from .hysteria2 import parse_hysteria2_link
from .tuic import parse_tuic_link


def parse_share_link(link: str) -> Optional[ProxyNode]:
    """Parse any supported share link into a ProxyNode.

    Auto-detects the protocol and dispatches to the appropriate parser.
    Returns None if the link is invalid or unsupported.
    """
    link = link.strip()
    if not link:
        return None

    proto = detect_protocol(link)
    if proto is None:
        return None

    parsers = {
        ProxyType.SS: parse_ss_link,
        ProxyType.VMESS: parse_vmess_link,
        ProxyType.TROJAN: parse_trojan_link,
        ProxyType.VLESS: parse_vless_link,
        ProxyType.HYSTERIA2: parse_hysteria2_link,
        ProxyType.TUIC: parse_tuic_link,
    }

    parser = parsers.get(proto)
    if parser is None:
        return None

    try:
        return parser(link)
    except Exception:
        return None


def parse_multiple_links(text: str) -> list[ProxyNode]:
    """Parse multiple share links from text (one per line).

    Returns list of successfully parsed nodes. Failed lines are silently skipped.
    """
    nodes: list[ProxyNode] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        node = parse_share_link(line)
        if node:
            nodes.append(node)
    return nodes


__all__ = [
    "parse_share_link",
    "parse_multiple_links",
    "is_valid_share_link",
    "detect_protocol",
]
