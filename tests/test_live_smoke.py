"""**真机冒烟**（P4 验收）：1 本 2 话 → CBZ → 入库 → 校验退出码 0。

默认**跳过** —— 联网、下文件、要代理。只有显式给 ``JM_LIVE=1`` 才跑::

    $env:JM_LIVE = "1"
    uv run pytest tests/test_live_smoke.py -v

跑的是**全链路**，落点全在**工作区内的临时书库**（不碰真实书库、不碰共享区）：

1. ``manga download --photo --apply`` × 2 章 → 散图落 ``00-Incoming/_raw_jmcomic``；
2. ``manga convert-cbz --apply`` → 一话一个 CBZ 落收件箱；
3. ``manga ingest --apply --generate-comicinfo --zero-pad`` → 进权威库；
4. ``manga verify --series <系列名>`` → **退出码 0**（增量验收，不受存量库历史问题拖累）。

环境变量：

===================== ==============================================================
``JM_LIVE=1``         开关；没有它整个文件跳过
``JM_LIVE_PHOTOS``    章节 ID（空格分隔），默认 ``404005 390442``
``JM_LIVE_PROXY``     代理（默认读 option 文件）
===================== ==============================================================
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from manga.cli import app

runner = CliRunner()

LIVE = os.environ.get("JM_LIVE") == "1"
DEFAULT_PHOTOS = "404005 390442"

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="真机冒烟：需要联网与代理。设 JM_LIVE=1 后重跑（JM_LIVE_PHOTOS 可指定章节 ID）。",
)


def _invoke(*args: str):
    result = runner.invoke(app, list(args))
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


def _photos() -> list[str]:
    return [p for p in os.environ.get("JM_LIVE_PHOTOS", DEFAULT_PHOTOS).split() if p]


@pytest.mark.usefixtures("scripts_dir")
def test_live_download_to_verify(workdir: Path, library_root: Path) -> None:
    """全链路：2 章下载 → CBZ → 入库 → 校验 0。"""
    photos = _photos()
    assert len(photos) >= 2, "至少要 2 个章节 ID"

    raw = library_root / "00-Incoming" / "_raw_jmcomic"
    inbox = library_root / "00-Incoming" / "Manga"
    export = library_root / "Export" / "Manga"
    export.mkdir(parents=True, exist_ok=True)
    before_export = sorted(p.name for p in export.rglob("*"))

    # ① 下载两章
    for photo_id in photos[:2]:
        code, payload = _invoke(
            "download",
            photo_id,
            "--photo",
            "--apply",
            "--dest",
            str(raw),
            "--library-root",
            str(library_root),
        )
        assert code == 0, f"下载 {photo_id} 失败：{payload}"
        assert payload["data"]["file_count"] > 0, f"下载 {photo_id} 报告 0 张图"

    assert any(raw.rglob("*.webp")) or any(raw.rglob("*.jpg")), "散图没落到暂存区"

    # ② 打包成 CBZ
    code, payload = _invoke(
        "convert-cbz",
        "--apply",
        "--source-root",
        str(raw),
        "--target-root",
        str(inbox),
        "--library-root",
        str(library_root),
    )
    assert code == 0, f"打包失败：{payload}"

    cbz_files = sorted(inbox.rglob("*.cbz"))
    assert cbz_files, "收件箱里没有 CBZ"

    # ③ 入库
    code, payload = _invoke(
        "ingest",
        "--apply",
        "--generate-comicinfo",
        "--zero-pad",
        "--library-root",
        str(library_root),
    )
    assert code == 0, f"入库失败：{payload}"

    library = library_root / "Manga" / "Library"
    assert list(library.rglob("*.cbz")), "权威库里没有 CBZ"

    # ④ 增量校验：只验刚入库的那个系列
    series_dirs = {p.parent.name for p in library.rglob("*.cbz")}
    assert series_dirs, "找不到系列目录"
    series = sorted(series_dirs)[0]

    code, payload = _invoke("verify", "--series", series, "--library-root", str(library_root))
    assert code == 0, f"校验未通过（退出码 {code}）：{json.dumps(payload, ensure_ascii=False)[:2000]}"

    # ⑤ 红线：共享区不能多出任何东西
    after_export = sorted(p.name for p in export.rglob("*"))
    assert after_export == before_export, "红线：共享区出现了新增文件"
