"""红线守卫测试：灰色来源的产物不得进入 ``Export\\Manga``（Syncthing 共享区）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from manga.adapters.pipeline import (
    ExportGuard,
    RedLineViolation,
    assert_not_grey_source_export,
    diff_snapshots,
    snapshot_dir,
)


class TestSnapshot:
    def test_missing_dir_is_empty(self, workdir: Path) -> None:
        assert snapshot_dir(workdir / "nope") == {}

    def test_snapshot_lists_files_recursively(self, workdir: Path) -> None:
        (workdir / "Series").mkdir()
        (workdir / "Series" / "001.cbz").write_bytes(b"x")
        snap = snapshot_dir(workdir)
        assert set(snap) == {str(Path("Series") / "001.cbz")}

    def test_diff_detects_added_removed_changed(self, workdir: Path) -> None:
        (workdir / "a.cbz").write_bytes(b"a")
        before = snapshot_dir(workdir)
        (workdir / "b.cbz").write_bytes(b"b")
        (workdir / "a.cbz").write_bytes(b"changed")
        after = snapshot_dir(workdir)
        diff = diff_snapshots(before, after)
        assert diff["added"] == ["b.cbz"]
        assert diff["changed"] == ["a.cbz"]
        assert diff["removed"] == []


class TestExportGuard:
    def test_no_change_passes(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        (export / "keep.cbz").write_bytes(b"keep")
        with ExportGuard(export):
            pass  # 什么都没做

    def test_added_file_raises_red_line(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        with pytest.raises(RedLineViolation) as exc:
            with ExportGuard(export):
                (export / "leak.cbz").write_bytes(b"leak")
        assert exc.value.code == "RED_LINE"
        assert exc.value.exit_code == 6
        assert "leak.cbz" in exc.value.data["added_files"]

    def test_modified_file_is_reported_not_raised(self, workdir: Path) -> None:
        """共享区里已有文件被改动：提示，不算红线违规（红线管的是「有没有新文件进来」）。"""
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        target = export / "x.cbz"
        target.write_bytes(b"old-content")
        with ExportGuard(export):
            target.write_bytes(b"new-content-longer")  # 不抛异常
        assert diff_snapshots(snapshot_dir(export), snapshot_dir(export))["added"] == []

    def test_removed_file_raises(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        target = export / "gone.cbz"
        target.write_bytes(b"x")
        with pytest.raises(RedLineViolation):
            with ExportGuard(export):
                target.unlink()

    def test_original_exception_is_not_masked(self, workdir: Path) -> None:
        export = workdir / "Export" / "Manga"
        export.mkdir(parents=True)
        with pytest.raises(ValueError, match="原始错误"):
            with ExportGuard(export):
                raise ValueError("原始错误")


class TestForbiddenExport:
    def test_export_script_is_refused_for_grey_source(self) -> None:
        with pytest.raises(RedLineViolation):
            assert_not_grey_source_export("Export-MangaLibrary.ps1")

    def test_other_scripts_are_allowed(self) -> None:
        assert_not_grey_source_export("Test-MangaLibrary.ps1")
