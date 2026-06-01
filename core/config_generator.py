"""sing-box configuration generator.

Generates a complete sing-box config.json from managed nodes and routing preferences.
Supports split-routing: different exit nodes for private IP vs public IP traffic.

sing-box config structure (v1.10+):
{
    "log": {...},
    "dns": {...},
    "inbounds": [...],
    "outbounds": [...],
    "route": {...}
}
"""

from __future__ import annotations

import json
from typing import Optional

from models.node import ProxyNode, ProxyType, AppConfig, RoutingConfig, ExitMode, NodeStatus
from parser.base import PRIVATE_IP_RANGES


def generate_singbox_config(
    nodes: list[ProxyNode],
    config: AppConfig,
    active_node_ids: Optional[set[str]] = None,
) -> dict:
    """Generate a complete sing-box configuration.

    Args:
        nodes: All managed proxy nodes.
        config: Application configuration (ports, TUN, routing).
        active_node_ids: Set of node IDs to include. If None, includes all nodes.

    Returns:
        A dict representing the sing-box config.json.
    """
    if active_node_ids is None:
        active_node_ids = {n.id for n in nodes}

    active_nodes = [n for n in nodes if n.id in active_node_ids]

    # Sanitize: reset routing refs to nodes that no longer exist
    _sanitize_routing(config, active_node_ids)

    outbounds = _generate_outbounds(active_nodes, config)

    # Forward mode: add a local outbound pointing to user's proxy software
    if config.routing.public_ip_node_id == "__forward__":
        fwd_addr = config.routing.forward_proxy_addr or "127.0.0.1:1080"
        fwd_type = config.routing.forward_proxy_type or "socks"
        host, _, port_str = fwd_addr.rpartition(":")
        try:
            fwd_port = int(port_str)
        except ValueError:
            fwd_port = 1080
        outbounds.append({
            "type": fwd_type,
            "tag": "forward-local",
            "server": host or "127.0.0.1",
            "server_port": fwd_port,
        })

    inbounds = _generate_inbounds(config)
    route = _generate_route(nodes, config)
    dns_config = _generate_dns(config, active_node_ids)

    return {
        "log": {
            "level": config.log_level,
            "timestamp": True,
        },
        "dns": dns_config,
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": route,
        "experimental": {
            "cache_file": {
                "enabled": True,
                "path": "proxynet-cache.db",
            },
        },
    }


def _sanitize_routing(config: AppConfig, valid_ids: set[str]) -> None:
    """Reset routing references to nodes that no longer exist."""
    if config.routing.private_ip_node_id and config.routing.private_ip_node_id not in valid_ids:
        config.routing.private_ip_mode = ExitMode.LOCAL
        config.routing.private_ip_node_id = None
    pid = config.routing.public_ip_node_id
    if pid and pid != "__forward__" and pid not in valid_ids:
        config.routing.public_ip_mode = ExitMode.LOCAL
        config.routing.public_ip_node_id = None


def _generate_inbounds(config: AppConfig) -> list[dict]:
    """Generate inbound configurations."""
    inbounds = []

    # Mixed inbound (SOCKS5 + HTTP)
    mixed_inbound: dict = {
        "type": "mixed",
        "tag": "mixed-in",
        "listen": "0.0.0.0" if config.allow_lan else "127.0.0.1",
        "listen_port": config.mixed_port,
    }
    inbounds.append(mixed_inbound)

    # TUN inbound for full-network routing
    if config.tun_mode:
        tun_inbound: dict = {
            "type": "tun",
            "tag": "tun-in",
            "interface_name": config.tun_name,
            "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
            "mtu": 9000,
            "auto_route": True,
            "strict_route": True,
            "stack": "mixed",
        }
        inbounds.append(tun_inbound)

    return inbounds


def _generate_outbounds(
    active_nodes: list[ProxyNode],
    config: AppConfig,
) -> list[dict]:
    """Generate outbound configurations for each node plus direct outbound."""
    outbounds = []

    for node in active_nodes:
        outbound = _node_to_outbound(node)
        outbounds.append(outbound)

    # Direct outbound (no proxy, for local traffic)
    outbounds.append({
        "type": "direct",
        "tag": "direct-out",
    })

    # Block outbound (for rejecting unwanted traffic)
    outbounds.append({
        "type": "block",
        "tag": "block-out",
    })

    return outbounds


def _node_to_outbound(node: ProxyNode) -> dict:
    """Convert a ProxyNode to a sing-box outbound configuration."""
    outbound: dict = {
        "tag": f"proxy-{node.id}",
        "server": node.host,
        "server_port": node.port,
    }

    if node.proxy_type == ProxyType.SS:
        outbound["type"] = "shadowsocks"
        outbound["method"] = node.method
        outbound["password"] = node.password

    elif node.proxy_type == ProxyType.VMESS:
        outbound["type"] = "vmess"
        outbound["uuid"] = node.uuid
        outbound["security"] = node.security
        outbound["alter_id"] = 0

    elif node.proxy_type == ProxyType.TROJAN:
        outbound["type"] = "trojan"
        outbound["password"] = node.password

    elif node.proxy_type == ProxyType.VLESS:
        outbound["type"] = "vless"
        outbound["uuid"] = node.uuid
        if node.flow:
            outbound["flow"] = node.flow

    elif node.proxy_type == ProxyType.HYSTERIA2:
        outbound["type"] = "hysteria2"
        outbound.pop("server", None)
        outbound.pop("server_port", None)
        outbound["server"] = node.host
        outbound["server_port"] = node.port
        outbound["password"] = node.password
        if node.up_mbps:
            outbound["up_mbps"] = node.up_mbps
        if node.down_mbps:
            outbound["down_mbps"] = node.down_mbps
        if node.obfs:
            obfs_cfg: dict = {"type": "salamander", "password": node.obfs}
            outbound["obfs"] = obfs_cfg

    elif node.proxy_type == ProxyType.TUIC:
        outbound["type"] = "tuic"
        outbound["uuid"] = node.uuid
        outbound["password"] = node.tuic_password or node.password
        if node.congestion_control:
            outbound["congestion_control"] = node.congestion_control

    # Transport settings (skip for hysteria2/tuic — they handle transport internally)
    if node.proxy_type not in (ProxyType.HYSTERIA2, ProxyType.TUIC):
        transport: dict = {}

        if node.network == "ws":
            transport["type"] = "ws"
            if node.ws_path:
                transport["path"] = node.ws_path
            if node.ws_host:
                transport["headers"] = {"Host": node.ws_host}
            transport["early_data_header_name"] = "Sec-WebSocket-Protocol"

        elif node.network == "grpc":
            transport["type"] = "grpc"
            if node.ws_path:
                transport["service_name"] = node.ws_path

        elif node.network == "h2":
            transport["type"] = "http"
            if node.ws_path:
                transport["path"] = node.ws_path
            if node.ws_host:
                transport["host"] = [node.ws_host]

        elif node.network == "quic":
            transport["type"] = "quic"

        if transport:
            outbound["transport"] = transport

    # TLS settings
    if node.tls:
        tls_config: dict = {"enabled": True}
        if node.sni:
            tls_config["server_name"] = node.sni
        if node.alpn:
            tls_config["alpn"] = node.alpn
        if node.fingerprint:
            tls_config["utls"] = {
                "enabled": True,
                "fingerprint": node.fingerprint,
            }
        if node.allow_insecure:
            tls_config["insecure"] = True
        # Reality
        if node.reality:
            reality_cfg: dict = {"enabled": True}
            if node.reality_pbk:
                reality_cfg["public_key"] = node.reality_pbk
            if node.reality_sid:
                reality_cfg["short_id"] = node.reality_sid
            tls_config["reality"] = reality_cfg
        outbound["tls"] = tls_config

    return outbound


def _generate_route(
    nodes: list[ProxyNode],
    config: AppConfig,
) -> dict:
    """Generate routing rules with split-routing support.

    Split routing logic:
    - Private IP traffic → exit via private_ip_node (or direct if LOCAL)
    - Public IP traffic → exit via public_ip_node (or direct if LOCAL)
    - DNS queries → route to DNS outbound
    """
    rules: list[dict] = []
    routing = config.routing

    # Build node lookup
    node_map: dict[str, ProxyNode] = {n.id: n for n in nodes}

    # ── Private IP routing rule ─────────────────────────────────
    if routing.private_ip_mode == ExitMode.LOCAL:
        # Private IPs go direct
        rules.append({
            "ip_is_private": True,
            "outbound": "direct-out",
        })
    elif routing.private_ip_node_id and routing.private_ip_node_id in node_map:
        # Private IPs go through selected remote node
        rules.append({
            "ip_is_private": True,
            "outbound": f"proxy-{routing.private_ip_node_id}",
        })

    # Public IP routing rule
    if routing.public_ip_node_id == "__forward__":
        # "IP Forward" mode — all public IPs routed to local proxy
        rules.append({
            "network": "tcp",
            "outbound": "forward-local",
        })
        rules.append({
            "network": "udp",
            "outbound": "forward-local",
        })
    elif routing.public_ip_mode == ExitMode.REMOTE and routing.public_ip_node_id:
        if routing.public_ip_node_id in node_map:
            rules.append({
                "network": "tcp",
                "outbound": f"proxy-{routing.public_ip_node_id}",
            })
            rules.append({
                "network": "udp",
                "outbound": f"proxy-{routing.public_ip_node_id}",
            })
    elif routing.public_ip_mode == ExitMode.LOCAL:
        # All remaining traffic direct
        pass

    # Final catch-all (if no public IP rule matched)
    if routing.public_ip_mode == ExitMode.LOCAL:
        rules.append({
            "outbound": "direct-out",
        })

    route: dict = {
        "rules": rules,
        "auto_detect_interface": True,
        "default_domain_resolver": "dns-main",
    }

    return route


def _generate_dns(config: AppConfig, valid_ids: set[str] | None = None) -> dict:
    """Generate DNS configuration (sing-box 1.12+ compatible)."""
    dns_servers: list[dict] = []

    # Main DNS server (uses direct outbound for resolution)
    dns_servers.append({
        "tag": "dns-main",
        "address": "https://223.5.5.5/dns-query",
        "detour": "direct-out",
        "strategy": "prefer_ipv4",
    })

    # Remote DNS via proxy (only if routing public through remote node AND node exists)
    pub_id = config.routing.public_ip_node_id
    if (config.routing.public_ip_mode == ExitMode.REMOTE and pub_id
            and (valid_ids is None or pub_id in valid_ids)):
        dns_servers.append({
            "tag": "dns-remote",
            "address": "https://1.1.1.1/dns-query",
            "detour": f"proxy-{pub_id}",
            "strategy": "prefer_ipv4",
        })

    dns: dict = {
        "servers": dns_servers,
        "strategy": "prefer_ipv4",
    }

    return dns


def save_config_to_file(config_dict: dict, path: str) -> None:
    """Write the generated sing-box config to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config_dict, f, ensure_ascii=False, indent=2)
