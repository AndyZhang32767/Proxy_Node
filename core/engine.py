"""sing-box process manager.

Manages the lifecycle of the sing-box proxy process:
- Detect/find sing-box binary
- Start/stop the process
- Capture and stream logs
- Monitor process health
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Optional, Callable, Awaitable

from models.node import ProxyNode, AppConfig
from .config_generator import generate_singbox_config, save_config_to_file


def _get_project_bin_dir() -> Path:
    """Get the project's local bin/ directory."""
    # Find the project root (parent of core/)
    core_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    project_root = core_dir.parent
    return project_root / "bin"


def find_sing_box() -> Optional[str]:
    """Find sing-box binary on the system.

    Checks in order:
    1. Bundled binary in <project>/bin/sing-box(.exe)
    2. Configured path in app config
    3. PATH environment variable
    4. Common install locations for this platform
    """
    # 1. Check bundled bin/ first (no system changes needed)
    bin_dir = _get_project_bin_dir()
    if sys.platform == "win32":
        bundled = bin_dir / "sing-box.exe"
    else:
        bundled = bin_dir / "sing-box"
    if bundled.exists() and bundled.is_file():
        return str(bundled)

    # 2. Check PATH
    found = shutil.which("sing-box")
    if found:
        return found

    # 3. Check common locations
    if sys.platform == "win32":
        common_paths = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "sing-box" / "sing-box.exe",
            Path.home() / "scoop" / "shims" / "sing-box.exe",
            Path("C:/") / "sing-box" / "sing-box.exe",
        ]
    elif sys.platform == "darwin":
        common_paths = [
            Path("/usr/local/bin/sing-box"),
            Path.home() / ".local" / "bin" / "sing-box",
        ]
    else:
        common_paths = [
            Path("/usr/local/bin/sing-box"),
            Path("/usr/bin/sing-box"),
            Path.home() / ".local" / "bin" / "sing-box",
        ]

    for p in common_paths:
        if p.exists() and p.is_file():
            return str(p)

    return None


class SingBoxEngine:
    """Manages the sing-box proxy process."""

    def __init__(
        self,
        config: AppConfig,
        on_log: Optional[Callable[[str], Awaitable[None]]] = None,
        on_status_change: Optional[Callable[[bool], Awaitable[None]]] = None,
    ):
        self._app_config = config
        self._process: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._config_path: Optional[str] = None
        self._on_log = on_log
        self._on_status_change = on_status_change
        self._log_buffer: list[str] = []
        self._max_log_lines = 500
        self._auto_restart = True
        self._restart_count = 0
        self._max_restarts = 3
        self._nodes: list = []  # Stored for auto-restart
        self._stopping = False

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    @property
    def log_lines(self) -> list[str]:
        return list(self._log_buffer)

    async def start(
        self,
        nodes: list[ProxyNode],
        active_node_ids: Optional[set[str]] = None,
    ) -> bool:
        """Generate config and start sing-box.

        Returns True if started successfully.
        """
        if self._running:
            await self._emit_log("[WARN] sing-box is already running")
            return True

        self._nodes = list(nodes)
        self._restart_count = 0
        self._stopping = False

        # Find binary
        binary = self._app_config.sing_box_path or find_sing_box()
        if not binary:
            await self._emit_log("[ERROR] sing-box binary not found. Please install sing-box or set the path in settings.")
            return False

        if not os.path.exists(binary):
            await self._emit_log(f"[ERROR] sing-box binary not found at: {binary}")
            return False

        await self._emit_log(f"[INFO] Using sing-box at: {binary}")

        # Generate config
        try:
            config_dict = generate_singbox_config(nodes, self._app_config, active_node_ids)
        except Exception as e:
            await self._emit_log(f"[ERROR] Failed to generate config: {e}")
            return False

        # Write config to temp file
        tmpdir = tempfile.gettempdir()
        self._config_path = os.path.join(tmpdir, f"proxynet-singbox-{os.getpid()}.json")
        save_config_to_file(config_dict, self._config_path)
        await self._emit_log(f"[INFO] Config written to: {self._config_path}")

        # Start process
        try:
            # sing-box 1.12+ needs these for legacy config format compatibility
            env = os.environ.copy()
            env["ENABLE_DEPRECATED_LEGACY_DNS_SERVERS"] = "true"
            env["ENABLE_DEPRECATED_MISSING_DOMAIN_RESOLVER"] = "true"

            if sys.platform == "win32":
                self._process = await asyncio.create_subprocess_exec(
                    binary, "run", "-c", self._config_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            else:
                self._process = await asyncio.create_subprocess_exec(
                    binary, "run", "-c", self._config_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    preexec_fn=os.setsid,
                )

            self._running = True
            await self._emit_log(f"[INFO] sing-box started (PID: {self._process.pid})")

            if self._on_status_change:
                await self._on_status_change(True)

            # Start log readers
            asyncio.create_task(self._read_stream(self._process.stdout, "OUT"))
            asyncio.create_task(self._read_stream(self._process.stderr, "ERR"))

            # Monitor process
            asyncio.create_task(self._monitor_process())

            return True

        except FileNotFoundError:
            await self._emit_log(f"[ERROR] Could not execute: {binary}")
            return False
        except Exception as e:
            await self._emit_log(f"[ERROR] Failed to start sing-box: {e}")
            return False

    async def stop(self) -> None:
        """Stop the sing-box process gracefully."""
        if not self._running or self._process is None:
            return

        self._stopping = True
        await self._emit_log("[INFO] Stopping sing-box...")

        try:
            if sys.platform == "win32":
                self._process.terminate()
            else:
                # Send SIGTERM to process group
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    self._process.terminate()

            # Wait for graceful shutdown
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                await self._emit_log("[WARN] sing-box did not stop gracefully, force killing...")
                self._process.kill()
                await self._process.wait()

            await self._emit_log("[INFO] sing-box stopped")

        except Exception as e:
            await self._emit_log(f"[ERROR] Error stopping sing-box: {e}")
            if self._process:
                try:
                    self._process.kill()
                except Exception:
                    pass

        self._running = False
        self._process = None
        self._stopping = False

        # Clean up config file
        if self._config_path and os.path.exists(self._config_path):
            try:
                os.remove(self._config_path)
            except OSError:
                pass

        if self._on_status_change:
            await self._on_status_change(False)

    async def restart(
        self,
        nodes: list[ProxyNode],
        active_node_ids: Optional[set[str]] = None,
    ) -> bool:
        """Restart sing-box with new configuration."""
        await self.stop()
        # Brief pause to let ports release
        await asyncio.sleep(0.5)
        return await self.start(nodes, active_node_ids)

    async def _read_stream(self, stream, tag: str) -> None:
        """Read lines from a process stream and emit as logs."""
        if stream is None:
            return
        try:
            while self._running:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    await self._emit_log(f"[{tag}] {text}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _monitor_process(self) -> None:
        """Monitor the sing-box process for unexpected exit. Auto-restart on crash."""
        if self._process is None:
            return
        try:
            returncode = await self._process.wait()
            self._running = False

            if returncode != 0:
                if self._stopping:
                    await self._emit_log("[INFO] sing-box stopped")
                else:
                    await self._emit_log(f"[ERROR] sing-box crashed (exit code {returncode})")
                    # Auto-restart if enabled
                    if self._auto_restart and self._restart_count < self._max_restarts:
                        self._restart_count += 1
                        await self._emit_log(
                            f"[WARN] Auto-restarting sing-box (attempt {self._restart_count}/{self._max_restarts})..."
                        )
                        await asyncio.sleep(1)
                        await self.start(self._nodes)
                        return
                    else:
                        await self._emit_log("[ERROR] sing-box stopped unexpectedly — check stderr above for details")
            else:
                await self._emit_log("[INFO] sing-box exited normally")

            if self._on_status_change:
                await self._on_status_change(False)

        except asyncio.CancelledError:
            pass

    async def _emit_log(self, text: str) -> None:
        """Add a line to the log buffer and notify listeners."""
        self._log_buffer.append(text)
        # Trim buffer
        if len(self._log_buffer) > self._max_log_lines:
            self._log_buffer = self._log_buffer[-self._max_log_lines:]
        if self._on_log:
            await self._on_log(text)
