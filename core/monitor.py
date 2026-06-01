"""Status monitor for proxy nodes.

Asynchronously tests node latency via TCP connection and updates node status.
Runs periodic health checks.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Callable, Awaitable

from models.node import ProxyNode, NodeStatus


async def test_latency(host: str, port: int, timeout: float = 5.0) -> Optional[int]:
    """Test TCP latency to a host:port.

    Returns latency in milliseconds, or None if unreachable.
    """
    try:
        start = time.monotonic()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        elapsed = int((time.monotonic() - start) * 1000)
        writer.close()
        await writer.wait_closed()
        return elapsed
    except (asyncio.TimeoutError, OSError, Exception):
        return None


class NodeMonitor:
    """Monitors proxy node health via periodic latency tests."""

    def __init__(
        self,
        interval: float = 30.0,
        on_node_update: Optional[Callable[[ProxyNode], Awaitable[None]]] = None,
    ):
        self.interval = interval
        self._on_node_update = on_node_update
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self, nodes: list[ProxyNode]) -> None:
        """Start periodic monitoring."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(nodes))

    async def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def test_single(self, node: ProxyNode) -> Optional[int]:
        """Test a single node's latency and update its status."""
        node.status = NodeStatus.TESTING
        if self._on_node_update:
            await self._on_node_update(node)

        latency = await test_latency(node.host, node.port)

        if latency is not None:
            node.latency_ms = latency
            node.status = NodeStatus.ONLINE
        else:
            node.latency_ms = 0
            node.status = NodeStatus.OFFLINE

        if self._on_node_update:
            await self._on_node_update(node)

        return latency

    async def test_all(self, nodes: list[ProxyNode]) -> dict[str, Optional[int]]:
        """Test all nodes concurrently. Returns {node_id: latency_ms}."""
        results: dict[str, Optional[int]] = {}

        async def test_and_record(node: ProxyNode):
            results[node.id] = await self.test_single(node)

        tasks = [test_and_record(n) for n in nodes]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results

    async def _monitor_loop(self, nodes: list[ProxyNode]) -> None:
        """Periodic health check loop."""
        while self._running:
            await self.test_all(nodes)
            await asyncio.sleep(self.interval)
