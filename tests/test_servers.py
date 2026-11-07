"""Komga / Kavita 扫描助手的单元测试。

**绝不联网**：所有 HTTP 都用 monkeypatch 替换掉 ``urllib.request.urlopen``；
也不需要真的装了 Komga / Kavita。
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from manga import servers
from manga.envelope import MangaError

BASE = "http://komga.test:25600"


class FakeResponse:
    """最小可用的 http.client 风格响应。"""

    def __init__(self, body: str = "", status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def info(self) -> dict[str, str]:
        return self.headers

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def install_routes(monkeypatch, routes: list[tuple[str, str, object]]) -> list[dict]:
    """装上假的 urlopen。

    ``routes`` 每项是 ``(方法, URL 后缀, 结果)``；结果是 FakeResponse
    （状态码 >= 400 时按真实 urllib 的行为抛 HTTPError）或一个异常实例。
    """
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None, **kwargs):  # noqa: ANN001 - 测试替身
        method = request.get_method()
        url = request.full_url
        calls.append(
            {
                "method": method,
                "url": url,
                "timeout": timeout,
                "headers": {key.lower(): value for key, value in request.header_items()},
            }
        )
        for route_method, suffix, outcome in routes:
            if method == route_method and url.endswith(suffix):
                if isinstance(outcome, Exception):
                    raise outcome
                assert isinstance(outcome, FakeResponse)
                if outcome.status >= 400:
                    raise urllib.error.HTTPError(url, outcome.status, "boom", None, None)
                return outcome
        raise urllib.error.URLError(f"没有预置的路由：{method} {url}")

    monkeypatch.setattr(servers.urllib.request, "urlopen", fake_urlopen)
    return calls


LIBRARIES = [
    {"id": "0e3f0000-0000-0000-0000-000000000001", "name": "漫画", "root": "/data/manga", "available": True},
    {"id": "0e3f0000-0000-0000-0000-000000000002", "name": "合集", "root": "/data/other", "available": False},
]


class TestCheckServer:
    def test_reachable_komga(self, monkeypatch) -> None:
        install_routes(
            monkeypatch,
            [
                ("GET", "/api/v1/libraries", FakeResponse(json.dumps(LIBRARIES))),
                ("GET", "/", FakeResponse("<html><title>Komga</title></html>")),
            ],
        )
        result = servers.check_server(BASE)
        assert result["reachable"] is True
        assert result["kind"] == "komga"
        assert result["auth_ok"] is True
        assert result["libraries"][0]["name"] == "漫画"
        assert result["reason"]

    def test_reachable_kavita(self, monkeypatch) -> None:
        install_routes(
            monkeypatch,
            [
                ("GET", "/api/v1/libraries", FakeResponse("", 404)),
                ("GET", "/", FakeResponse("<html><title>Kavita</title></html>")),
            ],
        )
        result = servers.check_server(BASE)
        assert result["reachable"] is True
        assert result["kind"] == "kavita"

    def test_unauthorized_does_not_raise(self, monkeypatch) -> None:
        install_routes(
            monkeypatch,
            [
                ("GET", "/api/v1/libraries", FakeResponse("", 401)),
                ("GET", "/", FakeResponse("", 401)),
            ],
        )
        result = servers.check_server(BASE)
        # 服务器可达，只是没带对凭据
        assert result["reachable"] is True
        assert result["kind"] == "komga"
        assert result["auth_ok"] is False
        assert "401" in result["reason"] or "认证" in result["reason"]
        assert "API Key" in result["detail"] or "权限" in result["detail"]

    def test_connection_failure_does_not_raise(self, monkeypatch) -> None:
        install_routes(
            monkeypatch,
            [
                ("GET", "/api/v1/libraries", urllib.error.URLError("Connection refused")),
                ("GET", "/", urllib.error.URLError("Connection refused")),
            ],
        )
        result = servers.check_server(BASE)
        assert result["reachable"] is False
        assert result["kind"] == "unknown"
        assert "Connection refused" in result["reason"]
        assert result["detail"]

    def test_timeout_does_not_raise(self, monkeypatch) -> None:
        install_routes(
            monkeypatch,
            [("GET", "/api/v1/libraries", TimeoutError("timed out")), ("GET", "/", TimeoutError("timed out"))],
        )
        result = servers.check_server(BASE, timeout=0.01)
        assert result["reachable"] is False
        assert result["reason"]

    def test_auth_headers_are_sent(self, monkeypatch) -> None:
        calls = install_routes(
            monkeypatch,
            [
                ("GET", "/api/v1/libraries", FakeResponse(json.dumps(LIBRARIES))),
                ("GET", "/", FakeResponse("Komga")),
            ],
        )
        servers.check_server(BASE, auth=("admin", "pw"), api_key="abc123")
        headers = calls[0]["headers"]
        assert headers["authorization"].startswith("Basic ")
        assert headers["x-api-key"] == "abc123"

    def test_invalid_url_is_a_usage_error(self) -> None:
        with pytest.raises(MangaError) as excinfo:
            servers.check_server("not-a-url")
        assert excinfo.value.code == "USAGE"
        assert excinfo.value.exit_code == 1


class TestKomgaLibraries:
    def test_lists_id_name_and_root(self, monkeypatch) -> None:
        install_routes(monkeypatch, [("GET", "/api/v1/libraries", FakeResponse(json.dumps(LIBRARIES)))])
        result = servers.komga_libraries(BASE)
        assert result["count"] == 2
        first = result["libraries"][0]
        assert first["id"] == LIBRARIES[0]["id"]
        assert first["name"] == "漫画"
        assert first["root"] == "/data/manga"

    def test_unauthorized_raises_readable_error(self, monkeypatch) -> None:
        install_routes(monkeypatch, [("GET", "/api/v1/libraries", FakeResponse("", 403))])
        with pytest.raises(MangaError) as excinfo:
            servers.komga_libraries(BASE)
        assert excinfo.value.code == "AUTH_FAILED"
        assert excinfo.value.exit_code == 2
        assert "ADMIN" in (excinfo.value.hint or "")

    def test_unreachable_raises_manga_error_not_raw_exception(self, monkeypatch) -> None:
        install_routes(monkeypatch, [("GET", "/api/v1/libraries", urllib.error.URLError("Connection refused"))])
        with pytest.raises(MangaError) as excinfo:
            servers.komga_libraries(BASE)
        assert excinfo.value.code == "SERVER_UNREACHABLE"
        assert excinfo.value.exit_code == 2


class TestTriggerKomgaScan:
    def test_posts_to_scan_endpoint(self, monkeypatch) -> None:
        calls = install_routes(monkeypatch, [("POST", "/api/v1/libraries/lib-1/scan", FakeResponse("", 202))])
        result = servers.trigger_komga_scan(BASE, "lib-1")
        assert result["ok"] is True
        assert result["status"] == 202
        assert calls[0]["method"] == "POST"
        assert calls[0]["url"].endswith("/api/v1/libraries/lib-1/scan")

    def test_unauthorized_gives_permission_hint(self, monkeypatch) -> None:
        install_routes(monkeypatch, [("POST", "/api/v1/libraries/lib-1/scan", FakeResponse("", 401))])
        with pytest.raises(MangaError) as excinfo:
            servers.trigger_komga_scan(BASE, "lib-1")
        assert excinfo.value.code == "AUTH_FAILED"
        hint = excinfo.value.hint or ""
        assert "ADMIN" in hint or "权限" in hint
        assert "API" in hint

    def test_unknown_library_is_not_found(self, monkeypatch) -> None:
        install_routes(monkeypatch, [("POST", "/api/v1/libraries/nope/scan", FakeResponse("", 404))])
        with pytest.raises(MangaError) as excinfo:
            servers.trigger_komga_scan(BASE, "nope")
        assert excinfo.value.code == "LIBRARY_NOT_FOUND"
        assert excinfo.value.exit_code == 4

    def test_empty_library_id_is_a_usage_error(self) -> None:
        with pytest.raises(MangaError) as excinfo:
            servers.trigger_komga_scan(BASE, "  ")
        assert excinfo.value.code == "USAGE"


class TestKavitaHint:
    def test_returns_guidance_without_any_io(self, monkeypatch) -> None:
        def explode(*args, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("kavita_scan_hint 不该发任何 HTTP 请求")

        monkeypatch.setattr(servers.urllib.request, "urlopen", explode)
        result = servers.kavita_scan_hint("http://kavita.test:5000", library_path="/data/manga")
        assert result["ok"] is True
        assert result["kind"] == "kavita"
        assert result["automated"] is False
        assert result["action"] == "manual"
        assert result["instructions"]
        assert result["why_not_automated"]
        assert result["server"] == "http://kavita.test:5000"
        assert result["library_path"] == "/data/manga"

    def test_explains_why_not_automated(self) -> None:
        result = servers.kavita_scan_hint()
        blob = " ".join(result["why_not_automated"])
        assert "API" in blob
        assert "Folder Watching" in blob or "10 分钟" in blob

    def test_module_cannot_do_destructive_http(self) -> None:
        assert servers.ALLOWED_HTTP_METHODS == ("GET", "POST")
        with pytest.raises(MangaError) as excinfo:
            servers._request("DELETE", BASE + "/api/v1/libraries")
        assert excinfo.value.code == "METHOD_NOT_ALLOWED"

    def test_module_has_no_metadata_write_api(self) -> None:
        public = [name for name in dir(servers) if not name.startswith("_")]
        banned = ("set_metadata", "update_metadata", "mount", "unmount", "delete", "purge", "refresh_metadata")
        for name in public:
            assert not any(word in name.lower() for word in banned), name
