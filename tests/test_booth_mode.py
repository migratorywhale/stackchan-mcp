import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mcp_server import mcp_tools, stackchan_config
from mcp_server.stackchan_client import StackchanClient

STATUS_FIELDS = ("success", "booth_mode", "persisted", "capture_allowed", "mic_running")


def booth_status(enabled=True, **overrides):
    return {
        "success": True,
        "booth_mode": enabled,
        "persisted": True,
        "capture_allowed": not enabled,
        "mic_running": not enabled,
        **overrides,
    }


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


def forbidden(*_args, **_kwargs):
    raise AssertionError("Unexpected network, device, subprocess, or audio operation")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith(("STACKCHAN_", "FISH_AUDIO_")) or name in {
            "MAC_IP", "TTS_ENGINE", "AUDIO_SERVE_PORT", "EDGE_TTS_BIN"
        }:
            monkeypatch.delenv(name)
    monkeypatch.setattr(stackchan_config, "load_dotenv", lambda: None)
    monkeypatch.setattr(stackchan_config, "resolve_mac_ip", lambda *_args: "192.0.2.10")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("mcp_server.stackchan_client.socket.create_connection", forbidden)
    monkeypatch.setattr("mcp_server.stackchan_client.subprocess.run", forbidden)
    monkeypatch.setattr(mcp_tools, "signal_face_tracking", forbidden)
    monkeypatch.setattr(mcp_tools, "start_audio_server", forbidden)
    monkeypatch.setattr(mcp_tools, "AUDIO_DIR", tmp_path)


@pytest.fixture
def config():
    return replace(stackchan_config.load_config(), stackchan_ip="192.0.2.20")


@pytest.fixture
def device():
    client = Mock(spec=["get_booth_mode", "set_booth_mode"])
    client.get_booth_mode.return_value = booth_status()
    client.set_booth_mode.side_effect = booth_status
    return client


@pytest.fixture
def mcp(config, device):
    mcp = FastMCP("booth-test")
    mcp_tools.register_tools(mcp, device, config, image_cls=None)
    return mcp


@pytest.fixture
def tool(mcp):
    return mcp._tool_manager.get_tool("stackchan_booth_mode")


@pytest.mark.parametrize("transport", ["direct", "relay", "curl"])
@pytest.mark.parametrize("enabled", [None, True, False])
def test_client_maps_booth_status_and_explicit_writes(config, monkeypatch, transport, enabled):
    calls = []
    body = booth_status(True if enabled is None else enabled)
    expected_method = "GET" if enabled is None else "POST"
    payload = None if enabled is None else {"enabled": enabled}
    timeout = config.http_status_timeout if enabled is None else config.http_command_timeout
    if transport == "relay":
        config = replace(config, transport="relay", relay_token="test-relay-token")
        timeout = max(timeout, 22)
    if transport == "curl":
        monkeypatch.setenv("STACKCHAN_HTTP_TRANSPORT", "curl")

        def run(command, **kwargs):
            calls.append(command)
            assert command[:2] == ["/usr/bin/curl", "--disable"]
            assert command[command.index("--request") + 1] == expected_method
            assert command[command.index("--max-time") + 1] == str(timeout)
            assert command[-1] == "http://192.0.2.20:80/booth"
            assert "--retry" not in command
            assert "--location" not in command
            assert kwargs["input"] == (None if payload is None else json.dumps(payload).encode())
            return SimpleNamespace(returncode=0, stdout=json.dumps(body).encode() + b"\n200", stderr=b"")

        monkeypatch.setattr("mcp_server.stackchan_client.subprocess.run", run)
    else:
        def request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            assert method.upper() == expected_method
            assert url == (
                "http://127.0.0.1:8766/device/booth" if transport == "relay"
                else "http://192.0.2.20:80/booth"
            )
            assert kwargs.get("json") == payload
            assert kwargs["allow_redirects"] is False
            assert kwargs["timeout"] == timeout
            assert session.get_adapter(url).max_retries.total == 0
            if transport == "relay":
                assert session.trust_env is False
                assert kwargs["headers"]["Authorization"] == "Bearer test-relay-token"
            return response(body)

        monkeypatch.setattr("requests.sessions.Session.request", request)
    client = StackchanClient(config)
    result = client.get_booth_mode() if enabled is None else client.set_booth_mode(enabled)
    assert result == body
    assert len(calls) == 1


@pytest.mark.parametrize("enabled", [None, 0, 1, 0.0, "true", "false", [], {}])
def test_client_rejects_nonboolean_writes_without_a_request(config, enabled):
    with pytest.raises(ValueError, match="boolean"):
        StackchanClient(config).set_booth_mode(enabled)


@pytest.mark.parametrize("transport", ["direct", "relay", "curl"])
@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("code", [302, 307, 400, 404, 409, 500, 503, 504])
def test_client_rejects_http_errors_without_retry(config, monkeypatch, transport, enabled, code):
    calls = []
    body = {"success": False, "error": "transition incomplete"}
    if transport == "relay":
        config = replace(config, transport="relay", relay_token="test-relay-token")
    if transport == "curl":
        monkeypatch.setenv("STACKCHAN_HTTP_TRANSPORT", "curl")

        def run(*_args, **_kwargs):
            calls.append(code)
            return SimpleNamespace(returncode=0, stdout=json.dumps(body).encode() + f"\n{code}".encode(), stderr=b"")

        monkeypatch.setattr("mcp_server.stackchan_client.subprocess.run", run)
    else:
        def request(*_args, **kwargs):
            calls.append(code)
            assert kwargs["allow_redirects"] is False
            return response(body, code)

        monkeypatch.setattr("requests.sessions.Session.request", request)
    client = StackchanClient(config)
    with pytest.raises(requests.HTTPError) as error:
        client.get_booth_mode() if enabled is None else client.set_booth_mode(enabled)
    assert error.value.response.status_code == code
    assert error.value.response.json() == body
    assert calls == [code]


@pytest.mark.parametrize("arguments", [{}, {"enabled": None}, {"enabled": True}, {"enabled": False}])
def test_tool_only_queries_or_explicitly_sets_once(tool, device, arguments):
    content = asyncio.run(tool.run(arguments, convert_result=True))
    json.dumps(content[1])
    result = json.loads(content[1]["result"])
    enabled = arguments.get("enabled")
    assert result["success"] is True
    assert result["outcome"] == "confirmed"
    assert result["requested"] is enabled
    if enabled is None:
        assert result["operation"] == "query"
        device.get_booth_mode.assert_called_once_with()
        device.set_booth_mode.assert_not_called()
    else:
        assert result["operation"] == "set"
        assert result["status"] == booth_status(enabled)
        device.set_booth_mode.assert_called_once_with(enabled)
        device.get_booth_mode.assert_not_called()


@pytest.mark.parametrize("enabled", [0, 1, 0.0, 1.0, "true", "false", "yes", "0", "", [], {}])
def test_fastmcp_rejects_coercible_nonboolean_arguments(mcp, tool, device, enabled):
    with pytest.raises(ToolError, match="valid boolean"):
        asyncio.run(mcp.call_tool("stackchan_booth_mode", {"enabled": enabled}))
    with pytest.raises(ValueError, match="JSON boolean"):
        tool.fn(enabled)
    assert device.mock_calls == []


@pytest.mark.parametrize("arguments", [{"enabled": "null"}, {"toggle": True}, {"enable": True}])
def test_fastmcp_normalized_null_or_unknown_keys_can_only_query(mcp, device, arguments):
    # Pinned FastMCP parses "null" before Pydantic validation and ignores extra keys.
    asyncio.run(mcp.call_tool("stackchan_booth_mode", arguments))
    device.get_booth_mode.assert_called_once_with()
    device.set_booth_mode.assert_not_called()


def test_unknown_keys_do_not_change_explicit_boolean_write(mcp, device):
    asyncio.run(mcp.call_tool("stackchan_booth_mode", {"enabled": False, "toggle": True}))
    device.set_booth_mode.assert_called_once_with(False)
    device.get_booth_mode.assert_not_called()


def test_direct_python_call_still_rejects_null_string(tool, device):
    with pytest.raises(ValueError, match="JSON boolean"):
        tool.fn("null")
    assert device.mock_calls == []


def test_tool_schema_and_operating_caveats(tool):
    assert "enabled" not in tool.parameters.get("required", [])
    assert tool.parameters["properties"]["enabled"]["anyOf"] == [{"type": "boolean"}, {"type": "null"}]
    for text in (
        "persists across reboot", "ALL device microphone capture", "pending local recording",
        "speaker still works", "phone supplies input", "Already-downloaded audio cannot be recalled",
        "camera is", "visual feedback only", "Never toggles implicitly or retries writes",
    ):
        assert text in tool.description


@pytest.mark.parametrize("status", [None, False, True, [], "on", {}, {"success": True}])
def test_tool_reports_unknown_for_malformed_status(tool, device, status):
    device.get_booth_mode.return_value = status
    result = json.loads(asyncio.run(tool.run({})))
    assert result["success"] is False
    assert result["outcome"] == "unknown"
    assert result["status"] == status
    device.get_booth_mode.assert_called_once_with()
    device.set_booth_mode.assert_not_called()


@pytest.mark.parametrize("field", STATUS_FIELDS)
@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, [], {}])
def test_tool_requires_actual_booleans_in_all_status_fields(tool, device, field, value):
    status = booth_status(**{field: value})
    device.get_booth_mode.return_value = status
    result = json.loads(asyncio.run(tool.run({})))
    assert result["success"] is False
    assert result["status"] == status


@pytest.mark.parametrize("field", STATUS_FIELDS)
def test_tool_rejects_missing_status_fields(tool, device, field):
    status = booth_status()
    del status[field]
    device.get_booth_mode.return_value = status
    assert json.loads(asyncio.run(tool.run({})))["success"] is False


@pytest.mark.parametrize("enabled,status,message", [
    (True, booth_status(False), "does not match"),
    (False, booth_status(True), "does not match"),
    (True, booth_status(persisted=False), "Persistence"),
    (True, booth_status(capture_allowed=True), "not confirmed blocked"),
    (True, booth_status(mic_running=True), "not confirmed blocked"),
    (False, booth_status(False, capture_allowed=False), "not confirmed allowed"),
    (True, booth_status(success=False, error="NVS failed"), "successful boolean"),
])
def test_tool_exposes_partial_results_without_compensating_writes(tool, device, enabled, status, message):
    device.set_booth_mode.side_effect = None
    device.set_booth_mode.return_value = status
    result = json.loads(asyncio.run(tool.run({"enabled": enabled})))
    assert result["success"] is False
    assert result["outcome"] == "partial"
    assert result["status"] == status
    assert message in result["error"]
    assert "no write was retried" in result["note"]
    device.set_booth_mode.assert_called_once_with(enabled)
    device.get_booth_mode.assert_not_called()


def test_disabled_mode_does_not_require_mic_running_during_playback(tool, device):
    device.get_booth_mode.return_value = booth_status(False, mic_running=False)
    assert json.loads(asyncio.run(tool.run({})))["success"] is True


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("error", [requests.Timeout("timed out"), requests.ConnectionError("offline"), ValueError("invalid JSON")])
def test_tool_failure_keeps_completion_unknown_and_never_retries(tool, device, enabled, error):
    operation = device.get_booth_mode if enabled is None else device.set_booth_mode
    operation.side_effect = error
    result = json.loads(asyncio.run(tool.run({"enabled": enabled})))
    assert result["success"] is False
    assert result["outcome"] == "unknown"
    assert str(error) in result["error"]
    assert len(device.mock_calls) == 1
    if enabled is not None:
        assert "State may have changed" in result["note"]
        assert "no write was retried" in result["note"]


@pytest.mark.parametrize("status", [
    {"error": "firmware unsupported"},
    booth_status(success=False, persisted=False, error="flash write failed"),
    booth_status(),
])
def test_tool_preserves_http_error_body_even_when_it_claims_success(tool, device, status):
    device.set_booth_mode.side_effect = requests.HTTPError("Service unavailable", response=response(status, 503))
    result = json.loads(asyncio.run(tool.run({"enabled": True})))
    assert result["success"] is False
    assert "HTTP 503" in result["error"]
    assert result["status"] == status
    assert len(device.mock_calls) == 1


def test_tool_preserves_non_json_http_error(tool, device):
    error_response = response(None, 502)
    error_response._content = b"upstream unavailable"
    device.get_booth_mode.side_effect = requests.HTTPError(response=error_response)
    result = json.loads(asyncio.run(tool.run({})))
    assert result["success"] is False
    assert result["outcome"] == "unknown"
    assert "HTTP 502" in result["error"]
    assert "upstream unavailable" in result["error"]
