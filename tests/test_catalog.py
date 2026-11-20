"""车号索引 / 「是否在库」判定测试（离线）。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from conftest import make_cbz
from manga import catalog
from manga.cli import app

runner = CliRunner()


def _invoke(*args: str):
    result = runner.invoke(app, list(args))
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


def _lib(workdir: Path) -> Path:
    lib = workdir / "Library" / "Manga" / "Library"
    make_cbz(lib / "Alpha Work" / "001 - A.cbz", series="Alpha Work", notes="jm:404005")
    make_cbz(lib / "Beta Work" / "001 - B.cbz", series="Beta Work", notes="jm:999")
    make_cbz(lib / "Gamma Work" / "001 - C.cbz", series="Gamma Work")
    return lib


class TestExtractAlbumId:
    def test_notes_has_priority(self) -> None:
        xml = "<ComicInfo><Notes>jm:404005</Notes><Web>https://18comic.vip/album/1/</Web></ComicInfo>"
        assert catalog.extract_album_id(xml) == "404005"

    def test_web_is_fallback(self) -> None:
        xml = "<ComicInfo><Notes></Notes><Web>https://18comic.vip/album/777/</Web></ComicInfo>"
        assert catalog.extract_album_id(xml) == "777"

    def test_none_when_absent(self) -> None:
        assert catalog.extract_album_id("<ComicInfo><Notes></Notes></ComicInfo>") is None


class TestScanAndLookup:
    def test_scan_builds_id_index(self, workdir: Path) -> None:
        lib = _lib(workdir)
        index = catalog.scan_library(lib)
        assert index["by_id"]["404005"]["series"] == "Alpha Work"
        assert index["by_id"]["999"]["series"] == "Beta Work"
        assert len(index["series"]) == 3

    def test_lookup_by_album_id_hits(self, workdir: Path) -> None:
        data = catalog.lookup(_lib(workdir), album_id="404005")
        assert data["in_library"] is True
        assert data["by"] == "album_id"
        assert data["match"]["series"] == "Alpha Work"

    def test_lookup_by_album_id_miss(self, workdir: Path) -> None:
        data = catalog.lookup(_lib(workdir), album_id="123456")
        assert data["in_library"] is False
        assert data["by"] is None

    def test_lookup_title_fallback(self, workdir: Path) -> None:
        # Gamma Work 没有车号，只能靠标题兜底命中
        data = catalog.lookup(_lib(workdir), title="gamma work")
        assert data["in_library"] is True
        assert data["by"] == "title"
        assert data["match"]["series"] == "Gamma Work"

    def test_album_id_beats_title(self, workdir: Path) -> None:
        data = catalog.lookup(_lib(workdir), album_id="404005", title="完全不相干")
        assert data["in_library"] is True
        assert data["by"] == "album_id"


class TestSourcePrefixDoesNotBreakTitleLookup:
    """库是「来源名/书名」两层结构 —— 标题兜底不能把来源名算进书名。

    之前 ``title_norm`` 取的是**相对库根的整条路径**
    （``禁漫 (JMComic)\\My Work`` → ``禁漫jmcomicmywork``），
    于是 ``manga lookup --title "My Work"`` 永远匹配不上 —— 明明在库里。
    """

    def _nested_lib(self, workdir: Path) -> Path:
        lib = workdir / "Library" / "Manga" / "Library"
        make_cbz(
            lib / "禁漫 (JMComic)" / "My Work" / "001 - A.cbz",
            series="My Work",
            notes="jm:404005",
        )
        return lib

    def test_title_norm_uses_leaf_directory_name(self, workdir: Path) -> None:
        index = catalog.scan_library(self._nested_lib(workdir))
        row = index["series"][0]
        assert row["title_norm"] == catalog.normalize_title("My Work")

    def test_lookup_by_title_finds_nested_series(self, workdir: Path) -> None:
        data = catalog.lookup(self._nested_lib(workdir), title="My Work")
        assert data["in_library"] is True
        assert data["by"] == "title"

    def test_series_field_keeps_the_full_relative_path(self, workdir: Path) -> None:
        """``series`` 仍给完整相对路径（人能看懂书在哪个来源下）。"""
        index = catalog.scan_library(self._nested_lib(workdir))
        assert index["series"][0]["series"] == "禁漫 (JMComic)\\My Work"


class TestLookupCli:
    def test_cli_lookup_by_id(self, workdir: Path) -> None:
        lib = _lib(workdir)
        code, env = _invoke("lookup", "404005", "--library-root", str(lib.parents[1]))
        assert code == 0
        assert env["ok"] is True
        assert env["data"]["in_library"] is True

    def test_cli_requires_a_query(self, workdir: Path) -> None:
        lib = _lib(workdir)
        code, env = _invoke("lookup", "--library-root", str(lib.parents[1]))
        assert code == 1
        assert env["error"]["code"] == "MISSING_QUERY"
