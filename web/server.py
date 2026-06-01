"""Flask web server for ProxyNet - provides REST API + serves web UI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import queue
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response

# Ensure project root in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.node import ProxyNode, ProxyType, NodeStatus, ExitMode, RoutingConfig, AppConfig
from parser import parse_share_link, is_valid_share_link
from storage.manager import StorageManager
from core.engine import SingBoxEngine, find_sing_box
from core.monitor import NodeMonitor, test_latency
from core.config_generator import generate_singbox_config

app = Flask(__name__)

# ── Global state ─────────────────────────────────────────────────

storage = StorageManager()
nodes: list[ProxyNode] = []
app_config: AppConfig = AppConfig()
engine: SingBoxEngine | None = None
monitor: NodeMonitor | None = None
log_queue: queue.Queue = queue.Queue()
# Dedicated event loop + thread for engine operations
_engine_loop: asyncio.AbstractEventLoop | None = None
_engine_thread: threading.Thread | None = None
_loop_running: bool = False


def _start_engine_loop():
    """Start a persistent event loop in a background thread."""
    global _engine_loop, _engine_thread, _loop_running
    if _loop_running:
        return  # Already running

    _engine_loop = asyncio.new_event_loop()
    _loop_running = True

    def _run_loop():
        asyncio.set_event_loop(_engine_loop)
        _engine_loop.run_forever()

    _engine_thread = threading.Thread(target=_run_loop, daemon=True)
    _engine_thread.start()


def _stop_engine_loop():
    """Stop the persistent event loop."""
    global _engine_loop, _loop_running
    _loop_running = False
    if _engine_loop and not _engine_loop.is_closed():
        _engine_loop.call_soon_threadsafe(_engine_loop.stop)
    _engine_loop = None


def _run_async(coro):
    """Run an async coroutine in the persistent background event loop."""
    if _engine_loop is None or _engine_loop.is_closed():
        raise RuntimeError("Engine event loop not running")
    future = asyncio.run_coroutine_threadsafe(coro, _engine_loop)
    return future.result(timeout=60)


def _on_engine_log(text: str):
    """Callback for engine log messages → pushed to SSE queue."""
    log_queue.put(text)


async def _async_log_handler(text: str) -> None:
    """Async wrapper for engine log callback."""
    _on_engine_log(text)


async def _async_node_update(node: ProxyNode) -> None:
    """Async wrapper for node status updates."""
    global nodes
    storage.update_node(node)
    nodes = storage.load_nodes()


def init_app():
    """Initialize the application state."""
    global nodes, app_config, engine, monitor

    nodes = storage.load_nodes()
    app_config = storage.load_config()

    # Start the persistent event loop for async engine operations
    _start_engine_loop()

    engine = SingBoxEngine(
        config=app_config,
        on_log=_async_log_handler,
    )

    monitor = NodeMonitor(on_node_update=_async_node_update)


# ── API: Nodes ───────────────────────────────────────────────────

@app.route("/api/nodes", methods=["GET"])
def api_list_nodes():
    return jsonify([_node_to_dict(n) for n in nodes])


@app.route("/api/nodes", methods=["POST"])
def api_add_node():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    # Check if it's a share link import
    link = data.get("link", "")
    if link and is_valid_share_link(link):
        node = parse_share_link(link)
        if not node:
            return jsonify({"error": "Invalid share link"}), 400
    else:
        node = _dict_to_node(data)

    storage.add_node(node)
    global nodes
    nodes = storage.load_nodes()
    return jsonify(_node_to_dict(node)), 201


@app.route("/api/nodes/import-batch", methods=["POST"])
def api_import_batch():
    data = request.get_json()
    links_text = data.get("links", "")
    if not links_text:
        return jsonify({"error": "No links provided"}), 400

    from parser import parse_multiple_links
    imported = parse_multiple_links(links_text)
    for node in imported:
        storage.add_node(node)

    global nodes
    nodes = storage.load_nodes()
    return jsonify({"imported": len(imported), "total": len(nodes)})


@app.route("/api/nodes/<node_id>", methods=["GET"])
def api_get_node(node_id):
    node = storage.get_node(node_id)
    if not node:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_node_to_dict(node))


@app.route("/api/nodes/<node_id>", methods=["PUT"])
def api_update_node(node_id):
    data = request.get_json()
    node = storage.get_node(node_id)
    if not node:
        return jsonify({"error": "Not found"}), 404

    _apply_dict_to_node(node, data)
    storage.update_node(node)
    global nodes
    nodes = storage.load_nodes()
    return jsonify(_node_to_dict(node))


@app.route("/api/nodes/<node_id>", methods=["DELETE"])
def api_delete_node(node_id):
    removed = storage.remove_node(node_id)
    if not removed:
        return jsonify({"error": "Not found"}), 404
    global nodes
    nodes = storage.load_nodes()
    return jsonify({"ok": True})


@app.route("/api/nodes/<node_id>/test", methods=["POST"])
def api_test_node(node_id):
    global nodes
    node = storage.get_node(node_id)
    if not node:
        return jsonify({"error": "Not found"}), 404

    lat = _run_async(test_latency(node.host, node.port))
    if lat is not None:
        node.latency_ms = lat
        node.status = NodeStatus.ONLINE
    else:
        node.latency_ms = 0
        node.status = NodeStatus.OFFLINE

    storage.update_node(node)
    nodes = storage.load_nodes()
    return jsonify({"latency_ms": lat, "status": node.status.value})


@app.route("/api/nodes/test-all", methods=["POST"])
def api_test_all_nodes():
    global nodes
    if monitor is None:
        return jsonify({"error": "Monitor not initialized"}), 500

    results = _run_async(monitor.test_all(nodes))
    nodes = storage.load_nodes()
    storage.save_nodes(nodes)

    return jsonify({
        node_id: lat
        for node_id, lat in results.items()
    })


# ── API: Engine ──────────────────────────────────────────────────

@app.route("/api/engine/status", methods=["GET"])
def api_engine_status():
    return jsonify({
        "running": engine is not None and engine.is_running,
        "log_lines": engine.log_lines[-50:] if engine else [],
    })


@app.route("/api/engine/start", methods=["POST"])
def api_engine_start():
    if engine is None:
        return jsonify({"error": "Engine not initialized"}), 500

    if engine.is_running:
        return jsonify({"ok": True, "message": "Already running"})

    _on_engine_log("[INFO] Starting sing-box...")
    success = _run_async(engine.start(nodes))
    return jsonify({"ok": success, "message": "Started" if success else "Failed"})


@app.route("/api/engine/stop", methods=["POST"])
def api_engine_stop():
    if engine is None:
        return jsonify({"error": "Engine not initialized"}), 500

    _on_engine_log("[INFO] Stopping sing-box...")
    _run_async(engine.stop())
    return jsonify({"ok": True, "message": "Stopped"})


@app.route("/api/engine/logs/stream")
def api_engine_logs_stream():
    """Server-Sent Events stream of engine logs."""
    def generate():
        while True:
            try:
                msg = log_queue.get(timeout=30)
                yield f"data: {json.dumps({'text': msg})}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'text': ''})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ── API: Routing ─────────────────────────────────────────────────

@app.route("/api/routing", methods=["GET"])
def api_get_routing():
    return jsonify({
        "private_ip_mode": app_config.routing.private_ip_mode.value,
        "private_ip_node_id": app_config.routing.private_ip_node_id,
        "public_ip_mode": app_config.routing.public_ip_mode.value,
        "public_ip_node_id": app_config.routing.public_ip_node_id,
    })


@app.route("/api/routing", methods=["PUT"])
def api_update_routing():
    data = request.get_json()
    routing = app_config.routing

    if "private_ip_mode" in data:
        routing.private_ip_mode = ExitMode(data["private_ip_mode"])
    if "private_ip_node_id" in data:
        routing.private_ip_node_id = data["private_ip_node_id"] or None
    if "public_ip_mode" in data:
        routing.public_ip_mode = ExitMode(data["public_ip_mode"])
    if "public_ip_node_id" in data:
        routing.public_ip_node_id = data["public_ip_node_id"] or None

    storage.save_config(app_config)
    return jsonify({"ok": True})


# ── API: Config ──────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify({
        "socks_port": app_config.socks_port,
        "http_port": app_config.http_port,
        "mixed_port": app_config.mixed_port,
        "allow_lan": app_config.allow_lan,
        "tun_mode": app_config.tun_mode,
        "log_level": app_config.log_level,
        "sing_box_path": app_config.sing_box_path,
    })


@app.route("/api/config", methods=["PUT"])
def api_update_config():
    data = request.get_json()
    for field in ("socks_port", "http_port", "mixed_port", "allow_lan",
                  "tun_mode", "log_level", "sing_box_path"):
        if field in data:
            setattr(app_config, field, data[field])
    storage.save_config(app_config)
    return jsonify({"ok": True})


@app.route("/api/config/export", methods=["GET"])
def api_export_config():
    """Generate and return sing-box config JSON."""
    config_dict = generate_singbox_config(nodes, app_config)
    return jsonify(config_dict)


# ── Main page ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Serialization helpers ─────────────────────────────────────────

def _node_to_dict(node: ProxyNode) -> dict:
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
        "status": node.status.value,
        "latency_ms": node.latency_ms,
        "group": node.group,
        "tags": node.tags,
        "notes": node.notes,
    }


def _dict_to_node(data: dict) -> ProxyNode:
    return ProxyNode(
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
        group=data.get("group", "default"),
        tags=data.get("tags", []),
        notes=data.get("notes", ""),
    )


def _apply_dict_to_node(node: ProxyNode, data: dict) -> None:
    for attr in ("name", "host", "uuid", "password", "method", "security",
                 "flow", "network", "ws_path", "ws_host", "sni", "fingerprint",
                 "group", "notes"):
        if attr in data:
            setattr(node, attr, data[attr])
    if "port" in data:
        node.port = int(data["port"])
    if "proxy_type" in data:
        node.proxy_type = ProxyType(data["proxy_type"])
    if "tls" in data:
        node.tls = bool(data["tls"])
    if "allow_insecure" in data:
        node.allow_insecure = bool(data["allow_insecure"])
    if "alpn" in data:
        node.alpn = data["alpn"] if isinstance(data["alpn"], list) else []
    if "tags" in data:
        node.tags = data["tags"] if isinstance(data["tags"], list) else []


def start_server(host: str = "127.0.0.1", port: int = 18080, debug: bool = False):
    """Initialize and start the Flask server."""
    init_app()

    print(f"\n  ProxyNet Web UI: http://{host}:{port}")
    print(f"  Press Ctrl+C to stop\n")

    # Don't open browser automatically - let user decide
    app.run(host=host, port=port, debug=debug, threaded=True)
