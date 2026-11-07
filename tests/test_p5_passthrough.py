"""P5 新能力的测试（全部离线）。

覆盖四块：

1. **透传红线扫描**：`passthrough.scan_passthrough_args` 拦下指向共享区/权威库的参数；
2. **--option 变体配置**：CLI 命令接受 `--option <file>`；
3. **搜索分类/排行榜**：`search --category/--ranking` 与关键词互斥、字符串名映射到常量；
4. **优雅取消**：作业子进程看到取消标记 → 先调 ``DownloadControl.cancel()`` → 兜底 ``os._exit``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from manga import passthrough
from manga.cli import app
from manga.envelope import EXIT_REFUSED, EXIT_USAGE, MangaError, RedLineViolation

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
OPTION_FILE = REPO_ROOT / "config" / "jmcomic-option.yml"


def _invoke(*args: str):
    result = runner.invoke(app, list(args))
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


class TestPassthroughRedLine:
    def test_key_value_dest_to_export_is_refused(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        violations = passthrough.scan_passthrough_args(
            ["--dest", str(export)],
            export_dir=export,
            library_dir=None,
        )
        assert violations

    def test_gallery_style_base_directory_is_refused(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        violations = passthrough.scan_passthrough_args(
            ["-o", f"base-directory={export}"],
            export_dir=export,
            library_dir=None,
        )
        assert violations

    def test_bare_absolute_path_to_library_is_refused(self, workdir: Path) -> None:
        library = workdir / "Manga" / "Library"
        library.mkdir(parents=True)
        violations = passthrough.scan_passthrough_args(
            [str(library / "x.cbz")],
            export_dir=None,
            library_dir=library,
        )
        assert violations

    def test_safe_args_pass(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        library = workdir / "Manga" / "Library"
        violations = passthrough.scan_passthrough_args(
            ["--delay", "1.5", "-o", "metadata.format=json", "https://example.com/x"],
            export_dir=export,
            library_dir=library,
        )
        assert violations == []

    def test_assert_raises_red_line(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        with pytest.raises(RedLineViolation) as excinfo:
            passthrough.assert_passthrough_safe(
                ["--path", str(export)],
                export_dir=export,
                library_dir=None,
            )
        assert excinfo.value.exit_code == EXIT_REFUSED


class TestOptionFlag:
    def test_download_accepts_option_file(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """--option 参数要被接受，且传给 build_option 的就是这份文件。"""
        from manga.adapters import jmcomic as jm

        seen: dict = {}

        def fake_build_option(option_file, **kwargs):
            seen["option_file"] = Path(option_file)
            return object()

        monkeypatch.setattr(jm, "build_option", fake_build_option)
        monkeypatch.setattr(
            jm,
            "download",
            lambda *a, **k: {"dry_run": True, "plan": [], "degraded": 0},
        )

        code, payload = _invoke(
            "download",
            "404005",
            "--option",
            str(OPTION_FILE),
            "--library-root",
            str(workdir / "lib"),
        )
        assert code == 0
        assert seen["option_file"] == OPTION_FILE

    def test_download_with_bad_option_file_reports_error(self, workdir: Path) -> None:
        code, payload = _invoke(
            "download",
            "404005",
            "--option",
            str(workdir / "no-such-file.yml"),
            "--library-root",
            str(workdir / "lib"),
        )
        assert payload["error"]["code"] == "OPTION_MISSING"


class TestSearchModes:
    def test_ranking_without_query_works(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """--ranking 不需要关键词。"""
        from manga.adapters import jmcomic as jm

        class _FakePage:
            total = 2
            page_count = 1

            def iter_id_title(self):
                yield "123", "周榜第一"
                yield "456", "周榜第二"

        class _FakeClient:
            def week_ranking(self, page):  # noqa: ARG002 - self 是绑定方法的第一个参数
                return _FakePage()

        monkeypatch.setattr(jm, "make_client", lambda *a, **k: _FakeClient())

        code, payload = _invoke("search", "--ranking", "week", "--library-root", str(workdir / "lib"))
        assert code == 0
        assert payload["data"]["ranking"] == "week"
        assert payload["data"]["count"] == 2

    def test_category_with_time_and_order(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        from manga.adapters import jmcomic as jm

        captured: dict = {}

        class _Page:
            total = 1
            page_count = 1

            def iter_id_title(self):
                yield "789", "韩漫"

        class _FakeClient:
            def categories_filter(self, **kwargs):
                captured.update(kwargs)
                return _Page()

        monkeypatch.setattr(jm, "make_client", lambda *a, **k: _FakeClient())

        code, payload = _invoke(
            "search",
            "--category",
            "hanman",
            "--time",
            "week",
            "--order-by",
            "view",
            "--library-root",
            str(workdir / "lib"),
        )
        assert code == 0
        assert captured["category"] is not None
        assert captured["time"] is not None

    def test_ranking_and_query_are_mutually_exclusive(self, workdir: Path) -> None:
        code, payload = _invoke("search", "关键词", "--ranking", "week", "--library-root", str(workdir / "lib"))
        assert code == EXIT_USAGE
        assert payload["error"]["code"] == "BAD_USAGE"

    def test_no_query_no_ranking_no_category_is_usage_error(self, workdir: Path) -> None:
        code, payload = _invoke("search", "--library-root", str(workdir / "lib"))
        assert code == EXIT_USAGE
        assert payload["error"]["code"] == "BAD_USAGE"


class TestGracefulCancel:
    def test_cancel_exception_maps_to_exit_5(self) -> None:
        """DownloadCancelledException → 退出码 5，data 里带已完成/未完成清单。"""
        from manga.adapters import jmcomic as jm

        exc = type(
            "DownloadCancelledException",
            (Exception,),
            {"reason": "用户取消", "completed_results": ["a"], "unfinished_ids": ["b", "c"]},
        )("用户取消")

        err = jm._map_exception(exc, what="下载")
        assert err.code == "CANCELLED"
        assert err.exit_code == 5
        assert err.data["completed"] == ["a"]
        assert err.data["unfinished"] == ["b", "c"]

    def test_watchdog_calls_control_cancel_before_exit(self, workdir: Path) -> None:
        """看门狗看到取消标记 → 先调 control.cancel()，再等 grace 后强退。"""
        from manga import jobs

        job_dir = workdir / "job"
        job_dir.mkdir(parents=True, exist_ok=True)

        cancelled: list[str] = []

        class _Control:
            def cancel(self, reason: str) -> None:
                cancelled.append(reason)

        import subprocess
        import sys

        script = (
            "import sys;"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r});"
            "from manga import jobs;"
            "from pathlib import Path;"
            "control = type('C', (), {'cancel': lambda self, r: print('CANCELLED:' + r, flush=True)})();"
            f"jobs.start_cancel_watchdog(Path({str(job_dir)!r}), on_cancel=lambda: control.cancel('用户取消'), grace=0.1);"
            "import time; time.sleep(60)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            jobs.request_cancel(job_dir)
            code = proc.wait(timeout=20)
            assert code == jobs.EXIT_CANCELLED
        finally:
            if proc.poll() is None:  # pragma: no cover
                proc.kill()
                proc.wait(timeout=10)
