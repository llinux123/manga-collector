"""CLI 端到端测试：跑真实的 PowerShell 脚本，验证信封契约与红线。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import make_cbz
from manga.cli import app

runner = CliRunner()


def _invoke(*args: str):
    """跑一个 CLI 命令，返回 (退出码, 解析后的信封)。"""
    result = runner.invoke(app, list(args))
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


class TestEnvelopeContract:
    def test_every_command_emits_one_json_object(self) -> None:
        code, env = _invoke("paths")
        assert code == 0
        assert set(env) == {"ok", "data", "error", "warnings", "exit"}
        assert env["ok"] is True
        assert env["exit"] == 0

    def test_paths_include_the_red_line_directory(self, library_root: Path) -> None:
        code, env = _invoke("paths", "--library-root", str(library_root))
        assert code == 0
        paths = env["data"]["paths"]
        assert paths["export"].endswith("Export\\Manga")
        assert paths["library"].endswith("Manga\\Library")


class TestUsageErrorsAreEnvelopes:
    """参数写错也必须给 JSON 信封（退出码 1）—— agent 只认一种形状。

    之前：Typer/Click 的用法错误直接打印 Rich 面板到 stderr、退出码 2、
    stdout 一个字都没有。agent 拿到空输出 + 退出码 2，既不知道是「用法错」
    还是「执行错」，也没法从 stdout 解析错误码。
    """

    @pytest.mark.parametrize(
        "args",
        [
            ("download",),                    # 缺必填参数
            ("search", "--mode", "bogus"),    # 未知子命令参数值
            ("nosuchcommand",),               # 未知子命令
            ("paths", "--no-such-flag"),       # 未知选项
            ("export", "123", "--format"),     # 选项缺值
        ],
    )
    def test_usage_error_is_json_envelope_with_exit_1(self, args: tuple[str, ...]) -> None:
        result = runner.invoke(app, list(args))
        assert result.exit_code == 1, f"{args} → 退出码应为 1（用法错），实际 {result.exit_code}"
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["exit"] == 1
        assert payload["error"]["code"] == "BAD_USAGE"
        assert payload["error"]["message"]

    def test_stdout_contains_only_the_envelope(self) -> None:
        """stdout 里不许混入 Typer 的帮助文本 / Rich 面板。"""
        result = runner.invoke(app, ["download"])
        payload = json.loads(result.stdout)  # 能整段解析就说明 stdout 只有信封
        assert payload["error"]["code"] == "BAD_USAGE"

    def test_help_still_works(self) -> None:
        """--help 是给人看的，不该被包成错误信封。"""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.stdout or "用法" in result.stdout


class TestVerify:
    def test_clean_library_passes(self, library_root: Path, scripts_dir: Path) -> None:
        code, env = _invoke(
            "verify", "--library-root", str(library_root), "--scripts-dir", str(scripts_dir)
        )
        assert code == 0
        assert env["ok"] is True
        report = env["data"]["report"]
        assert report["naming_compliance"]["percent"] == 100.0
        assert report["comicinfo_coverage"]["percent"] == 100.0

    def test_broken_library_exits_with_error_code(self, library_root: Path, scripts_dir: Path) -> None:
        # 塞一个损坏的 CBZ
        bad = library_root / "Manga" / "Library" / "Sample Series" / "003 - Broken.cbz"
        bad.write_bytes(b"not a zip")
        code, env = _invoke("verify", "--library-root", str(library_root), "--scripts-dir", str(scripts_dir))
        assert code == 2
        assert env["ok"] is False
        assert env["error"]["code"] == "SCRIPT_ERROR"


class TestIngest:
    def test_dry_run_does_not_touch_anything(self, library_root: Path, scripts_dir: Path, inbox: Path) -> None:
        before_inbox = sorted(p.name for p in inbox.rglob("*") if p.is_file())
        before_library = sorted(p.name for p in (library_root / "Manga" / "Library").rglob("*") if p.is_file())

        code, env = _invoke(
            "ingest", "--library-root", str(library_root), "--scripts-dir", str(scripts_dir)
        )
        assert code == 0
        assert env["ok"] is True
        assert env["data"]["dry_run"] is True
        assert any("dry-run" in w for w in env["warnings"])
        report = env["data"]["report"]
        assert report["parsed"] is True
        assert report["scanned"] == 1  # inbox 夹具放了 1 个 CBZ
        assert report["planned_moves"] == 1
        assert "moved" not in report  # dry-run 没有 apply-only 字段

        assert sorted(p.name for p in inbox.rglob("*") if p.is_file()) == before_inbox
        assert sorted(p.name for p in (library_root / "Manga" / "Library").rglob("*") if p.is_file()) == before_library

    def test_export_dir_untouched_by_ingest(self, library_root: Path, scripts_dir: Path, inbox: Path) -> None:
        export = library_root / "Export" / "Manga"
        before = sorted(p.name for p in export.rglob("*") if p.is_file())
        _invoke("ingest", "--library-root", str(library_root), "--scripts-dir", str(scripts_dir))
        assert sorted(p.name for p in export.rglob("*") if p.is_file()) == before


class TestIngestAlbumId:
    """入库时把车号写进 ComicInfo，并按车号判重。"""

    def _read_comicinfo(self, cbz: Path) -> str:
        import zipfile

        with zipfile.ZipFile(cbz) as z:
            return z.read("ComicInfo.xml").decode("utf-8", "replace")

    def test_album_id_written_to_comicinfo(self, library_root: Path, scripts_dir: Path, inbox: Path) -> None:
        import zipfile

        series = inbox / "Album Series"
        series.mkdir(parents=True, exist_ok=True)
        # 真实链路：Convert 打出的 CBZ 里没有 ComicInfo，入库时才生成
        with zipfile.ZipFile(series / "001 - X.cbz", "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("001.jpg", b"x")
        (series / ".jm-album.json").write_text(
            json.dumps({"album_id": "404005", "source": "jmcomic"}, ensure_ascii=False), encoding="utf-8"
        )

        code, env = _invoke(
            "ingest",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--generate-comicinfo",
            "--apply",
        )
        assert code == 0, env
        target = library_root / "Manga" / "Library" / "Album Series" / "001 - X.cbz"
        assert target.is_file()
        ci = self._read_comicinfo(target)
        assert "jm:404005" in ci
        assert "https://18comic.vip/album/404005/" in ci
        report = env["data"]["report"]
        assert report["scanned"] == 2  # inbox 夹具 1 个 + 本用例造的 1 个
        assert report["moved"] == 2
        assert report["comicinfo_written"] == 1  # 夹具包已有 ComicInfo，只有新包需要生成
        assert report["errors"] == 0

    def test_duplicate_album_id_is_skipped(self, library_root: Path, scripts_dir: Path) -> None:
        # 库里已有一个车号 404005 的系列
        make_cbz(
            library_root / "Manga" / "Library" / "Existing Work" / "001 - A.cbz",
            series="Existing Work",
            notes="jm:404005",
        )
        # 收件箱里换了个书名、但同一车号 → 应判重跳过
        another = library_root / "00-Incoming" / "Manga" / "Another Work"
        make_cbz(another / "001 - B.cbz", series="Another Work")
        (another / ".jm-album.json").write_text(
            json.dumps({"album_id": "404005"}, ensure_ascii=False), encoding="utf-8"
        )

        code, env = _invoke(
            "ingest",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--generate-comicinfo",
            "--apply",
        )
        assert code == 0, env
        assert not (library_root / "Manga" / "Library" / "Another Work").exists()
        assert any("退出码 1" in w or "冲突" in w for w in env["warnings"])
        assert env["data"]["report"]["conflicts"] == 1


class TestConvertCbzAlbumId:
    def test_sidecar_copied_to_inbox(self, library_root: Path, scripts_dir: Path) -> None:
        raw = library_root / "00-Incoming" / "_raw_jmcomic"
        album = raw / "Some Album"
        chapter = album / "第1话"
        chapter.mkdir(parents=True)
        (chapter / "001.jpg").write_bytes(b"x")
        (chapter / "002.jpg").write_bytes(b"y")
        (album / ".jm-album.json").write_text(
            json.dumps({"album_id": "777", "source": "jmcomic"}, ensure_ascii=False), encoding="utf-8"
        )

        code, env = _invoke(
            "convert-cbz",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--source-root",
            str(raw),
            "--target-root",
            str(library_root / "00-Incoming" / "Manga"),
            "--apply",
        )
        assert code == 0, env
        target_series = library_root / "00-Incoming" / "Manga" / "禁漫 (JMComic)" / "Some Album"
        assert (target_series / ".jm-album.json").is_file()
        assert list(target_series.glob("*.cbz"))

        # 信封自带结构化报告：agent 不必再列目录核对产物数量
        report = env["data"]["report"]
        assert report["parsed"] is True
        assert report["planned_or_succeeded"] == 1
        assert report["skipped"] == 0
        assert report["errors"] == 0
        assert report["packed"] == [
            {"series": "Some Album", "file": "001.cbz", "images": 2, "source": "第1话"}
        ]
        assert isinstance(env["data"]["elapsed_s"], (int, float))

    def test_empty_source_reports_nothing_to_do(self, library_root: Path, scripts_dir: Path) -> None:
        raw = library_root / "00-Incoming" / "_raw_jmcomic"
        raw.mkdir(parents=True, exist_ok=True)
        code, env = _invoke(
            "convert-cbz",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--source-root",
            str(raw),
            "--target-root",
            str(library_root / "00-Incoming" / "Manga"),
            "--apply",
        )
        assert code == 0, env
        assert env["data"]["report"]["nothing_to_do"] is True
        assert any("nothing_to_do" in w for w in env["warnings"])


class TestGuard:
    def test_guard_reports_inventory(self, library_root: Path) -> None:
        code, env = _invoke("guard", "--library-root", str(library_root))
        assert code == 0
        assert env["data"]["file_count"] == 0

    def test_guard_detects_new_files_in_export(self, library_root: Path) -> None:
        export = library_root / "Export" / "Manga"
        (export / "leak.cbz").write_bytes(b"x")
        snapshot = library_root / "snap.json"
        code, _ = _invoke("guard", "--library-root", str(library_root), "--snapshot", str(snapshot))
        assert code == 0
        assert snapshot.is_file()

        (export / "leak2.cbz").write_bytes(b"y")
        code, env = _invoke("guard", "--library-root", str(library_root), "--compare", str(snapshot))
        assert code == 6
        assert env["error"]["code"] == "RED_LINE"


class TestVerifyScope:
    """增量校验：存量库有历史问题时，用 --series 单独验新入库的那本。"""

    def test_series_option_scopes_the_check(self, library_root: Path, scripts_dir: Path) -> None:
        code, env = _invoke(
            "verify",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--series",
            "Sample Series",
        )
        assert code == 0
        assert env["ok"] is True
        report = env["data"]["report"]
        assert report["scanned"]["count"] == 2
        assert report["naming_compliance"]["percent"] == 100.0
        assert env["data"]["scoped_series"] == ["Sample Series"]

    def test_original_library_is_untouched(self, library_root: Path, scripts_dir: Path) -> None:
        before = sorted(p.name for p in (library_root / "Manga" / "Library").rglob("*"))
        _invoke(
            "verify",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--series",
            "Sample Series",
        )
        after = sorted(p.name for p in (library_root / "Manga" / "Library").rglob("*"))
        assert after == before

    def test_staging_directory_is_cleaned_up(self, library_root: Path, scripts_dir: Path) -> None:
        metadata = library_root / "Manga" / "Metadata"
        _invoke(
            "verify",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--series",
            "Sample Series",
        )
        leftovers = [p.name for p in metadata.glob(".verify-scope-*")] if metadata.is_dir() else []
        assert leftovers == [], f"临时影子库根没删干净：{leftovers}"

    def test_series_available_is_reported(self, library_root: Path, scripts_dir: Path) -> None:
        code, env = _invoke("verify", "--library-root", str(library_root), "--scripts-dir", str(scripts_dir))
        assert code == 0
        assert "Sample Series" in env["data"]["series_available"]

    def test_missing_series_gives_actionable_error(self, library_root: Path, scripts_dir: Path) -> None:
        code, env = _invoke(
            "verify",
            "--library-root",
            str(library_root),
            "--scripts-dir",
            str(scripts_dir),
            "--series",
            "根本没有这个系列",
        )
        assert code == 4
        assert env["error"]["code"] == "SERIES_MISSING"


class TestDoctor:
    def test_doctor_reports_version_auth_and_dependencies(self, library_root: Path) -> None:
        code, env = _invoke("doctor", "--library-root", str(library_root))
        assert code == 0
        data = env["data"]
        assert "jmcomic_version" in data
        assert data["min_jmcomic_version"] == "2.7.11"
        assert "logged_in" in data["auth"]
        assert "missing_modules" in data["dependencies"]
        assert "missing_executables" in data["dependencies"]

    def test_doctor_checks_include_auth_and_dependency_checks(self, library_root: Path) -> None:
        code, env = _invoke("doctor", "--library-root", str(library_root))
        checks = {c["check"] for c in env["data"]["checks"]}
        assert "auth" in checks
        assert "jmcomic" in checks


class TestMissingScript:
    def test_missing_script_gives_actionable_error(self, library_root: Path, workdir: Path) -> None:
        code, env = _invoke(
            "verify", "--library-root", str(library_root), "--scripts-dir", str(workdir / "empty")
        )
        assert code == 4
        assert env["error"]["code"] == "SCRIPT_MISSING"
        assert "scripts" in env["error"]["hint"]


@pytest.mark.parametrize("command", ["paths", "doctor", "version"])
def test_never_crashes(command: str) -> None:
    code, env = _invoke(command)
    assert code == 0
    assert env["ok"] is True
