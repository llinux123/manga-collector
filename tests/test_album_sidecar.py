"""下载阶段写车号 sidecar 的单元测试（不联网）。"""

from __future__ import annotations

import json
from pathlib import Path

from manga.adapters import jmcomic as jm


class _Manifest:
    def __init__(self, paths: list[str]) -> None:
        self.image_filepath_list = paths


class _Dler:
    def __init__(self, paths: list[str]) -> None:
        self.manifest_dict = {0: _Manifest(paths)}


class _Album:
    def __init__(self, album_id: str, name: str) -> None:
        self.album_id = album_id
        self.name = name


def test_write_album_sidecars(workdir: Path) -> None:
    dest = workdir / "raw"
    album_dir = dest / "某本漫画"
    chapter = album_dir / "第1话"
    chapter.mkdir(parents=True)
    img = chapter / "001.jpg"
    img.write_bytes(b"x")

    items = [(_Album("404005", "某本漫画"), _Dler([str(img)]))]
    written = jm.write_album_sidecars(items, dest=dest)

    sidecar = album_dir / jm.ALBUM_SIDECAR_NAME
    assert sidecar.is_file(), written
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["album_id"] == "404005"
    assert data["source"] == "jmcomic"


def test_write_album_sidecars_skips_one_level_layout(workdir: Path) -> None:
    """单层布局（<dest>/<章节>/<图>）认不出本子目录 → 不写 sidecar，也不报错。"""
    dest = workdir / "raw"
    chapter = dest / "第1话"
    chapter.mkdir(parents=True)
    img = chapter / "001.jpg"
    img.write_bytes(b"x")

    items = [(_Album("1", "x"), _Dler([str(img)]))]
    assert jm.write_album_sidecars(items, dest=dest) == []
