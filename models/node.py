"""Proxy node data models with split-routing support."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class ProxyType(str, Enum):
    SS = "ss"
    VMESS = "vmess"
    TROJAN = "trojan"
    VLESS = "vless"
    HYSTERIA2 = "hysteria2"
    TUIC = "tuic"


class NodeStatus(str, Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    TESTING = "testing"
    ERROR = "error"


class ExitMode(str, Enum):
    """Where traffic of a given category exits."""
    LOCAL = "local"       # Direct connection, no proxy
    REMOTE = "remote"     # Route through selected remote node


@dataclass
class ProxyNode:
    """Represents a single proxy node / server."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    proxy_type: ProxyType = ProxyType.VMESS
    host: str = ""
    port: int = 443

    # Protocol-specific fields
    uuid: str = ""          # VMess / VLESS user ID
    password: str = ""      # SS / Trojan password
    method: str = "aes-256-gcm"   # SS encryption method
    security: str = "auto"  # VMess security
    flow: str = ""          # VLESS flow (xtls-rprx-vision, etc.)

    # Transport settings
    network: str = "tcp"    # tcp, ws, grpc, h2, quic
    ws_path: str = ""       # WebSocket path
    ws_host: str = ""       # WebSocket host header
    tls: bool = False
    sni: str = ""           # TLS SNI
    alpn: list[str] = field(default_factory=list)
    fingerprint: str = ""   # TLS fingerprint (chrome, firefox, etc.)
    allow_insecure: bool = False
    # Reality (VLESS)
    reality: bool = False
    reality_pbk: str = ""   # Reality public key
    reality_sid: str = ""   # Reality short ID
    reality_spx: str = ""   # Reality spider X

    # Hysteria2 / TUIC
    up_mbps: int = 50        # Upload bandwidth in Mbps
    down_mbps: int = 100     # Download bandwidth in Mbps
    obfs: str = ""           # Obfuscation password (hysteria2)
    congestion_control: str = "bbr"  # TUIC congestion control
    tuic_password: str = ""  # TUIC password (separate from UUID)

    # Status tracking
    status: NodeStatus = NodeStatus.UNKNOWN
    latency_ms: int = 0
    upload_bytes: int = 0
    download_bytes: int = 0

    # Metadata
    group: str = "default"
    tags: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class ForwardRule:
    """A port forwarding rule: route target_ip:port through a specific node."""
    target_ip: str = ""        # e.g. "192.168.1.100"
    target_port: int = 0       # e.g. 8080, 0 = all ports
    node_id: str = ""          # Proxy node to route through


@dataclass
class RoutingConfig:
    """Split-routing configuration for private vs public IP traffic."""
    # Port forwarding rules: specific IP:port → via local proxy
    forward_rules: list[ForwardRule] = field(default_factory=list)
    forward_proxy_addr: str = "127.0.0.1:1080"  # Local proxy for forward rules
    forward_proxy_type: str = "socks"            # socks or http

    # Private IP exit (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
    private_ip_mode: ExitMode = ExitMode.LOCAL
    private_ip_node_id: Optional[str] = None   # Node ID if mode == REMOTE

    # Public IP exit (everything else)
    public_ip_mode: ExitMode = ExitMode.REMOTE
    public_ip_node_id: Optional[str] = None     # Node ID if mode == REMOTE


@dataclass
class AppConfig:
    """Global application configuration."""
    sing_box_path: str = ""  # Empty = auto-detect (bundled > system PATH)
    socks_port: int = 1080
    http_port: int = 8080
    mixed_port: int = 10810       # sing-box mixed inbound
    allow_lan: bool = False
    tun_mode: bool = False        # Enable TUN for full-network routing
    tun_name: str = "tun0"
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    log_level: str = "info"
    auto_start: bool = False
