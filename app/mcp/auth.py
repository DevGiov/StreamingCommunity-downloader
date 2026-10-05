"""Authentication middleware for the MCP server.

Supports Bearer token authentication via the Authorization header or
a ?token= query parameter (useful for SSE clients that cannot set headers).
Uses pure ASGI to avoid buffering or interfering with Server-Sent Events streams.
"""

import hmac
import logging
from urllib.parse import parse_qs

from starlette.responses import JSONResponse

from app.auth.models import get_setting

logger = logging.getLogger(__name__)

SETTING_MCP_TOKEN = "mcp_token"


def get_configured_token() -> str:
    """Return the currently configured MCP token, or empty string if none."""
    return (get_setting(SETTING_MCP_TOKEN) or "").strip()


def verify_token(provided_token: str) -> bool:
    """Constant-time token verification."""
    configured = get_configured_token()
    if not configured:
        # If no token is configured, allow access (open mode)
        return True
    return hmac.compare_digest(configured.encode("utf-8"), provided_token.strip().encode("utf-8"))


class MCPAuthMiddleware:
    """Pure ASGI middleware protecting the MCP server endpoints."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        configured = get_configured_token()
        if not configured:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # 1. Check Authorization header: Bearer <token>
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

        # 2. Check query parameter: ?token=<token>
        if not token:
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            token = qs.get("token", [""])[0].strip()

        if not token or not verify_token(token):
            path = scope.get("path", "")
            logger.warning("Rejected unauthorized MCP request to %s", path)
            response = JSONResponse(
                {"error": "Unauthorized", "detail": "Valid MCP Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
