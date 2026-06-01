"""Storage manager: JSON-based persistence for nodes and app config.

Stores data in the user's config directory by default:
- Windows: %APPDATA%/proxynet/
- Linux: ~/.config/proxynet/
- macOS: ~/Library/Application Support/proxynet/
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from models.node import ProxyNode, ProxyType, NodeStatus, AppConfig, ForwardRule, RoutingConfig, ExitMode


def _get_data_dir() -> Path:
    """Get the data directory for ProxyNet."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))

    data_dir = base / "proxynet"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


class StorageManager:
    """Manages persistence of nodes and application configuration."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or _get_data_dir()
        self.nodes_file = self.data_dir / "nodes.json"
        self.config_file = self.data_dir / "config.json"
        self._ensure_files()

    def _ensure_files(self) -> None:
        """Create default files if they don't exist."""
        if not self.nodes_file.exists():
            self._save_json(self.nodes_file, {"nodes": [], "updated_at": ""})

        if not self.config_file.exists():
            default_config = AppConfig()
            self.save_config(default_config)

    def _save_json(self, path: Path, data: dict) -> None:
        """Save data as formatted JSON."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def _load_json(self, path: Path) -> dict:
        """Load JSON data from file."""
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ── Node operations ──────────────────────────────────────────

    def load_nodes(self) -> list[ProxyNode]:
        """Load all nodes from storage."""
        data = self._load_json(self.nodes_file)
        nodes_data = data.get("nodes", [])
        nodes = []
        for nd in nodes_data:
            try:
                node = self._dict_to_node(nd)
                nodes.append(node)
            except Exception:
                continue  # Skip corrupted entries
        return nodes

    def save_nodes(self, nodes: list[ProxyNode]) -> None:
        """Save all nodes to storage."""
        nodes_data = [self._node_to_dict(n) for n in nodes]
        self._save_json(self.nodes_file, {
            "nodes": nodes_data,
            "updated_at": datetime.now().isoformat(),
        })

    def add_node(self, node: ProxyNode) -> None:
        """Add a single node to storage."""
        nodes = self.load_nodes()
        # Avoid duplicates by host+port+type
        for existing in nodes:
            if (existing.host == node.host and
                    existing.port == node.port and
                    existing.proxy_type == node.proxy_type):
                # Update existing node instead
                existing.name = node.name or existing.name
                existing.uuid = node.uuid or existing.uuid
                existing.password = node.password or existing.password
                existing.method = node.method or existing.method
                existing.network = node.network or existing.network
                existing.ws_path = node.ws_path or existing.ws_path
                existing.ws_host = node.ws_host or existing.ws_host
                existing.tls = node.tls
                existing.sni = node.sni or existing.sni
                existing.fingerprint = node.fingerprint or existing.fingerprint
                existing.notes = node.notes or existing.notes
                self.save_nodes(nodes)
                return

        nodes.append(node)
        self.save_nodes(nodes)

    def remove_node(self, node_id: str) -> bool:
        """Remove a node by ID. Returns True if removed."""
        nodes = self.load_nodes()
        new_nodes = [n for n in nodes if n.id != node_id]
        if len(new_nodes) != len(nodes):
            self.save_nodes(new_nodes)
            return True
        return False

    def get_node(self, node_id: str) -> Optional[ProxyNode]:
        """Get a single node by ID."""
        for node in self.load_nodes():
            if node.id == node_id:
                return node
        return None

    def update_node(self, node: ProxyNode) -> bool:
        """Update an existing node. Returns True if found and updated."""
        nodes = self.load_nodes()
        for i, existing in enumerate(nodes):
            if existing.id == node.id:
                nodes[i] = node
                self.save_nodes(nodes)
                return True
        return False

    # ── Config operations ────────────────────────────────────────

    def load_config(self) -> AppConfig:
        """Load application configuration."""
        data = self._load_json(self.config_file)
        if not data:
            return AppConfig()

        config = AppConfig(
            sing_box_path=data.get("sing_box_path", "sing-box"),
            socks_port=data.get("socks_port", 1080),
            http_port=data.get("http_port", 8080),
            mixed_port=data.get("mixed_port", 2080),
            allow_lan=data.get("allow_lan", False),
            tun_mode=data.get("tun_mode", False),
            tun_name=data.get("tun_name", "tun0"),
            log_level=data.get("log_level", "info"),
            auto_start=data.get("auto_start", False),
        )

        # Routing config
        routing_data = data.get("routing", {})
        if routing_data:
            fwd_rules = []
            for fr in routing_data.get("forward_rules", []):
                fwd_rules.append(ForwardRule(
                    target_ip=fr.get("target_ip", ""),
                    target_port=fr.get("target_port", 0),
                    node_id=fr.get("node_id", ""),
                ))
            config.routing = RoutingConfig(
                forward_rules=fwd_rules,
                forward_proxy_addr=routing_data.get("forward_proxy_addr", "127.0.0.1:1080"),
                forward_proxy_type=routing_data.get("forward_proxy_type", "socks"),
                private_ip_mode=ExitMode(routing_data.get("private_ip_mode", "local")),
                private_ip_node_id=routing_data.get("private_ip_node_id"),
                public_ip_mode=ExitMode(routing_data.get("public_ip_mode", "remote")),
                public_ip_node_id=routing_data.get("public_ip_node_id"),
            )

        return config

    def save_config(self, config: AppConfig) -> None:
        """Save application configuration."""
        data = {
            "sing_box_path": config.sing_box_path,
            "socks_port": config.socks_port,
            "http_port": config.http_port,
            "mixed_port": config.mixed_port,
            "allow_lan": config.allow_lan,
            "tun_mode": config.tun_mode,
            "tun_name": config.tun_name,
            "log_level": config.log_level,
            "auto_start": config.auto_start,
            "routing": {
                "forward_rules": [
                    {"target_ip": fr.target_ip, "target_port": fr.target_port, "node_id": fr.node_id}
                    for fr in config.routing.forward_rules
                ],
                "forward_proxy_addr": config.routing.forward_proxy_addr,
                "forward_proxy_type": config.routing.forward_proxy_type,
                "private_ip_mode": config.routing.private_ip_mode.value,
                "private_ip_node_id": config.routing.private_ip_node_id,
                "public_ip_mode": config.routing.public_ip_mode.value,
                "public_ip_node_id": config.routing.public_ip_node_id,
            },
        }
        self._save_json(self.config_file, data)

    # ── Serialization helpers ────────────────────────────────────

    def _node_to_dict(self, node: ProxyNode) -> dict:
        """Convert a ProxyNode to a JSON-serializable dict."""
        return {
            "id": node.id,
            "name": node.name,
            "proxy_type": node.proxy_type.value,
            "host": node.host,
            "port": node.port,
            "uuid": node.uuid,
            "password": node.password,
            "method": node.method,
            "security": node.security,
            "flow": node.flow,
            "network": node.network,
            "ws_path": node.ws_path,
            "ws_host": node.ws_host,
            "tls": node.tls,
            "sni": node.sni,
            "alpn": node.alpn,
            "fingerprint": node.fingerprint,
            "allow_insecure": node.allow_insecure,
            "reality": node.reality,
            "reality_pbk": node.reality_pbk,
            "reality_sid": node.reality_sid,
            "reality_spx": node.reality_spx,
            "up_mbps": node.up_mbps,
            "down_mbps": node.down_mbps,
            "obfs": node.obfs,
            "congestion_control": node.congestion_control,
            "tuic_password": node.tuic_password,
            "status": node.status.value,
            "latency_ms": node.latency_ms,
            "group": node.group,
            "tags": node.tags,
            "notes": node.notes,
        }

    def _dict_to_node(self, data: dict) -> ProxyNode:
        """Convert a JSON dict to a ProxyNode."""
        return ProxyNode(
            id=data.get("id", ""),
            name=data.get("name", ""),
            proxy_type=ProxyType(data.get("proxy_type", "vmess")),
            host=data.get("host", ""),
            port=data.get("port", 443),
            uuid=data.get("uuid", ""),
            password=data.get("password", ""),
            method=data.get("method", "aes-256-gcm"),
            security=data.get("security", "auto"),
            flow=data.get("flow", ""),
            network=data.get("network", "tcp"),
            ws_path=data.get("ws_path", ""),
            ws_host=data.get("ws_host", ""),
            tls=data.get("tls", False),
            sni=data.get("sni", ""),
            alpn=data.get("alpn", []),
            fingerprint=data.get("fingerprint", ""),
            allow_insecure=data.get("allow_insecure", False),
            reality=data.get("reality", False),
            reality_pbk=data.get("reality_pbk", ""),
            reality_sid=data.get("reality_sid", ""),
            reality_spx=data.get("reality_spx", ""),
            up_mbps=data.get("up_mbps", 50),
            down_mbps=data.get("down_mbps", 100),
            obfs=data.get("obfs", ""),
            congestion_control=data.get("congestion_control", "bbr"),
            tuic_password=data.get("tuic_password", ""),
            status=NodeStatus(data.get("status", "unknown")),
            latency_ms=data.get("latency_ms", 0),
            group=data.get("group", "default"),
            tags=data.get("tags", []),
            notes=data.get("notes", ""),
        )
