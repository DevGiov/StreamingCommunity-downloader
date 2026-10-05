"""Lifecycle manager for the MCP server.

Runs the MCP Starlette ASGI app over SSE using uvicorn in a dedicated daemon thread,
allowing dynamic enabling/disabling and port reconfiguration from the UI.
"""

import asyncio
import logging
import secrets
import threading
from typing import Optional
import uvicorn

from mcp.server.mcpserver import MCPServer
from mcp.server.sse import TransportSecuritySettings

from app.auth.models import get_setting, set_setting
from app.config import get_settings
from app.mcp.auth import MCPAuthMiddleware, SETTING_MCP_TOKEN
from app.mcp.tools import mcp_server

logger = logging.getLogger(__name__)


def ensure_mcp_token() -> str:
    """Ensure an MCP token exists in the database. Generate one if missing."""
    try:
        token = (get_setting(SETTING_MCP_TOKEN) or "").strip()
        if not token:
            token = secrets.token_hex(24)
            set_setting(SETTING_MCP_TOKEN, token)
            logger.info("Generated new default MCP token")
        return token
    except Exception as e:
        logger.warning("Could not read/set MCP token in database: %s", e)
        return ""


class MCPServerManager:
    """Singleton manager for the background MCP server process."""

    def __init__(self):
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running: bool = False
        self._host: str = "0.0.0.0"
        self._port: int = 8001
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running and self._server is not None and self._server.started

    @property
    def port(self) -> int:
        return self._port

    @property
    def host(self) -> str:
        return self._host

    async def start(self, host: Optional[str] = None, port: Optional[int] = None):
        """Start the MCP server."""
        async with self._lock:
            settings = get_settings()
            target_host = host or settings.get("mcp_host", "0.0.0.0")
            target_port = port or settings.get("mcp_port", 8001)

            if self.is_running:
                if self._host == target_host and self._port == target_port:
                    logger.debug("MCP server already running on %s:%d", target_host, target_port)
                    return
                # Port or host changed: stop first
                await self._stop_locked()

            # Ensure we have a security token
            ensure_mcp_token()

            self._host = target_host
            self._port = target_port

            # Build Starlette app with authentication
            app = mcp_server.sse_app(
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
            )
            app.add_middleware(MCPAuthMiddleware)

            config = uvicorn.Config(
                app=app,
                host=self._host,
                port=self._port,
                log_level="warning",
                access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._running = True

            def _run():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                try:
                    loop.run_until_complete(self._server.serve())
                except Exception:
                    logger.exception("MCP server crashed unexpectedly")
                finally:
                    self._running = False
                    loop.close()

            self._thread = threading.Thread(target=_run, name="mcp-server", daemon=True)
            self._thread.start()

            # Wait briefly for uvicorn to bind socket and mark started
            for _ in range(60):
                if (self._server and self._server.started) or not self._running:
                    break
                await asyncio.sleep(0.05)
            logger.info("MCP server started on %s:%d", self._host, self._port)

    async def stop(self):
        """Stop the running MCP server."""
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self):
        if self._server:
            self._server.should_exit = True
            if self._thread and self._thread.is_alive():
                await asyncio.to_thread(self._thread.join, 3.0)
            self._server = None
            self._thread = None
            self._loop = None
            self._running = False
            logger.info("MCP server stopped")

    async def restart(self, host: Optional[str] = None, port: Optional[int] = None):
        """Restart the MCP server with given or current configuration."""
        await self.stop()
        await self.start(host=host, port=port)

    async def apply_settings(self, settings: dict):
        """React to settings change from UI."""
        enabled = settings.get("mcp_enabled", False)
        port = settings.get("mcp_port", 8001)
        host = settings.get("mcp_host", "0.0.0.0")

        if enabled:
            if not self.is_running or self._port != port or self._host != host:
                await self.restart(host=host, port=port)
        else:
            if self.is_running:
                await self.stop()


mcp_manager = MCPServerManager()
