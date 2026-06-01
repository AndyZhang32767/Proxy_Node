"""Main Textual application for ProxyNet - 异地组网代理管理工具.

Provides a terminal UI for managing proxy nodes, importing share links,
configuring split-routing (private/public IP exit nodes), and controlling
the sing-box proxy engine.
"""

from __future__ import annotations

import re
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Header, Footer, DataTable, Static, Button, Input,
    Label, Switch, Select, RichLog, TabbedContent, TabPane,
    LoadingIndicator,
)
from textual.screen import ModalScreen
from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.css.query import NoMatches
from typing import Optional

from models.node import ProxyNode, ProxyType, NodeStatus, ExitMode, RoutingConfig, AppConfig
from parser import parse_share_link, parse_multiple_links, is_valid_share_link
from storage.manager import StorageManager
from core.engine import SingBoxEngine, find_sing_box
from core.monitor import NodeMonitor, test_latency
from core.config_generator import generate_singbox_config


# ── Log formatter ────────────────────────────────────────────────

class LogFormatter:
    """Parse sing-box raw logs → clean formatted output."""

    _SB_LINE = re.compile(
        r'[+-]\d{4}\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+'
        r'(INFO|ERROR|WARN|WARNING|FATAL|DEBUG)\s*(.*)'
    )
    _CONN_ID = re.compile(r'\[(\d+)\s+\d+ms\]\s*')
    _FROM = re.compile(r'inbound connection from\s+([^\s:]+):(\d+)')
    _TO = re.compile(r'inbound connection to\s+([^\s:]+):(\d+)')
    _OUTBOUND = re.compile(r'outbound/(\w+)\[([^\]]+)\]:\s*outbound connection to\s+([^\s:]+):(\d+)')
    _CONN_ERR = re.compile(
        r'connection:\s*open connection to\s+([^\s:]+):(\d+)\s+'
        r'using outbound/(\w+)\[([^\]]+)\]:\s*(.+)'
    )
    _ANSI = re.compile(r'\x1b\[[\d;]*m')

    def __init__(self):
        self._conn_state: dict[str, dict] = {}

    def format(self, raw: str) -> str | None:
        """Parse a raw log line → formatted string, or None to skip."""
        now = datetime.now().strftime("%Y/%m/%d %H:%M")
        raw = raw.strip()

        # Internal ProxyNet prefixes
        if raw.startswith("[INFO]"):
            return f"[{now}] [INFO] {raw[7:]}"
        if raw.startswith("[WARN]"):
            return f"[{now}] [WARNING] {raw[7:]}"
        if raw.startswith("[ERROR]"):
            return f"[{now}] [ERROR] {raw[8:]}"
        if raw.startswith("[OUT]") or raw.startswith("[ERR]"):
            return self._parse_sb(raw[5:], now)

        return self._parse_sb(raw, now)

    def _parse_sb(self, text: str, now: str) -> str | None:
        """Parse a sing-box log line."""
        # Strip ANSI BEFORE all regex matching
        text = self._ANSI.sub('', text).strip()
        if not text:
            return None

        m = self._SB_LINE.match(text)
        if not m:
            return None

        date, time, level, msg = m.groups()
        ts = f"{date.replace('-', '/')} {time[:5]}"
        lvl = {"WARN": "WARNING", "FATAL": "ERROR"}.get(level, level)

        # Connection tracking (msg already ANSI-clean)
        cid_m = self._CONN_ID.search(msg)
        conn_id = cid_m.group(1) if cid_m else None
        msg_body = self._CONN_ID.sub('', msg) if cid_m else msg

        if conn_id:
            state = self._conn_state.setdefault(conn_id, {})
            fm = self._FROM.search(msg_body)
            if fm:
                state["from"] = f"{fm.group(1)}:{fm.group(2)}"
            tm = self._TO.search(msg_body)
            if tm:
                state["to"] = f"{tm.group(1)}:{tm.group(2)}"
            om = self._OUTBOUND.search(msg_body)
            if om:
                state["out_type"] = om.group(1)
                state["out_tag"] = om.group(2)
                state["out_to"] = f"{om.group(3)}:{om.group(4)}"

            # Error on this connection
            em = self._CONN_ERR.search(msg_body)
            if em:
                err = em.group(5).strip()
                dst_ip = em.group(1)
                dst_port = em.group(2)
                out_type = em.group(3)
                if conn_id in self._conn_state:
                    del self._conn_state[conn_id]
                return f"[{ts}] [{lvl}] {dst_ip}:{dst_port} via {out_type}: {err}"

            # Emit connection log when outbound routing is decided
            src = state.get("from", "?")
            dst = state.get("out_to") or state.get("to", "?")
            out_type = state.get("out_type", "")
            if out_type:  # Only emit once we know the routing decision
                if conn_id in self._conn_state:
                    del self._conn_state[conn_id]
                route = "PROXY" if out_type not in ("direct", "block", "dns") else "DIRECT"
                detail = f": {out_type}" if route == "PROXY" else ""
                return f"[{ts}] [{lvl}] {src} -> {dst} [{route}{detail}]"
            return None

        # Non-connection lines
        if "sing-box started" in msg:
            return f"[{ts}] [INFO] sing-box started"
        if "tcp server started" in msg:
            pm = re.search(r'(\d+\.\d+\.\d+\.\d+):(\d+)', msg)
            if pm:
                return f"[{ts}] [INFO] Proxy listening on {pm.group(0)} (HTTP + SOCKS5)"
            return f"[{ts}] [INFO] {msg}"
        if "network: updated" in msg:
            return None  # skip noise
        if "inbound connection" in msg or "outbound connection" in msg:
            return None
        em = self._CONN_ERR.search(msg)
        if em:
            return f"[{ts}] [{lvl}] {em.group(1)}:{em.group(2)} via {em.group(3)}: {em.group(5).strip()}"

        return f"[{ts}] [{lvl}] {msg}"


# ── Color constants ──────────────────────────────────────────────

STATUS_ICONS: dict[NodeStatus, str] = {
    NodeStatus.UNKNOWN: "⚪",
    NodeStatus.ONLINE: "🟢",
    NodeStatus.OFFLINE: "🔴",
    NodeStatus.TESTING: "🟡",
    NodeStatus.ERROR: "⛔",
}

PROXY_TYPE_COLORS: dict[ProxyType, str] = {
    ProxyType.SS: "cyan",
    ProxyType.VMESS: "magenta",
    ProxyType.TROJAN: "yellow",
    ProxyType.VLESS: "green",
}


# ── Import Dialog ─────────────────────────────────────────────────

class ImportDialog(ModalScreen[Optional[list[ProxyNode]]]):
    """Modal dialog for importing nodes via share links."""

    CSS = """
    ImportDialog {
        align: center middle;
    }
    #import-container {
        width: 70;
        height: auto;
        max-height: 30;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    #import-title {
        text-align: center;
        text-style: bold;
        padding: 1 0;
    }
    #import-input {
        width: 100%;
        height: 5;
        margin: 1 0;
    }
    #import-hint {
        text-style: italic;
        color: $text-disabled;
        padding: 0 0 1 0;
    }
    #import-buttons {
        width: 100%;
        align: center middle;
    }
    #import-status {
        padding: 1 0;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="import-container"):
            yield Static("📥 Import Proxy Nodes", id="import-title")
            yield Static(
                "Paste one or more share links (ss://, vmess://, trojan://, vless://)\n"
                "One link per line.",
                id="import-hint",
            )
            yield Input(
                placeholder="Paste share links here...",
                id="import-input",
            )
            with Horizontal(id="import-buttons"):
                yield Button("Import", variant="primary", id="btn-import")
                yield Button("Cancel", variant="default", id="btn-cancel")
            yield Static("", id="import-status")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-import":
            text = self.query_one("#import-input", Input).value.strip()
            if not text:
                self.query_one("#import-status", Static).update("[red]Please enter share links[/red]")
                return

            nodes = parse_multiple_links(text)
            if not nodes:
                # Try single link parse for better error message
                node = parse_share_link(text)
                if node:
                    nodes = [node]

            if nodes:
                self.query_one("#import-status", Static).update(
                    f"[green]✓ Parsed {len(nodes)} node(s)[/green]"
                )
                self.dismiss(nodes)
            else:
                self.query_one("#import-status", Static).update(
                    "[red]✗ Could not parse any valid share links[/red]"
                )

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


# ── Routing Config Screen ────────────────────────────────────────

class RoutingConfigScreen(ModalScreen[Optional[RoutingConfig]]):
    """Modal screen for configuring split-routing (private vs public IP exit)."""

    CSS = """
    RoutingConfigScreen {
        align: center middle;
    }
    #routing-container {
        width: 92%;
        height: 88%;
        background: $surface;
        border: thick $secondary;
        padding: 1 2;
    }
    #routing-title {
        text-align: center;
        text-style: bold;
        padding: 1 0;
        height: 3;
    }
    #routing-cols {
        width: 100%;
        height: 1fr;
        margin: 1 0;
    }
    #routing-left, #routing-right {
        width: 1fr;
        padding: 0 1;
        border: solid $primary-background;
    }
    .routing-section {
        padding: 1 0;
        border-top: solid $primary-background;
    }
    .routing-label {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #routing-buttons {
        width: 100%;
        height: 3;
        align: center middle;
    }
    """

    def __init__(self, config: RoutingConfig, nodes: list[ProxyNode], app_config=None):
        super().__init__()
        self.routing = config
        self.nodes = nodes
        self.app_config = app_config  # For TUN mode toggle
        self._node_ids = {n.id for n in nodes}
        # Sanitize invalid node IDs (skip __forward__ marker)
        if self.routing.private_ip_node_id and self.routing.private_ip_node_id not in self._node_ids:
            self.routing.private_ip_mode = ExitMode.LOCAL
            self.routing.private_ip_node_id = None
        pid = self.routing.public_ip_node_id
        if pid and pid != "__forward__" and pid not in self._node_ids:
            self.routing.public_ip_mode = ExitMode.LOCAL
            self.routing.public_ip_node_id = None

    def compose(self) -> ComposeResult:
        node_options = [(n.name or f"{n.proxy_type.value}-{n.host}", n.id) for n in self.nodes]
        node_options.insert(0, ("None (use default)", ""))

        with Vertical(id="routing-container"):
            yield Static("🔀 Split Routing Configuration", id="routing-title")

            with Horizontal(id="routing-cols"):
                # ── Left Column ──────────────────────────
                with VerticalScroll(id="routing-left"):
                    with Vertical(classes="routing-section"):
                        yield Static("🏠 Private IP Traffic", classes="routing-label")
                        yield Static("10.x / 172.16.x / 192.168.x")
                        yield Select(
                            [("Direct (no proxy)", "local")] + node_options,
                            value=self._safe_select_value(self.routing.private_ip_mode, self.routing.private_ip_node_id),
                            id="sel-private",
                        )

                    with Vertical(classes="routing-section"):
                        yield Static("🔧 TUN Mode", classes="routing-label")
                        yield Static("Capture ALL traffic. Needs admin.")
                        tun_on = self.app_config.tun_mode if self.app_config else False
                        yield Switch(value=tun_on, id="sw-tun")

                # ── Right Column ─────────────────────────
                with VerticalScroll(id="routing-right"):
                    with Vertical(classes="routing-section"):
                        yield Static("🌐 Public IP Traffic", classes="routing-label")
                        pub_options = [("Direct (no proxy)", "local")] + node_options + [("IP Forward (use rules)", "forward")]
                        yield Select(
                            pub_options,
                            value=self._safe_select_value(self.routing.public_ip_mode, self.routing.public_ip_node_id),
                            id="sel-public",
                        )

                    is_fwd = self.routing.public_ip_node_id == "__forward__"
                    fwd_display = "block" if is_fwd else "none"

                    with Vertical(classes="routing-section", id="fwd-section"):
                        yield Static("🔀 IP Forward Proxy", classes="routing-label")
                        yield Static("⚠️ Only active when Public IP = \"IP Forward\"" if not is_fwd else "All public traffic goes to this local proxy.")
                        yield Static("Local proxy address:", classes="routing-label")
                        yield Input(
                            value=self.routing.forward_proxy_addr,
                            placeholder="127.0.0.1:1080",
                            id="inp-fwd-proxy-addr",
                        )
                        yield Static("Local proxy type:", classes="routing-label")
                        yield Select(
                            [("SOCKS5", "socks"), ("HTTP", "http")],
                            value=self.routing.forward_proxy_type,
                            id="sel-proxy-type",
                        )

            # Buttons
            with Horizontal(id="routing-buttons"):
                yield Button("Save", variant="primary", id="btn-save-routing")
                yield Button("Cancel", variant="default", id="btn-cancel-routing")

    def _safe_select_value(self, mode: ExitMode, node_id: str | None) -> str:
        """Return a Select value guaranteed to exist in options."""
        if node_id == "__forward__":
            return "forward"
        if node_id == "__forward__":
            return "forward"
        if mode == ExitMode.LOCAL or not node_id:
            return "local"
        if node_id in self._node_ids:
            return node_id
        return "local"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel-routing":
            self.dismiss(None)
        elif event.button.id == "btn-save-routing":
            private_val = self.query_one("#sel-private", Select).value
            public_val = self.query_one("#sel-public", Select).value
            tun_on = self.query_one("#sw-tun", Switch).value

            self.routing.private_ip_mode = ExitMode.LOCAL if private_val == "local" or private_val == "" else ExitMode.REMOTE
            self.routing.private_ip_node_id = None if private_val in ("local", "") else private_val

            if public_val == "forward":
                self.routing.public_ip_mode = ExitMode.REMOTE  # Keep as REMOTE for config compat
                self.routing.public_ip_node_id = "__forward__"  # Marker for forward mode
            elif public_val == "local" or public_val == "":
                self.routing.public_ip_mode = ExitMode.LOCAL
                self.routing.public_ip_node_id = None
            else:
                self.routing.public_ip_mode = ExitMode.REMOTE
                self.routing.public_ip_node_id = public_val

            # IP Forward proxy settings
            fwd_proxy_addr = self.query_one("#inp-fwd-proxy-addr", Input).value.strip() or "127.0.0.1:1080"
            fwd_proxy_type = self.query_one("#sel-proxy-type", Select).value or "socks5"
            self.routing.forward_proxy_addr = fwd_proxy_addr
            self.routing.forward_proxy_type = fwd_proxy_type

            # Return (routing, tun_enabled)
            self.dismiss((self.routing, tun_on))

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


# ── Log Viewer Modal ──────────────────────────────────────────────

class LogViewerScreen(ModalScreen[None]):
    """Full-screen floating log viewer."""

    CSS = """
    LogViewerScreen {
        align: center middle;
    }
    #logviewer-container {
        width: 95%;
        height: 90%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    #logviewer-title {
        text-align: center;
        text-style: bold;
        padding: 0 0 1 0;
    }
    #logviewer-content {
        height: 1fr;
        background: #0a0b10;
        padding: 0 1;
    }
    #logviewer-hint {
        text-style: italic;
        color: $text-disabled;
        text-align: center;
        padding: 1 0 0 0;
    }
    """

    def __init__(self, log_lines: list[str]):
        super().__init__()
        self._lines = log_lines  # Reference to app's live log buffer
        self._last_count = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="logviewer-container"):
            yield Static("📜 ProxyNet Logs — Live", id="logviewer-title")
            log = RichLog(id="logviewer-content", max_lines=0, auto_scroll=True)
            yield log
            yield Static("ESC to close | Mouse scroll | PgUp/PgDn | Home/End", id="logviewer-hint")

    def on_mount(self) -> None:
        log = self.query_one("#logviewer-content", RichLog)
        for line in self._lines:
            log.write(line)
        self._last_count = len(self._lines)
        # Poll for new log lines every 0.5s
        self.set_interval(0.5, self._refresh_logs)

    def _refresh_logs(self) -> None:
        """Check for new log lines and write them to the viewer."""
        if len(self._lines) <= self._last_count:
            return
        log = self.query_one("#logviewer-content", RichLog)
        for line in self._lines[self._last_count:]:
            log.write(line)
        self._last_count = len(self._lines)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


# ── Node Edit Modal ───────────────────────────────────────────────

class NodeEditScreen(ModalScreen[Optional[ProxyNode]]):
    """Modal screen for editing a node's basic properties."""

    CSS = """
    NodeEditScreen {
        align: center middle;
    }
    #edit-container {
        width: 55;
        height: auto;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    #edit-title {
        text-align: center;
        text-style: bold;
        padding: 1 0;
    }
    .edit-row {
        height: 3;
        margin: 0 1;
    }
    .edit-label {
        width: 15;
        content-align: right middle;
        padding: 0 1;
    }
    .edit-input {
        width: 1fr;
    }
    #edit-buttons {
        width: 100%;
        align: center middle;
        margin: 1 0;
    }
    """

    def __init__(self, node: Optional[ProxyNode] = None):
        super().__init__()
        self.node = node or ProxyNode()
        self.is_new = node is None

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-container"):
            yield Static("✏️ Edit Node" if not self.is_new else "➕ Add Node", id="edit-title")

            with Horizontal(classes="edit-row"):
                yield Static("Name:", classes="edit-label")
                yield Input(value=self.node.name, placeholder="Display name", id="edit-name", classes="edit-input")

            with Horizontal(classes="edit-row"):
                yield Static("Host:", classes="edit-label")
                yield Input(value=self.node.host, placeholder="Server address", id="edit-host", classes="edit-input")

            with Horizontal(classes="edit-row"):
                yield Static("Port:", classes="edit-label")
                yield Input(value=str(self.node.port), placeholder="443", id="edit-port", classes="edit-input")

            with Horizontal(classes="edit-row"):
                yield Static("UUID/Password:", classes="edit-label")
                yield Input(
                    value=self.node.uuid or self.node.password,
                    placeholder="UUID or password",
                    id="edit-auth",
                    classes="edit-input",
                )

            with Horizontal(classes="edit-row"):
                yield Static("Network:", classes="edit-label")
                yield Select(
                    [("tcp", "tcp"), ("ws", "ws"), ("grpc", "grpc"), ("h2", "h2"), ("quic", "quic")],
                    value=self.node.network or "tcp",
                    id="edit-network",
                )

            with Horizontal(classes="edit-row"):
                yield Static("TLS:", classes="edit-label")
                yield Switch(value=self.node.tls, id="edit-tls", classes="edit-input")

            with Horizontal(id="edit-buttons"):
                yield Button("Save", variant="primary", id="btn-save-edit")
                yield Button("Cancel", variant="default", id="btn-cancel-edit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel-edit":
            self.dismiss(None)
        elif event.button.id == "btn-save-edit":
            self.node.name = self.query_one("#edit-name", Input).value
            self.node.host = self.query_one("#edit-host", Input).value
            try:
                self.node.port = int(self.query_one("#edit-port", Input).value)
            except ValueError:
                self.node.port = 443

            auth = self.query_one("#edit-auth", Input).value
            if self.node.proxy_type in (ProxyType.VMESS, ProxyType.VLESS):
                self.node.uuid = auth
            else:
                self.node.password = auth

            self.node.network = self.query_one("#edit-network", Select).value or "tcp"
            self.node.tls = self.query_one("#edit-tls", Switch).value

            self.dismiss(self.node)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


# ── Main App ──────────────────────────────────────────────────────

class ProxyNetApp(App):
    """Main ProxyNet TUI application."""

    TITLE = "ProxyNet - 异地组网"
    SUB_TITLE = "Cross-site proxy networking"

    CSS = """
    #main-container {
        height: 1fr;
    }
    #left-panel {
        width: 55%;
        border-right: solid $primary-background;
    }
    #right-panel {
        width: 45%;
        padding: 0 1;
    }
    #node-table {
        height: 1fr;
    }
    #detail-section {
        height: auto;
        border-top: solid $primary-background;
        padding: 1 0;
    }
    #detail-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    .detail-row {
        height: 1;
        padding: 0 1;
    }
    .detail-key {
        color: $text-muted;
        width: 15;
    }
    .detail-value {
        width: 1fr;
    }

    #routing-section {
        border-top: solid $primary-background;
        padding: 1 0;
    }
    #routing-header {
        text-style: bold;
        padding: 0 0 1 0;
    }

    #engine-section {
        border-top: solid $primary-background;
        padding: 1 0;
    }
    #engine-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #engine-buttons {
        width: 100%;
        align: center middle;
        margin-bottom: 1;
    }
    #engine-status-text {
        text-align: center;
        color: $text-muted;
        padding: 0 0 1 0;
    }

    #log-section {
        height: 1fr;
        min-height: 10;
        border-top: solid $primary-background;
        margin-top: 1;
    }
    #log-title {
        text-style: bold;
        padding: 1 0;
    }

    /* Status bar */
    #status-bar {
        height: 1;
        background: $primary-background;
        padding: 0 1;
    }
    #status-engine {
        color: $text-muted;
    }
    #status-engine.running {
        color: $success;
    }
    #status-nodes {
        color: $text-muted;
        text-align: right;
    }
    """

    BINDINGS = [
        Binding("i", "import_nodes", "Import", tooltip="Import share links"),
        Binding("e", "edit_node", "Edit", tooltip="Edit selected node"),
        Binding("d", "delete_node", "Delete", tooltip="Delete selected node"),
        Binding("t", "test_latency", "Test", tooltip="Test latency of selected node"),
        Binding("T", "test_all", "Test All", tooltip="Test all nodes"),
        Binding("r", "routing_config", "Routing", tooltip="Configure split routing"),
        Binding("s", "toggle_engine", "Start/Stop", tooltip="Start/Stop sing-box"),
        Binding("p", "toggle_system_proxy", "SysProxy", tooltip="Toggle Windows system proxy"),
        Binding("l", "show_logs", "Logs", tooltip="View full logs"),
        Binding("q", "quit", "Quit", tooltip="Quit ProxyNet"),
    ]

    def __init__(self):
        super().__init__()
        self.storage = StorageManager()
        self.nodes: list[ProxyNode] = []
        self.app_config: AppConfig = AppConfig()
        self._log_fmt = LogFormatter()
        self._log_lines: list[str] = []
        self._sys_proxy_enabled = False
        self.engine = SingBoxEngine(
            config=self.app_config,
            on_log=self._on_engine_log,
            on_status_change=self._on_engine_status_change,
        )
        self.monitor = NodeMonitor(on_node_update=self._on_node_update)
        self._selected_node_id: Optional[str] = None

    def on_mount(self) -> None:
        """Called when the app is first mounted."""
        self._load_data()
        self._refresh_node_table()
        self._update_status_bar()
        self._update_detail_panel()
        self._update_routing_display()
        self._update_footer_hints()

    def _load_data(self) -> None:
        """Load nodes and config from storage."""
        self.nodes = self.storage.load_nodes()
        self.app_config = self.storage.load_config()
        self.engine._app_config = self.app_config

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main-container"):
            with Horizontal():
                # Left panel: node table
                with Vertical(id="left-panel"):
                    yield DataTable(id="node-table", cursor_type="row")
                    # Status bar
                    with Horizontal(id="status-bar"):
                        yield Static("Engine: Stopped", id="status-engine")
                        yield Static("Nodes: 0", id="status-nodes")

                # Right panel: details + routing + quick log
                with VerticalScroll(id="right-panel"):
                    # Node details
                    with Vertical(id="detail-section"):
                        yield Static("📋 Node Details", id="detail-title")
                        with Horizontal(classes="detail-row"):
                            yield Static("Name:", classes="detail-key")
                            yield Static("-", id="det-name", classes="detail-value")
                        with Horizontal(classes="detail-row"):
                            yield Static("Type:", classes="detail-key")
                            yield Static("-", id="det-type", classes="detail-value")
                        with Horizontal(classes="detail-row"):
                            yield Static("Host:", classes="detail-key")
                            yield Static("-", id="det-host", classes="detail-value")
                        with Horizontal(classes="detail-row"):
                            yield Static("Port:", classes="detail-key")
                            yield Static("-", id="det-port", classes="detail-value")
                        with Horizontal(classes="detail-row"):
                            yield Static("Status:", classes="detail-key")
                            yield Static("-", id="det-status", classes="detail-value")
                        with Horizontal(classes="detail-row"):
                            yield Static("Latency:", classes="detail-key")
                            yield Static("-", id="det-latency", classes="detail-value")
                        with Horizontal(classes="detail-row"):
                            yield Static("Network:", classes="detail-key")
                            yield Static("-", id="det-network", classes="detail-value")
                        with Horizontal(classes="detail-row"):
                            yield Static("TLS:", classes="detail-key")
                            yield Static("-", id="det-tls", classes="detail-value")
                        yield Horizontal(
                            Button("✏️ Edit", variant="primary", id="btn-edit"),
                            Button("🗑 Delete", variant="error", id="btn-delete"),
                            Button("⚡ Test", variant="default", id="btn-test"),
                            id="detail-buttons",
                        )

                    # Split routing display
                    with Vertical(id="routing-section"):
                        yield Static("🔀 Split Routing", id="routing-header")
                        yield Static("-", id="routing-private")
                        yield Static("-", id="routing-public")
                        yield Button("Configure Routing", variant="default", id="btn-routing")

                    # Engine controls
                    with Vertical(id="engine-section"):
                        yield Static("⚙️ Engine Control", id="engine-title")
                        yield Horizontal(
                            Button("▶ Start", variant="success", id="btn-start"),
                            Button("⏹ Stop", variant="error", id="btn-stop"),
                            id="engine-buttons",
                        )
                        yield Static("Status: Stopped", id="engine-status-text")

                    # Minimal log bar
                    with Vertical(id="log-section"):
                        yield Static("📜 Logs: press [l] or click →", id="log-title")
                        yield Button("📋 Open Log Viewer", variant="default", id="btn-logs")

        yield Footer()

    # ── Data table ────────────────────────────────────────────────

    def _refresh_node_table(self) -> None:
        """Populate the node DataTable."""
        table = self.query_one("#node-table", DataTable)
        table.clear(columns=True)

        if not table.columns:
            table.add_columns("Status", "Name", "Type", "Host", "Latency", "Group")

        for node in self.nodes:
            icon = STATUS_ICONS.get(node.status, "⚪")
            lat_str = f"{node.latency_ms}ms" if node.latency_ms > 0 else "---"
            table.add_row(
                icon,
                node.name or f"{node.proxy_type.value}-{node.host}",
                node.proxy_type.value.upper(),
                f"{node.host}:{node.port}",
                lat_str,
                node.group,
                key=node.id,
            )

        # Auto-select first row so user can immediately act on it
        if self.nodes:
            try:
                table.move_cursor(row=0)
                self._selected_node_id = self.nodes[0].id
            except Exception:
                pass  # Graceful fallback if table is empty or not ready

    def _update_node_row(self, node: ProxyNode) -> None:
        """Update a single row in the table."""
        try:
            table = self.query_one("#node-table", DataTable)
            if node.id in table.rows:
                icon = STATUS_ICONS.get(node.status, "⚪")
                lat_str = f"{node.latency_ms}ms" if node.latency_ms > 0 else "---"
                table.update_cell(node.id, 0, icon)
                table.update_cell(node.id, 4, lat_str)
                table.update_cell(node.id, 2, node.proxy_type.value.upper())
        except Exception:
            pass

    # ── Detail panel ──────────────────────────────────────────────

    def _update_detail_panel(self) -> None:
        """Update the right panel with selected node details."""
        node = None
        if self._selected_node_id:
            for n in self.nodes:
                if n.id == self._selected_node_id:
                    node = n
                    break

        default = "-"

        self._set_detail("det-name", node.name if node else default)
        self._set_detail("det-type", node.proxy_type.value.upper() if node else default)
        self._set_detail("det-host", f"{node.host}:{node.port}" if node else default)
        self._set_detail("det-port", str(node.port) if node else default)

        if node:
            icon = STATUS_ICONS.get(node.status, "⚪")
            self._set_detail("det-status", f"{icon} {node.status.value}")
        else:
            self._set_detail("det-status", default)

        if node:
            lat_str = f"{node.latency_ms}ms" if node.latency_ms > 0 else "N/A"
            self._set_detail("det-latency", lat_str)
        else:
            self._set_detail("det-latency", default)

        self._set_detail("det-network", f"{node.network} {'🔒' if node and node.tls else ''}" if node else default)
        self._set_detail("det-tls", "Yes" if node and node.tls else "No")

    def _set_detail(self, widget_id: str, value: str) -> None:
        """Safely set a detail widget's text."""
        try:
            self.query_one(f"#{widget_id}", Static).update(value)
        except NoMatches:
            pass

    # ── Routing display ───────────────────────────────────────────

    def _update_routing_display(self) -> None:
        """Update the split routing display."""
        routing = self.app_config.routing
        node_map = {n.id: n for n in self.nodes}

        # Private IP routing
        if routing.private_ip_mode == ExitMode.LOCAL:
            priv_text = "🏠 Private IP → [green]Direct (local)[/green]"
        elif routing.private_ip_node_id and routing.private_ip_node_id in node_map:
            n = node_map[routing.private_ip_node_id]
            priv_text = f"🏠 Private IP → [cyan]Remote: {n.name or n.host}[/cyan]"
        else:
            priv_text = "🏠 Private IP → [yellow]Not configured[/yellow]"

        # Public IP routing
        if routing.public_ip_mode == ExitMode.LOCAL:
            pub_text = "🌐 Public IP → [green]Direct (local)[/green]"
        elif routing.public_ip_node_id and routing.public_ip_node_id in node_map:
            n = node_map[routing.public_ip_node_id]
            pub_text = f"🌐 Public IP → [magenta]Remote: {n.name or n.host}[/magenta]"
        else:
            pub_text = "🌐 Public IP → [yellow]Not configured[/yellow]"

        try:
            self.query_one("#routing-private", Static).update(priv_text)
            self.query_one("#routing-public", Static).update(pub_text)
        except NoMatches:
            pass

    # ── Status bar ────────────────────────────────────────────────

    def _update_status_bar(self) -> None:
        """Update the status bar."""
        try:
            engine_status = self.query_one("#status-engine", Static)
            if self.engine.is_running:
                proxy = f"127.0.0.1:{self.app_config.mixed_port}"
                sys_proxy = "ON" if self._sys_proxy_enabled else "OFF"
                engine_status.update(
                    f"Engine: 🟢 Running  |  Proxy: {proxy}  |  SysProxy: {sys_proxy}  |  [p] Toggle"
                )
                engine_status.set_class(True, "running")
            else:
                sys_proxy = "ON" if self._sys_proxy_enabled else "OFF"
                engine_status.update(f"Engine: ⚪ Stopped  |  SysProxy: {sys_proxy}")
                engine_status.set_class(False, "running")
        except NoMatches:
            pass

        try:
            nodes_status = self.query_one("#status-nodes", Static)
            online = sum(1 for n in self.nodes if n.status == NodeStatus.ONLINE)
            offline = sum(1 for n in self.nodes if n.status == NodeStatus.OFFLINE)
            nodes_status.update(f"Nodes: {len(self.nodes)} ({online}↑ {offline}↓)")
        except NoMatches:
            pass

    def _update_footer_hints(self) -> None:
        """Update footer with keyboard shortcut hints."""
        try:
            footer = self.query_one(Footer)
            # Footer auto-generates from bindings
        except NoMatches:
            pass

    # ── Log handling ──────────────────────────────────────────────

    def _log_write(self, msg: str) -> None:
        """Write a Rich-markup log message to the buffer."""
        self._log_lines.append(msg)
        if len(self._log_lines) > 2000:
            self._log_lines = self._log_lines[-1000:]

    async def _on_engine_log(self, text: str) -> None:
        """Handle log messages from the engine with formatting."""
        formatted = self._log_fmt.format(text)
        if formatted is None:
            return
        self._log_lines.append(formatted)
        if len(self._log_lines) > 2000:
            self._log_lines = self._log_lines[-1000:]

    async def _on_engine_status_change(self, running: bool) -> None:
        """Handle engine status changes."""
        self._update_status_bar()
        if not running:
            self.notify("Engine STOPPED", severity="warning", timeout=5)

    async def _on_node_update(self, node: ProxyNode) -> None:
        """Handle node status updates from the monitor."""
        self._update_node_row(node)
        self._update_status_bar()
        self._update_detail_panel()
        # Save to storage
        self.storage.save_nodes(self.nodes)

    # ── Actions ───────────────────────────────────────────────────

    def action_import_nodes(self) -> None:
        """Open the import dialog."""
        self.push_screen(ImportDialog(), self._on_import_result)

    def _on_import_result(self, result: Optional[list[ProxyNode]]) -> None:
        """Handle import dialog result."""
        if result:
            for node in result:
                self.storage.add_node(node)
            self._load_data()
            self._refresh_node_table()
            self._update_status_bar()
            self._update_routing_display()

            self._log_write(f"[green]✓ Imported {len(result)} node(s)[/green]")

    def action_edit_node(self) -> None:
        """Edit the selected node."""
        node = self._get_selected_node()
        if node:
            self.push_screen(NodeEditScreen(node), self._on_edit_result)

    def _on_edit_result(self, result: Optional[ProxyNode]) -> None:
        """Handle edit dialog result."""
        if result:
            self.storage.update_node(result)
            self._load_data()
            self._refresh_node_table()
            self._update_detail_panel()
            self._update_routing_display()

    def action_delete_node(self) -> None:
        """Delete the selected node."""
        if not self._selected_node_id:
            self._log_write("[yellow]No node selected. Use arrow keys to select a node first.[/yellow]")
            return

        # Find node name before deleting
        node_name = self._selected_node_id
        for n in self.nodes:
            if n.id == self._selected_node_id:
                node_name = n.name or n.host
                break

        removed = self.storage.remove_node(self._selected_node_id)
        if not removed:
            self._log_write(f"[red]Failed to delete node: {node_name}[/red]")
            return

        self._selected_node_id = None
        self._load_data()
        self._refresh_node_table()
        self._update_detail_panel()
        self._update_status_bar()
        self._update_routing_display()

        self._log_write(f"[yellow]Deleted: {node_name}[/yellow]")

    async def action_test_latency(self) -> None:
        """Test latency of the selected node."""
        node = self._get_selected_node()
        if node:
            self._log_write(f"[info]Testing {node.name or node.host}...[/info]")
            latency = await self.monitor.test_single(node)
            if latency is not None:
                self._log_write(f"[green]  {node.name or node.host}: {latency}ms[/green]")
            else:
                self._log_write(f"[red]  {node.name or node.host}: unreachable[/red]")
            self._refresh_node_table()
            self._update_detail_panel()
            self._update_status_bar()

    async def action_test_all(self) -> None:
        """Test latency of all nodes."""
        self._log_write("[info]Testing all nodes...[/info]")
        results = await self.monitor.test_all(self.nodes)
        online = sum(1 for v in results.values() if v is not None)
        self._log_write(f"[green]✓ {online}/{len(results)} nodes reachable[/green]")
        self._refresh_node_table()
        self._update_detail_panel()
        self._update_status_bar()

    def action_routing_config(self) -> None:
        """Open routing configuration screen."""
        self.push_screen(
            RoutingConfigScreen(self.app_config.routing, self.nodes, self.app_config),
            self._on_routing_result,
        )

    def _on_routing_result(self, result) -> None:
        """Handle routing config result: (routing, tun_enabled) tuple or None."""
        if result:
            routing, tun_on = result
            self.app_config.routing = routing
            self.app_config.tun_mode = tun_on
            self.storage.save_config(self.app_config)
            self._update_routing_display()

            self._log_write(f"[green]Routing updated (TUN: {'ON' if tun_on else 'OFF'})[/green]")
            if self.engine.is_running:
                self._log_write("[info]Routing changed, restarting sing-box to apply settings...[/info]")
                self.create_task(self._reload_engine())

    async def _reload_engine(self) -> None:
        """Restart sing-box to apply updated routing configuration."""
        success = await self.engine.restart(self.nodes)
        if success:
            self._update_engine_status_ui()
            self._log_write("[green]sing-box restarted with new routing settings[/green]")
        else:
            self._log_write("[red]Failed to restart sing-box after routing update[/red]")

    async def action_toggle_engine(self) -> None:
        """Toggle the sing-box engine (start if stopped, stop if running)."""
        if self.engine.is_running:
            await self._action_stop_engine()
        else:
            self._log_write("[info]Starting sing-box...[/info]")
            success = await self.engine.start(self.nodes)
            if success:
                self._update_engine_status_ui()
                self._log_write("[green]Engine started[/green]")
            else:
                self._log_write("[red]Failed to start sing-box. Check logs for details.[/red]")

    async def _action_stop_engine(self) -> None:
        """Stop the sing-box engine."""

        if not self.engine.is_running:
            self._log_write("[yellow]Engine is not running.[/yellow]")
            return

        self._log_write("[info]Stopping sing-box...[/info]")
        await self.engine.stop()
        self._update_engine_status_ui()

    def _update_engine_status_ui(self) -> None:
        """Update engine status text and buttons after state change."""
        try:
            status_text = self.query_one("#engine-status-text", Static)
            if self.engine.is_running:
                status_text.update("[green]Status: Running[/green]")
            else:
                status_text.update("Status: Stopped")
        except NoMatches:
            pass
        self._update_status_bar()

    def action_quit(self) -> None:
        """Quit ProxyNet — stop engine and disable system proxy."""
        # Disable system proxy if we enabled it
        if self._sys_proxy_enabled:
            self._disable_system_proxy()
        # Stop engine
        import asyncio
        asyncio.create_task(self._cleanup_and_exit())

    async def _cleanup_and_exit(self) -> None:
        if self.engine.is_running:
            await self.engine.stop()
        self.exit()

    def _disable_system_proxy(self) -> None:
        """Force-disable Windows system proxy."""
        import sys as _sys
        if _sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                    0, winreg.KEY_READ | winreg.KEY_WRITE,
                )
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                winreg.CloseKey(key)
                import ctypes
                ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 0, 2, 5000, None)
                self._sys_proxy_enabled = False
            except Exception:
                pass

    def action_toggle_system_proxy(self) -> None:
        """Toggle Windows system proxy on/off."""
        if not self.engine.is_running:
            self._log_write("[yellow]Engine not running. Start engine first.[/yellow]")
            return

        import sys as _sys
        proxy_addr = f"127.0.0.1:{self.app_config.mixed_port}"

        if _sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                    0, winreg.KEY_READ | winreg.KEY_WRITE,
                )
                current = winreg.QueryValueEx(key, "ProxyEnable")[0]
                if current:
                    # Disable
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                    winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "")
                    self._sys_proxy_enabled = False
                    self._log_write(f"[green]System proxy DISABLED[/green]")
                else:
                    # Enable — also set ProxyOverride empty so local addresses go through proxy
                    winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_addr)
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                    winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "")
                    self._sys_proxy_enabled = True
                    self._log_write(f"[green]System proxy ENABLED -> {proxy_addr} (no bypass)[/green]")
                winreg.CloseKey(key)
                # Notify system
                import ctypes
                ctypes.windll.user32.SendMessageTimeoutW(
                    0xFFFF, 0x001A, 0, 0, 2, 5000, None,
                )
            except Exception as e:
                self._log_write(f"[red]Failed to toggle system proxy: {e}[/red]")
        else:
            self._log_write(f"[info]Run in terminal:[/info]")
            self._log_write(f"  export http_proxy=http://{proxy_addr}")
            self._log_write(f"  export https_proxy=http://{proxy_addr}")
            self._log_write(f"  export ALL_PROXY=socks5://{proxy_addr}")
        self._update_status_bar()

    def action_show_logs(self) -> None:
        """Open the floating log viewer modal."""
        self.push_screen(LogViewerScreen(self._log_lines))

    # ── Event handlers ────────────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle node selection in the table."""
        if event.row_key and event.row_key.value:
            self._selected_node_id = str(event.row_key.value)
            self._update_detail_panel()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks in the main UI."""
        btn_id = event.button.id
        if btn_id == "btn-edit":
            self.action_edit_node()
        elif btn_id == "btn-delete":
            self.action_delete_node()
        elif btn_id == "btn-test":
            self.create_task(self.action_test_latency())  # type: ignore
        elif btn_id == "btn-routing":
            self.action_routing_config()
        elif btn_id == "btn-start":
            self.create_task(self.action_toggle_engine())  # type: ignore
        elif btn_id == "btn-stop":
            self.create_task(self._action_stop_engine())  # type: ignore
        elif btn_id == "btn-logs":
            self.action_show_logs()

    def on_key(self, event: events.Key) -> None:
        """Handle global key events."""
        # ESC to clear selection
        if event.key == "escape":
            self._selected_node_id = None
            self._update_detail_panel()

    # ── Helpers ───────────────────────────────────────────────────

    def _get_selected_node(self) -> Optional[ProxyNode]:
        """Get the currently selected node."""
        if not self._selected_node_id:
            return None
        for n in self.nodes:
            if n.id == self._selected_node_id:
                return n
        return None

    def create_task(self, coro) -> None:
        """Create a background task safely."""
        import asyncio
        asyncio.create_task(coro)
