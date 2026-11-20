"""pytest 夹具：在临时目录里造一个**能通过校验**的小型漫画库。

沿用本仓库 ``scripts/.selftest/`` 的做法：自建样本、自行清理，不留垃圾。

⚠ 不用 pytest 自带的 ``tmp_path``：它在 Windows 上会用 ``\\\\?\\`` 扩展路径前缀，
沙箱的文件过滤器不认这种写法，会一律拒绝访问。改用工作区内的普通路径。
"""

from __future__ import annotations

import io
import shutil
import uuid
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = REPO_ROOT / ".test-tmp"

COMIC_INFO = """<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Title>Chapter One</Title>
  <Series>Sample Series</Series>
  <Number>1</Number>
  <Volume>1</Volume>
  <Count>1</Count>
  <Summary>测试样本</Summary>
  <LanguageISO>zh-CN</LanguageISO>
  <Manga>Yes</Manga>
  <PageCount>5</PageCount>
</ComicInfo>
"""


def _tiny_jpeg() -> bytes:
    """用 Pillow 生成一张最小的合法 JPEG（避免手写字节出错）。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 200, 200)).save(buf, format="JPEG", quality=50)
    return buf.getvalue()


_TINY_JPEG = _tiny_jpeg()


def make_cbz(
    path: Path,
    *,
    pages: int = 5,
    number: int = 1,
    title: str = "Chapter One",
    series: str = "Sample Series",
    notes: str = "",
    web: str = "",
) -> Path:
    """造一个合规的 CBZ：图片平铺在包根目录 + 根目录 ComicInfo.xml。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    info = (
        COMIC_INFO.replace("<Number>1</Number>", f"<Number>{number}</Number>")
        .replace("<Title>Chapter One</Title>", f"<Title>{title}</Title>")
        .replace("<Series>Sample Series</Series>", f"<Series>{series}</Series>")
        .replace("<PageCount>5</PageCount>", f"<PageCount>{pages}</PageCount>")
    )
    extra = ""
    if notes:
        extra += f"  <Notes>{notes}</Notes>\n"
    if web:
        extra += f"  <Web>{web}</Web>\n"
    if extra:
        info = info.replace("</ComicInfo>", extra + "</ComicInfo>")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(1, pages + 1):
            zf.writestr(f"{i:05d}.jpg", _TINY_JPEG)
        zf.writestr("ComicInfo.xml", info)
    return path


@pytest.fixture()
def workdir() -> Path:
    """工作区内的临时目录（普通路径，避开 ``\\\\?\\`` 前缀问题）。

    ⚠ 不用 ``tempfile.mkdtemp``：它在 Windows 上会给新目录套一层受限权限，
    之后在该目录里再建子目录就会被拒绝。普通 ``mkdir`` 继承父目录权限，行为正常。
    """
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def library_root(workdir: Path) -> Path:
    """一个只含合规样本的书库根目录。"""
    root = workdir / "Library"
    series = root / "Manga" / "Library" / "Sample Series"
    make_cbz(series / "001 - Chapter One.cbz")
    make_cbz(series / "002 - Chapter Two.cbz", number=2, title="Chapter Two")
    for sub in ("00-Incoming\\Manga", "Export\\Manga", "Manga\\Metadata", "00-Incoming\\_raw_jmcomic"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def inbox(library_root: Path) -> Path:
    """收件箱里放一个待入库的 CBZ。"""
    make_cbz(
        library_root / "00-Incoming" / "Manga" / "Inbox Series" / "001 - Incoming.cbz",
        series="Inbox Series",
    )
    return library_root / "00-Incoming" / "Manga"


@pytest.fixture()
def scripts_dir() -> Path:
    """manga 工作区的 scripts/ 目录（真实脚本，用于端到端跑通）。

    该目录**属于 manga 工作区**，不在本仓库内；找不到时跳过依赖它的用例，
    并在跳过原因里说明怎么指路。
    """
    from manga.config import find_workspace

    workspace = find_workspace()
    if workspace is None:
        pytest.skip(
            "未找到 manga 工作区（流水线脚本的宿主）。"
            "设环境变量 MANGA_WORKSPACE=<工作区路径> 后重跑。"
        )
    scripts = workspace / "scripts"
    if not (scripts / "Test-MangaLibrary.ps1").is_file():
        pytest.skip(f"manga 工作区缺少 scripts/Test-MangaLibrary.ps1：{scripts}")
    return scripts
