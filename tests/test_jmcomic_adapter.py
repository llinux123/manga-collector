"""jmcomic 适配层测试（P1）—— 全部离线，不碰网络。

覆盖三块：

1. **落点红线**：共享区 / 权威库一律拒绝（退出码 6）；
2. **目录规则校验**：拒绝「分隔符与写法混用」的字面量目录坑；
3. **错误映射**：上游异常 → 稳定退出码（4=不存在 / 3=需登录 / 2=其它）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from manga.adapters import jmcomic as jm
from manga.envelope import EXIT_AUTH, EXIT_ERROR, EXIT_NOTFOUND, EXIT_REFUSED, MangaError
from manga.cli import app

runner = CliRunner()


def _invoke(*args: str):
    """跑一个 CLI 命令，返回 (退出码, 信封)。"""
    result = runner.invoke(app, list(args))
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


# ── 目录规则 ──────────────────────────────────────────────────────────────


class TestDirRule:
    @pytest.mark.parametrize(
        "rule",
        ["Bd/Atitle/Pname", "Bd_Atitle_Pname", "Bd / {Atitle} / {Pname}"],
    )
    def test_accepts_correct_forms(self, rule: str) -> None:
        assert jm.validate_dir_rule(rule) == ["Bd", "Atitle", "Pname"] or len(jm.validate_dir_rule(rule)) == 3

    def test_rejects_literal_prefix(self) -> None:
        """``Bd_Atitle/Pname`` 会生成字面量目录 ``Bd_Atitle`` → 静默污染库。"""
        with pytest.raises(MangaError) as excinfo:
            jm.validate_dir_rule("Bd_Atitle/Pname")
        assert excinfo.value.code == "DIR_RULE_LITERAL"
        assert excinfo.value.exit_code == EXIT_ERROR
        assert "Bd/Atitle/Pname" in (excinfo.value.hint or "")

    def test_rejects_wrong_level_count(self) -> None:
        with pytest.raises(MangaError) as excinfo:
            jm.validate_dir_rule("Bd/Pname")
        assert excinfo.value.code == "DIR_RULE_LEVELS"

    def test_shipped_option_file_is_valid(self) -> None:
        import yaml

        option_file = Path(__file__).resolve().parents[1] / "config" / "jmcomic-option.yml"
        data = yaml.safe_load(option_file.read_text(encoding="utf-8-sig"))
        assert jm.validate_dir_rule(data["dir_rule"]["rule"]) == ["Bd", "Atitle", "Pname"]


# ── 落点红线 ──────────────────────────────────────────────────────────────


class TestRedLine:
    def test_export_dir_is_forbidden(self, library_root: Path) -> None:
        export = library_root / "Export" / "Manga"
        with pytest.raises(MangaError) as excinfo:
            jm.ensure_safe_dest(export, export_dir=export, library_dir=library_root / "Manga" / "Library")
        assert excinfo.value.exit_code == EXIT_REFUSED
        assert excinfo.value.code == "RED_LINE"

    def test_nested_under_export_dir_is_forbidden(self, library_root: Path) -> None:
        export = library_root / "Export" / "Manga"
        with pytest.raises(MangaError):
            jm.ensure_safe_dest(export / "sub" / "deeper", export_dir=export)

    def test_library_dir_is_forbidden(self, library_root: Path) -> None:
        library = library_root / "Manga" / "Library"
        with pytest.raises(MangaError) as excinfo:
            jm.ensure_safe_dest(library, export_dir=library_root / "Export" / "Manga", library_dir=library)
        assert excinfo.value.exit_code == EXIT_REFUSED
        assert "Library" in excinfo.value.message

    def test_inbox_is_allowed(self, library_root: Path) -> None:
        raw = library_root / "00-Incoming" / "_raw_jmcomic"
        assert jm.ensure_safe_dest(raw, export_dir=library_root / "Export" / "Manga") == raw

    def test_is_inside(self, workdir: Path) -> None:
        # 用 conftest 的 workdir（普通 mkdir）而不是 tmp_path：
        # pytest 的 tmp_path 目录带 0o700 权限，沙箱内的受限进程读不了。
        assert jm.is_inside(workdir / "a" / "b", workdir)
        assert not jm.is_inside(workdir.parent / "elsewhere", workdir)


# ── 异常映射 ──────────────────────────────────────────────────────────────


class TestErrorMapping:
    def test_missing_album_maps_to_not_found(self) -> None:
        class MissingAlbumPhotoException(Exception):
            error_jmid = "123456"

        err = jm._map_exception(MissingAlbumPhotoException("没了"), what="下载")
        assert err.exit_code == EXIT_NOTFOUND
        assert err.code == "NOT_FOUND"

    def test_retry_exhausted_maps_to_error_with_hint(self) -> None:
        class RequestRetryAllFailException(Exception):
            pass

        err = jm._map_exception(RequestRetryAllFailException("重试耗尽"), what="下载")
        assert err.exit_code == EXIT_ERROR
        assert "pick-domain" in (err.hint or "")

    def test_login_error_maps_to_auth(self) -> None:
        err = jm._map_exception(Exception("Login required"), what="下载")
        assert err.exit_code == EXIT_AUTH
        assert err.code == "AUTH_REQUIRED"

    def test_unknown_error_maps_to_error(self) -> None:
        err = jm._map_exception(ValueError("炸了"), what="下载")
        assert err.exit_code == EXIT_ERROR
        assert err.code == "VALUEERROR"


# ── 导出依赖（P4 加固） ───────────────────────────────────────────────────


class TestExportDependencies:
    def test_missing_dependency_gives_install_command(self, monkeypatch, library_root: Path) -> None:
        monkeypatch.setattr(jm, "missing_modules", lambda mods: ["img2pdf"])
        with pytest.raises(MangaError) as excinfo:
            jm.export(object(), "123456", fmt="pdf", dest=library_root / "00-Incoming" / "_raw_jmcomic")
        assert excinfo.value.code == "DEP_MISSING"
        # 缺依赖 = 「东西不存在」→ 退出码 4（与缺外部工具的 TOOL_MISSING 一致）
        assert excinfo.value.exit_code == EXIT_NOTFOUND
        assert "img2pdf" in (excinfo.value.hint or "")
        assert "uv pip install" in (excinfo.value.hint or "")

    def test_zip_export_needs_no_extra_dependency(self) -> None:
        """未加密 zip 走标准库 zipfile → 不该被依赖检查拦下。"""
        assert jm.EXPORT_DEPENDENCIES["zip"] == ()
        assert jm.EXPORT_DEPENDENCIES["pdf"] == ("img2pdf",)

    def test_import_error_maps_to_dep_missing_with_hint(self) -> None:
        err = jm._map_exception(ImportError("No module named 'img2pdf'", name="img2pdf"), what="导出")
        assert err.code == "DEP_MISSING"
        assert err.exit_code == EXIT_NOTFOUND
        assert "uv pip install img2pdf" in (err.hint or "")


# ── 干跑降级 ──────────────────────────────────────────────────────────────


class TestDryRunDegrade:
    def test_metadata_failure_degrades_to_minimal_plan(self, monkeypatch, library_root: Path) -> None:
        class FakeClient:
            def get_album_detail(self, album_id: str):
                raise RuntimeError("站点抽风")

        monkeypatch.setattr(jm, "make_client", lambda *a, **k: FakeClient())
        result = jm.download(
            object(),
            ["123456"],
            dest=library_root / "00-Incoming" / "_raw_jmcomic",
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["degraded"] == 1
        plan = result["plan"][0]
        assert plan["degraded"] is True
        assert plan["id"] == "123456"
        assert plan["episodes"] is None
        assert "站点抽风" in plan["error"]


# ── P1：逐 ID 驱动必须合并上游 BatchResult.failed ─────────────────────────


class TestPerIdFailureMerge:
    """作业模式的逐 ID 分支：上游把失败放进 BatchResult.failed（check_exception=False），
    此前完全不读 → 失败 ID 被记成 done（假成功）。"""

    class _FakeBatchResult:
        def __init__(self, failed: dict | None = None):
            self.failed = failed or {}

        def __iter__(self):
            return iter(())

    def test_failed_id_reports_failed_progress(self, monkeypatch, library_root: Path) -> None:
        fake = lambda: type("M", (), {"download_album": staticmethod(
            lambda ids, option, **kw: self._FakeBatchResult(failed={"bad-id": RuntimeError("目标不存在")})
        )})
        monkeypatch.setattr(jm, "_jmcomic", fake)

        events: list[tuple] = []
        data = jm.download(
            type("O", (), {"auth": {}})(),
            ["bad-id"],
            dest=library_root / "00-Incoming" / "_raw_jmcomic",
            login=False,
            on_progress=lambda event, item, error: events.append((event, item, error)),
        )
        assert data["failed"], "上游 failed 必须并入摘要"
        assert "bad-id" in data["failed"]
        assert any(e[0] == "failed" and e[1] == "bad-id" for e in events), events
        assert not any(e[0] == "done" for e in events), "失败 ID 绝不能发 done"

    def test_mixed_success_and_failure(self, monkeypatch, library_root: Path) -> None:
        ok_entity = type("A", (), {"album_id": "ok-id", "name": "Good"})()
        ok_dler = type("D", (), {"manifest_dict": {0: type("M", (), {"image_filepath_list": []})()}})()

        def fake_download_album(ids, option, **kw):
            target = ids[0]
            if target == "bad-id":
                return self.TestPerIdFailureMerge._FakeBatchResult(failed={target: RuntimeError("挂了")})
            return [(ok_entity, ok_dler)]

        fake = lambda: type("M", (), {"download_album": staticmethod(fake_download_album)})
        monkeypatch.setattr(jm, "_jmcomic", fake)

        events: list[tuple] = []
        data = jm.download(
            type("O", (), {"auth": {}})(),
            ["ok-id", "bad-id"],
            dest=library_root / "00-Incoming" / "_raw_jmcomic",
            login=False,
            on_progress=lambda event, item, error: events.append((event, item, error)),
        )
        assert data["requested"] == 2
        assert [e[1] for e in events if e[0] == "done"] == ["ok-id"]
        assert [e[1] for e in events if e[0] == "failed"] == ["bad-id"]

    def test_all_failed_returns_nonzero_envelope(self, monkeypatch, library_root: Path) -> None:
        """整批失败 → CLI 必须是非零信封（假成功防护）。

        补丁要打在**下载器**上而不是整个 jmcomic 模块：option 构造仍然要用真实的
        ``create_option_by_file``（否则只会得到 OPTION_INVALID，测不到下载分支）。
        """
        import jmcomic

        def fake_download_album(ids, option, **kw):
            return self.TestPerIdFailureMerge._FakeBatchResult(failed={ids[0]: RuntimeError("挂了")})

        fake = type(
            "M",
            (),
            {
                "create_option_by_file": staticmethod(jmcomic.create_option_by_file),
                "download_album": staticmethod(fake_download_album),
            },
        )
        monkeypatch.setattr(jm, "_jmcomic", lambda: fake)
        code, env = _invoke(
            "download", "bad-id",
            "--library-root", str(library_root),
            "--dest", str(library_root / "00-Incoming" / "_raw_jmcomic"),
            "--no-login",
            "--apply",
            "--option", str(Path(__file__).resolve().parents[1] / "config" / "jmcomic-option.yml"),
        )
        assert code == 2
        assert env["ok"] is False
        assert env["error"]["code"] in ("PARTIAL_DOWNLOAD", "NO_ARTIFACT")


# ── 上游形态陷阱（P1 回归） ───────────────────────────────────────────────


class TestUpstreamShape:
    def test_episode_list_is_a_tuple_not_an_object(self) -> None:
        """jmcomic 的 episode_list 是元组 —— 按对象取会静默拿到 None。"""
        entry = jm._episode_entry(("404005", "1", "幸福督市", "2024-01-01"))
        assert entry["id"] == "404005"
        assert entry["index"] == "1"
        assert entry["name"] == "幸福督市"

    def test_episode_entry_accepts_objects_too(self) -> None:
        class FakePhoto:
            photo_id = "390442"
            album_index = 2
            name = "幸福督市_2"
            page_arr = ["a", "b", "c"]

        entry = jm._episode_entry(FakePhoto())
        assert entry["id"] == "390442"
        assert entry["index"] == 2
        assert entry["images"] == 3

    def test_manifest_lives_in_manifest_dict(self) -> None:
        """清单在 manifest_dict 里，没有 .manifest 属性 —— 取错就是「成功但 0 张图」。"""

        class FakeManifest:
            def __init__(self, files):
                self.image_filepath_list = list(files)

        class FakeDownloader:
            def __init__(self):
                self.manifest_dict = {"a": FakeManifest(["1.jpg", "2.jpg"]), "b": FakeManifest(["3.jpg"])}

        dler = FakeDownloader()
        assert jm._manifest_images(dler) == ["1.jpg", "2.jpg", "3.jpg"]
        assert len(jm._manifest_objects(dler)) == 2

    def test_manifest_falls_back_to_legacy_attribute(self) -> None:
        class LegacyManifest:
            image_filepath_list = ["x.jpg"]

        class LegacyDownloader:
            manifest = LegacyManifest()

        assert jm._manifest_images(LegacyDownloader()) == ["x.jpg"]


# ── CLI 契约（离线） ───────────────────────────────────────────────────────


class TestCliRedLine:
    def test_download_into_export_dir_is_refused(self, library_root: Path) -> None:
        export = library_root / "Export" / "Manga"
        code, env = _invoke(
            "download", "123456", "--library-root", str(library_root), "--dest", str(export)
        )
        assert code == EXIT_REFUSED
        assert env["ok"] is False
        assert env["error"]["code"] == "RED_LINE"
        assert env["data"]["dest"] == str(export)

    def test_download_into_library_dir_is_refused(self, library_root: Path) -> None:
        library = library_root / "Manga" / "Library"
        code, env = _invoke(
            "download", "123456", "--library-root", str(library_root), "--dest", str(library)
        )
        assert code == EXIT_REFUSED
        assert env["error"]["code"] == "RED_LINE"

    def test_export_into_export_dir_is_refused(self, library_root: Path) -> None:
        export = library_root / "Export" / "Manga"
        code, env = _invoke(
            "export", "123456", "--library-root", str(library_root), "--dest", str(export)
        )
        assert code == EXIT_REFUSED
        assert env["error"]["code"] == "RED_LINE"


class TestCliUsage:
    def test_unknown_search_mode_is_usage_error(self, library_root: Path) -> None:
        code, env = _invoke("search", "x", "--mode", "bogus", "--library-root", str(library_root))
        assert code == EXIT_ERROR
        assert env["error"]["code"] == "BAD_MODE"

    def test_unknown_export_format_is_usage_error(self, library_root: Path) -> None:
        code, env = _invoke("export", "123456", "--format", "bogus", "--library-root", str(library_root))
        assert code == EXIT_ERROR
        assert env["error"]["code"] == "BAD_FORMAT"

    def test_domains_command_lists_api_pool_offline(self) -> None:
        code, env = _invoke("domains", "--no-html")
        assert code == 0
        assert env["ok"] is True
        assert env["data"]["api_count"] > 0
        assert env["data"]["default_impl"] == "api"
        assert env["data"]["html_count"] == 0


# ── P0：option 插件输出红线闸门 ────────────────────────────────────────────


class TestOptionPluginRedLine:
    """变体 option 里的输出插件不得把灰色产物写进共享区/权威库。

    build_option 只覆盖 dir_rule.base_dir 是不够的 —— zip/img2pdf/long_img
    等插件有独立的输出目录参数，必须在**调用下载器之前**逐一校验。
    """

    def _write_option(self, workdir: Path, plugins_yaml: str) -> Path:
        import yaml

        base = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "config" / "jmcomic-option.yml").read_text(
                encoding="utf-8-sig"
            )
        )
        base["plugins"] = yaml.safe_load(plugins_yaml)
        # 测试环境没装 img2pdf 等可选依赖：failed-fast 会在构造阶段就拦下，
        # 掩盖我们要测的红线闸门 → 变体统一用 ignore-only-log。
        base["plugins"]["dependencies_strategy"] = "ignore-only-log"
        option_file = workdir / "variant.yml"
        option_file.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
        return option_file

    def _dirs(self, library_root: Path) -> dict:
        return {
            "export_dir": library_root / "Export" / "Manga",
            "library_dir": library_root / "Manga" / "Library",
        }

    def test_zip_plugin_into_export_is_refused_before_download(
        self, workdir: Path, library_root: Path
    ) -> None:
        export = library_root / "Export" / "Manga"
        option_file = self._write_option(
            workdir,
            f"""
after_album:
  - plugin: zip
    kwargs:
      zip_dir: "{export.as_posix()}"
""",
        )
        with pytest.raises(MangaError) as excinfo:
            jm.build_option(option_file, dest=workdir / "raw", **self._dirs(library_root))
        assert excinfo.value.exit_code == EXIT_REFUSED
        assert excinfo.value.code == "RED_LINE"
        assert "zip" in excinfo.value.message

    def test_nested_dir_rule_into_library_is_refused(self, workdir: Path, library_root: Path) -> None:
        library = library_root / "Manga" / "Library"
        option_file = self._write_option(
            workdir,
            f"""
after_album:
  - plugin: download_cover
    kwargs:
      dir_rule:
        base_dir: "{library.as_posix()}"
        rule: Bd_Aid
""",
        )
        with pytest.raises(MangaError) as excinfo:
            jm.build_option(option_file, dest=workdir / "raw", **self._dirs(library_root))
        assert excinfo.value.exit_code == EXIT_REFUSED

    def test_relative_path_resolved_against_cwd(self, workdir: Path, library_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """相对路径按进程 CWD 解析后仍要过闸门（不能靠相对写法绕过）。"""
        export = library_root / "Export" / "Manga"
        monkeypatch.chdir(library_root)
        option_file = self._write_option(
            workdir,
            """
after_photo:
  - plugin: img2pdf
    kwargs:
      pdf_dir: "Export/Manga"
""",
        )
        with pytest.raises(MangaError) as excinfo:
            jm.build_option(option_file, dest=workdir / "raw", **self._dirs(library_root))
        assert excinfo.value.exit_code == EXIT_REFUSED
        assert str(export) in json.dumps(excinfo.value.data, ensure_ascii=False) or "Export" in excinfo.value.message

    def test_unknown_output_plugin_is_refused(self, workdir: Path, library_root: Path) -> None:
        """不在白名单里的插件（可能写文件/改路径）一律拒绝。"""
        option_file = self._write_option(
            workdir,
            """
after_init:
  - plugin: replace_path_string
    kwargs:
      replace: {"00-Incoming": "Export"}
""",
        )
        with pytest.raises(MangaError) as excinfo:
            jm.build_option(option_file, dest=workdir / "raw", **self._dirs(library_root))
        assert excinfo.value.exit_code == EXIT_REFUSED
        assert "replace_path_string" in excinfo.value.message

    def test_whitelisted_retry_plugin_passes(self, workdir: Path, library_root: Path) -> None:
        option_file = self._write_option(
            workdir,
            """
after_init:
  - plugin: advanced_retry
    kwargs:
      retry_config:
        retry_rounds: 3
""",
        )
        option = jm.build_option(option_file, dest=workdir / "raw", **self._dirs(library_root))
        assert option.dir_rule.base_dir == str(workdir / "raw")

    def test_safe_zip_dir_passes(self, workdir: Path, library_root: Path) -> None:
        raw = workdir / "raw"
        option_file = self._write_option(
            workdir,
            f"""
after_album:
  - plugin: zip
    kwargs:
      zip_dir: "{raw.as_posix()}"
""",
        )
        option = jm.build_option(option_file, dest=raw, **self._dirs(library_root))
        assert option is not None

    def test_cli_download_with_evil_option_exits_6(self, workdir: Path, library_root: Path) -> None:
        export = library_root / "Export" / "Manga"
        option_file = self._write_option(
            workdir,
            f"""
after_album:
  - plugin: zip
    kwargs:
      zip_dir: "{export.as_posix()}"
""",
        )
        code, env = _invoke(
            "download", "123456",
            "--library-root", str(library_root),
            "--dest", str(workdir / "raw"),
            "--option", str(option_file),
        )
        assert code == EXIT_REFUSED
        assert env["error"]["code"] == "RED_LINE"
