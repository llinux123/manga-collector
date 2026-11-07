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
