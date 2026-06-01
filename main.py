#!/usr/bin/env python3
"""ProxyNet - 异地组网代理管理工具

Web-based proxy networking tool.
Manages proxy nodes via share links (SS, VMess, Trojan, VLESS),
supports split-routing for private/public IP traffic,
and controls sing-box as the proxy core engine.

Usage:
    python main.py                      Launch Web UI (default)
    python main.py --tui                Launch Textual TUI (legacy)
    python main.py --import <link>      Import a node (CLI mode)
    python main.py --start              Start engine with saved config
    python main.py --export-config <path>  Export sing-box config
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import os
import webbrowser

# Fix Unicode output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ProxyNet - Cross-site proxy networking tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--web", action="store_true", help="Launch Web UI instead of TUI")
    parser.add_argument("--import", dest="import_link", help="Import a node from share link")
    parser.add_argument("--start", action="store_true", help="Start engine with saved config")
    parser.add_argument("--export-config", metavar="PATH", help="Export sing-box config")
    parser.add_argument("--port", type=int, default=18080, help="Web UI port (default: 18080)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    parser.add_argument("--version", action="store_true", help="Show version")
    return parser.parse_args()


def cli_import(link: str) -> None:
    from storage.manager import StorageManager
    from parser import parse_share_link

    node = parse_share_link(link)
    if not node:
        print(f"Error: Could not parse link: {link}")
        sys.exit(1)

    StorageManager().add_node(node)
    print(f"Imported node: {node.name}")
    print(f"  Type: {node.proxy_type.value}")
    print(f"  Host: {node.host}:{node.port}")


async def cli_start() -> None:
    from storage.manager import StorageManager
    from core.engine import SingBoxEngine

    storage = StorageManager()
    nodes = storage.load_nodes()
    config = storage.load_config()

    if not nodes:
        print("Error: No nodes configured. Import a node first.")
        sys.exit(1)

    print(f"Loaded {len(nodes)} node(s)")
    engine = SingBoxEngine(config=config, on_log=lambda t: print(f"  {t}"))
    success = await engine.start(nodes)
    if not success:
        print("Failed to start engine.")
        sys.exit(1)

    print("Engine running. Press Ctrl+C to stop...")
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        await engine.stop()


def cli_export_config(path: str) -> None:
    from storage.manager import StorageManager
    from core.config_generator import generate_singbox_config, save_config_to_file

    storage = StorageManager()
    nodes = storage.load_nodes()
    config = storage.load_config()

    if not nodes:
        print("Error: No nodes configured. Import a node first.")
        sys.exit(1)

    config_dict = generate_singbox_config(nodes, config)
    save_config_to_file(config_dict, path)
    print(f"Config exported to: {path}")
    print(f"  Nodes: {len(nodes)}")
    print(f"  Mixed port: {config.mixed_port}")


def main() -> None:
    args = parse_args()

    if args.version:
        print("ProxyNet v0.2.0")
        return

    if args.import_link:
        cli_import(args.import_link)
        return

    if args.export_config:
        cli_export_config(args.export_config)
        return

    if args.start:
        asyncio.run(cli_start())
        return

    # ── Bootstrap ──────────────────────────────────────────────
    result = asyncio.run(_run_bootstrap())

    if not result["ready"]:
        print("\nBootstrap failed.")
        print("Try manually: pip install flask aiohttp")
        sys.exit(1)

    # ── TUI (default) ─────────────────────────────────────────
    if args.web:
        # ── Web UI (--web flag) ───────────────────────────────
        from web.server import start_server
        host = "127.0.0.1"
        port = args.port
        print()
        print(f"  ProxyNet Web UI: http://{host}:{port}")
        print(f"  Press Ctrl+C to stop")
        print()
        if not args.no_browser:
            try:
                webbrowser.open(f"http://{host}:{port}")
            except Exception:
                pass
        start_server(host=host, port=port)
        return

    # TUI mode — app.run() creates its own event loop
    from tui.app import ProxyNetApp
    app = ProxyNetApp()
    if result.get("singbox_path"):
        app.app_config.sing_box_path = result["singbox_path"]
        app.engine._app_config = app.app_config
    app.run()


async def _run_bootstrap() -> dict:
    from bootstrap import bootstrap
    return await bootstrap(verbose=True)


if __name__ == "__main__":
    main()
