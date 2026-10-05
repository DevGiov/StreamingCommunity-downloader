"""Tests for the Model Context Protocol (MCP) server, tools, and endpoints."""

import asyncio
import pytest
from starlette.requests import Request
from starlette.responses import Response

from app import config
from app.auth.models import get_setting, set_setting
from app.auth.permissions import Permission
from app.mcp import mcp_manager, mcp_server
from app.mcp.auth import MCPAuthMiddleware, SETTING_MCP_TOKEN, verify_token
from app.mcp.server import ensure_mcp_token
from tests.conftest import do_setup, make_user, session_for


def test_mcp_tools_registration_and_list():
    """Verify that all core MCP tools are registered and available in the server."""
    async def _test():
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        expected_tools = {
            "search_content",
            "get_content_details",
            "get_series_episodes",
            "get_anime_episodes",
            "get_home_shelves",
            "download_film",
            "download_episode",
            "download_season",
            "download_anime_episode",
            "list_downloads",
            "get_download_progress",
            "cancel_download",
            "retry_download",
            "follow_series",
            "list_watched_series",
            "unfollow_series",
            "check_series_updates",
            "submit_request",
            "list_requests",
            "approve_request",
            "get_system_status",
            "list_libraries",
        }
        missing = expected_tools - tool_names
        assert not missing, f"Missing registered MCP tools: {missing}"

    asyncio.run(_test())


import json


def _parse_mcp_result(result) -> dict:
    assert not result.is_error
    assert result.content and len(result.content) > 0
    return json.loads(result.content[0].text)


def test_mcp_system_status_tool():
    """Verify get_system_status returns valid panel information."""
    async def _test():
        result = await mcp_server.call_tool("get_system_status", {})
        data = _parse_mcp_result(result)
        assert "panel_version" in data
        assert "storage" in data
        assert "active_jobs_count" in data

    asyncio.run(_test())


def test_mcp_list_libraries_tool():
    """Verify list_libraries returns libraries configuration."""
    async def _test():
        result = await mcp_server.call_tool("list_libraries", {})
        data = _parse_mcp_result(result)
        assert "libraries" in data
        assert "excluded_folders" in data

    asyncio.run(_test())


def test_mcp_list_downloads_and_progress():
    """Verify list_downloads and get_download_progress return proper job structures."""
    async def _test():
        result = await mcp_server.call_tool("list_downloads", {"status": "all"})
        data = _parse_mcp_result(result)
        assert "jobs" in data
        assert "count" in data

        res_not_found = await mcp_server.call_tool("get_download_progress", {"job_id": "non_existent"})
        data_not_found = _parse_mcp_result(res_not_found)
        assert "error" in data_not_found

    asyncio.run(_test())


def test_mcp_cancel_download_tool():
    """Verify cancel_download tool behaviour on non-existent jobs."""
    async def _test():
        result = await mcp_server.call_tool("cancel_download", {"job_id": "fake_id"})
        data = _parse_mcp_result(result)
        assert data["ok"] is False

    asyncio.run(_test())


def test_mcp_search_content_no_domain():
    """Verify search handles empty domain gracefully."""
    async def _test():
        config.update_data({"domain": ""})
        result = await mcp_server.call_tool("search_content", {"query": "test", "source": "streamingcommunity"})
        data = _parse_mcp_result(result)
        assert "errors" in data
        assert any("No domain configured" in e for e in data["errors"])

    asyncio.run(_test())


def test_mcp_token_management(client):
    """Verify token generation, storage and verification."""
    set_setting(SETTING_MCP_TOKEN, "")
    token = ensure_mcp_token()
    assert len(token) >= 32
    assert get_setting(SETTING_MCP_TOKEN) == token
    assert verify_token(token) is True
    assert verify_token("wrong_token") is False


def test_mcp_auth_middleware(client):
    """Verify MCPAuthMiddleware enforces Bearer authentication."""
    async def _test():
        set_setting(SETTING_MCP_TOKEN, "secret-test-token")
        middleware = MCPAuthMiddleware(app=None)

        async def dummy_call_next(request: Request):
            return Response("ok", status_code=200)

        # 1. Missing token -> 401
        scope_no_auth = {
            "type": "http",
            "method": "GET",
            "path": "/sse",
            "headers": [],
            "query_string": b"",
        }
        req_no_auth = Request(scope_no_auth)
        res_no_auth = await middleware.dispatch(req_no_auth, dummy_call_next)
        assert res_no_auth.status_code == 401

        # 2. Invalid token -> 401
        scope_bad_auth = {
            "type": "http",
            "method": "GET",
            "path": "/sse",
            "headers": [(b"authorization", b"Bearer wrong-token")],
            "query_string": b"",
        }
        req_bad_auth = Request(scope_bad_auth)
        res_bad_auth = await middleware.dispatch(req_bad_auth, dummy_call_next)
        assert res_bad_auth.status_code == 401

        # 3. Valid Bearer token -> 200
        scope_good_auth = {
            "type": "http",
            "method": "GET",
            "path": "/sse",
            "headers": [(b"authorization", b"Bearer secret-test-token")],
            "query_string": b"",
        }
        req_good_auth = Request(scope_good_auth)
        res_good_auth = await middleware.dispatch(req_good_auth, dummy_call_next)
        assert res_good_auth.status_code == 200

        # 4. Valid query param token (?token=...) -> 200
        scope_param_auth = {
            "type": "http",
            "method": "GET",
            "path": "/sse",
            "headers": [],
            "query_string": b"token=secret-test-token",
        }
        req_param_auth = Request(scope_param_auth)
        res_param_auth = await middleware.dispatch(req_param_auth, dummy_call_next)
        assert res_param_auth.status_code == 200

    asyncio.run(_test())


def test_mcp_api_endpoints(client, admin_credentials):
    """Test /api/mcp/status and /api/mcp/token/regenerate endpoints."""
    do_setup(client, admin_credentials)
    user = make_user("admin", "admin-id", int(Permission.MANAGE_SETTINGS))
    _, csrf = user, session_for(client, user.id)

    # Status
    res = client.get("/api/mcp/status", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    data = res.json()
    assert "enabled" in data
    assert "port" in data
    assert "token" in data
    assert "sse_url" in data

    old_token = data["token"]

    # Regenerate token
    res_regen = client.post("/api/mcp/token/regenerate", json={}, headers={"X-CSRF-Token": csrf})
    assert res_regen.status_code == 200
    new_token = res_regen.json()["token"]
    assert new_token != old_token
    assert get_setting(SETTING_MCP_TOKEN) == new_token


def test_mcp_server_manager_lifecycle(client):
    """Verify MCPServerManager start, stop, and apply_settings."""
    async def _test():
        test_port = 8991
        try:
            await mcp_manager.start(host="127.0.0.1", port=test_port)
            assert mcp_manager.is_running is True
            assert mcp_manager.port == test_port

            # Test apply_settings disabling
            await mcp_manager.apply_settings({"mcp_enabled": False, "mcp_port": test_port})
            assert mcp_manager.is_running is False
        finally:
            await mcp_manager.stop()

    asyncio.run(_test())
