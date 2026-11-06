"""CLI 端到端测试：跑真实的 PowerShell 脚本，验证信封契约与红线。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

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

        assert sorted(p.name for p in inbox.rglob("*") if p.is_file()) == before_inbox
        assert sorted(p.name for p in (library_root / "Manga" / "Library").rglob("*") if p.is_file()) == before_library

    def test_export_dir_untouched_by_ingest(self, library_root: Path, scripts_dir: Path, inbox: Path) -> None:
        export = library_root / "Export" / "Manga"
        before = sorted(p.name for p in export.rglob("*") if p.is_file())
        _invoke("ingest", "--library-root", str(library_root), "--scripts-dir", str(scripts_dir))
        assert sorted(p.name for p in export.rglob("*") if p.is_file()) == before


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
