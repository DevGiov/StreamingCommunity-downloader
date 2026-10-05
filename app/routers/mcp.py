import logging
import secrets
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth.deps import require
from app.auth.models import set_setting
from app.auth.permissions import Permission
from app.config import get_settings
from app.mcp.auth import SETTING_MCP_TOKEN
from app.mcp.server import ensure_mcp_token, mcp_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

CAN_MANAGE = [Depends(require(Permission.MANAGE_SETTINGS))]


@router.get("/status", dependencies=CAN_MANAGE)
def get_mcp_status(request: Request):
    """Return runtime MCP status, active port, and token."""
    settings = get_settings()
    enabled = settings.get("mcp_enabled", False)
    port = settings.get("mcp_port", 8001)
    host = settings.get("mcp_host", "0.0.0.0")
    token = ensure_mcp_token()

    # Determine client connect host for convenience
    client_host = request.url.hostname or "127.0.0.1"
    sse_url = f"http://{client_host}:{port}/sse"

    return {
        "enabled": enabled,
        "running": mcp_manager.is_running,
        "port": port,
        "host": host,
        "token": token,
        "sse_url": sse_url,
    }


@router.post("/token/regenerate", dependencies=CAN_MANAGE)
def regenerate_mcp_token():
    """Generate and store a new random MCP authentication token."""
    new_token = secrets.token_hex(24)
    set_setting(SETTING_MCP_TOKEN, new_token)
    logger.info("Regenerated MCP token")
    return {"token": new_token}
