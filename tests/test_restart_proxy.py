"""Tests for the post-deploy restart hook."""

from unittest.mock import MagicMock, patch

from amazing_marvin_mcp.restart_proxy import restart_proxy


def test_skips_silently_when_token_missing(monkeypatch):
    monkeypatch.setenv("MCP_PROXY_APP_UUID", "u")
    monkeypatch.delenv("COOLIFY_API_TOKEN", raising=False)
    assert restart_proxy() == 0


def test_skips_silently_when_uuid_missing(monkeypatch):
    monkeypatch.setenv("COOLIFY_API_TOKEN", "t")
    monkeypatch.delenv("MCP_PROXY_APP_UUID", raising=False)
    assert restart_proxy() == 0


def _fake_response(status=200, body=b"{\"ok\":true}"):
    resp = MagicMock()
    resp.status = status
    resp.reason = "OK"
    resp.read.return_value = body
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    return resp


def test_calls_correct_url_with_default_base(monkeypatch):
    monkeypatch.setenv("COOLIFY_API_TOKEN", "secret-token")
    monkeypatch.setenv("MCP_PROXY_APP_UUID", "proxy-uuid")
    monkeypatch.delenv("COOLIFY_BASE_URL", raising=False)

    with patch("amazing_marvin_mcp.restart_proxy.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _fake_response()
        assert restart_proxy() == 0

    req = mock_open.call_args[0][0]
    assert req.full_url == (
        "http://host.docker.internal:8000/api/v1/applications/proxy-uuid/restart"
    )
    assert req.get_method() == "GET"
    assert req.headers["Authorization"] == "Bearer secret-token"


def test_strips_trailing_slash_on_base(monkeypatch):
    monkeypatch.setenv("COOLIFY_API_TOKEN", "t")
    monkeypatch.setenv("MCP_PROXY_APP_UUID", "u")
    monkeypatch.setenv("COOLIFY_BASE_URL", "https://coolify.example.com/")

    with patch("amazing_marvin_mcp.restart_proxy.urllib.request.urlopen") as mock_open:
        mock_open.return_value = _fake_response()
        restart_proxy()

    req = mock_open.call_args[0][0]
    assert req.full_url == "https://coolify.example.com/api/v1/applications/u/restart"


def test_returns_nonzero_on_http_error(monkeypatch):
    import urllib.error

    monkeypatch.setenv("COOLIFY_API_TOKEN", "t")
    monkeypatch.setenv("MCP_PROXY_APP_UUID", "u")

    def boom(*_a, **_kw):
        raise urllib.error.HTTPError(
            "http://x", 401, "Unauthorized", {}, MagicMock(read=lambda: b"nope")
        )

    with patch("amazing_marvin_mcp.restart_proxy.urllib.request.urlopen", side_effect=boom):
        assert restart_proxy() == 1


def test_returns_nonzero_on_generic_error(monkeypatch):
    monkeypatch.setenv("COOLIFY_API_TOKEN", "t")
    monkeypatch.setenv("MCP_PROXY_APP_UUID", "u")

    with patch(
        "amazing_marvin_mcp.restart_proxy.urllib.request.urlopen",
        side_effect=RuntimeError("conn refused"),
    ):
        assert restart_proxy() == 1
