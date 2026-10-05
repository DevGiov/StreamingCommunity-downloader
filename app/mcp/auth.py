"""Authentication middleware for the MCP server.

Supports Bearer token authentication via the Authorization header or
a ?token= query parameter (useful for SSE clients that cannot set headers).
"""

import hmac
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Protects the MCP server endpoints."""

    async def dispatch(self, request: Request, call_next) -> Response:
        configured = get_configured_token()
        # If no token is set in the database, allow open access
        if not configured:
            return await call_next(request)

        # Allow OPTIONS for CORS preflight if any
        if request.method == "OPTIONS":
            return await call_next(request)

        # 1. Check Authorization header: Bearer <token>
        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

        # 2. Check query parameter: ?token=<token>
        if not token:
            token = request.query_params.get("token", "").strip()

        if not token or not verify_token(token):
            logger.warning("Rejected unauthorized MCP request to %s", request.url.path)
            return JSONResponse(
                {"error": "Unauthorized", "detail": "Valid MCP Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
