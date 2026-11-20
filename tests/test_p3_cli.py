"""P3 新命令的 **CLI 接线**测试。

队友的测试覆盖了模块内部（``test_external_adapters.py`` / ``test_actions_gen.py`` /
``test_servers.py``）；这一份专门盯**接线**：命令名、参数名、退出码、红线，
以及「预检模式绝不能真的下载」。

全部离线：外部工具用 monkeypatch 替掉。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from manga.cli import app
from manga.envelope import EXIT_ERROR, EXIT_NOTFOUND, EXIT_OK, EXIT_REFUSED, EXIT_USAGE

runner = CliRunner()


def _invoke(*args: str):
    result = runner.invoke(app, list(args))
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


class TestGalleryDlCli:
    def test_unsupported_url_exits_4_and_never_downloads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不支持的 URL → 明确返回「不支持」，**且一次下载命令都不发**。"""
        from manga.adapters import gallery_dl

        calls: list[str] = []

        def fake_precheck(url: str, **kwargs):
            calls.append(url)
            return {"url": url, "supported": False, "status": "unsupported", "reason": "no extractor"}

        def fail_download(*args, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("预检模式不该下载")

        monkeypatch.setattr(gallery_dl, "precheck", fake_precheck)
        monkeypatch.setattr(gallery_dl, "download", fail_download)

        code, payload = _invoke("gallery-dl", "https://example.com/x")
        assert code == EXIT_NOTFOUND
        assert payload["error"]["code"] == "UNSUPPORTED_URL"
        assert payload["data"]["unsupported_count"] == 1
        assert calls == ["https://example.com/x"]

    def test_supported_url_is_dry_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from manga.adapters import gallery_dl

        monkeypatch.setattr(
            gallery_dl,
            "precheck",
            lambda url, **kwargs: {"url": url, "supported": True, "status": "supported"},
        )
        monkeypatch.setattr(
            gallery_dl,
            "download",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该下载")),
        )
        code, payload = _invoke("gallery-dl", "https://example.com/x")
        assert code == EXIT_OK
        assert payload["data"]["dry_run"] is True
        assert any("dry-run" in w for w in payload["warnings"])

    def test_apply_calls_download(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        from manga.adapters import gallery_dl

        seen: dict = {}

        def fake_download(urls, dest, **kwargs):
            seen["urls"] = list(urls)
            seen["dest"] = dest
            return {"dest": str(dest), "count": 1, "files": ["a.cbz"], "new_count": 1}

        monkeypatch.setattr(gallery_dl, "download", fake_download)
        code, payload = _invoke(
            "gallery-dl",
            "https://example.com/x",
            "--apply",
            "--dest",
            str(workdir / "inbox"),
        )
        assert code == EXIT_OK
        assert seen["urls"] == ["https://example.com/x"]
        assert payload["data"]["count"] == 1


class TestMangadexDlCli:
    def test_tool_missing_exits_4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from manga.adapters import mangadex_dl
        from manga.envelope import MangaError

        def missing(**kwargs):
            raise MangaError("找不到 mangadex-dl", code="TOOL_MISSING", exit_code=EXIT_NOTFOUND)

        monkeypatch.setattr(mangadex_dl, "find_executable", missing)
        code, payload = _invoke("mangadex-dl", "https://mangadex.org/title/x")
        assert code == EXIT_NOTFOUND
        assert payload["error"]["code"] == "TOOL_MISSING"

    def test_dry_run_reports_plan_without_downloading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from manga.adapters import mangadex_dl

        monkeypatch.setattr(mangadex_dl, "find_executable", lambda **kwargs: Path("mangadex-dl"))
        monkeypatch.setattr(
            mangadex_dl,
            "download",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该下载")),
        )
        code, payload = _invoke("mangadex-dl", "https://mangadex.org/title/x", "--language", "zh")
        assert code == EXIT_OK
        assert payload["data"]["dry_run"] is True
        assert "--language zh" in payload["data"]["command"]

    def test_adapter_warnings_reach_envelope_top_level(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """适配层的 warnings 必须冒到信封顶层 —— agent 只读信封的 warnings 字段。"""
        from manga.adapters import mangadex_dl

        monkeypatch.setattr(mangadex_dl, "find_executable", lambda **kwargs: Path("mangadex-dl"))
        monkeypatch.setattr(
            mangadex_dl,
            "download",
            lambda *a, **k: {
                "dest": str(workdir),
                "count": 1,
                "files": ["a.cbz"],
                "comicinfo": {"with": 0, "without": 1},
                "warnings": ["1 个 CBZ 里没有 ComicInfo.xml"],
            },
        )
        code, payload = _invoke(
            "mangadex-dl",
            "https://mangadex.org/title/x",
            "--apply",
            "--dest",
            str(workdir / "inbox"),
        )
        assert code == EXIT_OK
        assert any("ComicInfo" in w for w in payload["warnings"])
        assert "warnings" not in payload["data"]


class TestActionsGenCli:
    def test_dry_run_reports_but_writes_nothing(self, workdir: Path) -> None:
        repo = workdir / "repo"
        repo.mkdir()
        code, payload = _invoke("actions-gen", "--dest", str(repo))
        assert code == EXIT_OK
        assert payload["data"]["dry_run"] is True
        assert len(payload["data"]["planned"]) == 2
        assert not (repo / ".github" / "workflows").exists()

    def test_apply_writes_workflows(self, workdir: Path) -> None:
        repo = workdir / "repo"
        repo.mkdir()
        code, payload = _invoke("actions-gen", "--dest", str(repo), "--apply")
        assert code == EXIT_OK
        assert payload["data"]["dry_run"] is False
        written = {Path(p).name for p in payload["data"]["written"]}
        assert written == {"download_dispatch.yml", "export_favorites.yml"}
        assert (repo / ".github" / "workflows" / "download_dispatch.yml").is_file()

    def test_secrets_contain_no_credentials(self, workdir: Path) -> None:
        code, payload = _invoke("actions-gen", "--dest", str(workdir))
        assert code == EXIT_OK
        secrets = payload["data"]["secrets"]
        assert len(secrets) >= 10
        for item in secrets:
            # 只有「名字 + 说明 + 是否必需 + 用在哪」—— 没有任何取值字段
            assert set(item) == {"name", "description", "required", "used_by"}
            assert isinstance(item["name"], str)
            assert isinstance(item["description"], str)
            assert isinstance(item["required"], bool)


class TestServerCli:
    def test_check_does_not_raise_on_network_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from manga import servers

        monkeypatch.setattr(
            servers,
            "check_server",
            lambda url, **kwargs: {"reachable": False, "kind": "unknown", "reason": "连不上"},
        )
        code, payload = _invoke("server", "check", "--base-url", "http://127.0.0.1:1")
        assert code == EXIT_OK
        assert payload["data"]["reachable"] is False
        assert payload["warnings"]

    def test_kavita_hint_is_manual_only(self) -> None:
        code, payload = _invoke("server", "kavita-hint")
        assert code == EXIT_OK
        assert payload["data"]["automated"] is False

    def test_missing_base_url_is_usage_error(self) -> None:
        code, payload = _invoke("server", "check")
        assert code == EXIT_USAGE
        assert payload["error"]["code"] == "NO_BASE_URL"


class TestJobCli:
    def test_unknown_command_is_rejected_immediately(self) -> None:
        """不存在的命令要在 start 就被拦下，别静默变成一个失败作业。"""
        code, payload = _invoke("job", "start", "no-such-command")
        assert code == EXIT_USAGE
        assert payload["error"]["code"] == "UNKNOWN_COMMAND"
        assert "download" in payload["error"]["hint"]

    def test_start_then_poll_then_result(self, workdir: Path) -> None:
        library = workdir / "lib"
        library.mkdir()
        code, payload = _invoke("job", "start", "version", "--library-root", str(library))
        assert code == EXIT_OK
        job_id = payload["data"]["job_id"]
        assert payload["data"]["state"] in ("running", "succeeded")

        # 等子进程写终态
        import time

        for _ in range(100):
            _, polled = _invoke("job", "poll", job_id, "--library-root", str(library))
            if polled["data"]["finished"]:
                break
            time.sleep(0.1)
        assert polled["data"]["state"] == "succeeded"

        code, result = _invoke("job", "result", job_id, "--library-root", str(library))
        assert code == EXIT_OK
        assert result["data"]["ok"] is True
        assert "version" in result["data"]["data"]

    def test_poll_of_unknown_job_exits_4(self, workdir: Path) -> None:
        code, payload = _invoke("job", "poll", "no-such-job", "--library-root", str(workdir))
        assert code == EXIT_NOTFOUND
        assert payload["error"]["code"] == "JOB_NOT_FOUND"


class TestRedLineAcrossCli:
    def test_favorites_to_shared_area_exits_6(self, workdir: Path) -> None:
        library = workdir / "lib"
        (library / "Export" / "Manga").mkdir(parents=True)
        code, payload = _invoke(
            "favorites",
            "--library-root",
            str(library),
            "--dest",
            str(library / "Export" / "Manga" / "fav"),
        )
        assert code == EXIT_REFUSED
        assert payload["error"]["code"] == "RED_LINE"

    def test_gallery_dl_to_shared_area_exits_6(self, workdir: Path) -> None:
        """落点是共享区 → 红线拒绝（退出码 6），**且不执行任何外部命令**。

        这里**不能**把 ``download`` 替换掉 —— 红线检查就在它里面，
        替换掉等于没测。真实函数会先过红线、再去找可执行文件。
        """
        from manga.adapters import gallery_dl

        library = workdir / "lib"
        (library / "Export" / "Manga").mkdir(parents=True)

        def fail_spawn(*args, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError("红线就该拦住，不该真去跑 gallery-dl")

        original_run = gallery_dl._run
        gallery_dl._run = fail_spawn
        try:
            code, payload = _invoke(
                "gallery-dl",
                "https://example.com/x",
                "--apply",
                "--dest",
                str(library / "Export" / "Manga"),
                "--library-root",
                str(library),
            )
        finally:
            gallery_dl._run = original_run
        assert code == EXIT_REFUSED
        assert payload["error"]["code"] == "RED_LINE"
