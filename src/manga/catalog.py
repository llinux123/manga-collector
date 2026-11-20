"""权威库索引：按**车号**判断一本漫画是否已在库中（标题只作兜底）。

背景
----
以前判断「在库」只能靠标题模糊匹配：站点标题一变、或不同车号同名，就会误判。
现在入库时会把车号写进 CBZ 内的 ``ComicInfo.xml``：

* ``<Notes>jm:<album_id></Notes>`` —— 机器可解析的权威标识；
* ``<Web>https://18comic.vip/album/<album_id>/</Web>`` —— 方便阅读端点开。

本模块扫描权威库，从每个系列目录里读一份 CBZ 的 ComicInfo 建 ``车号 → 系列`` 索引，
于是「是否在库」可以先按车号精确判，命中不了再退回标题匹配。

设计取舍
--------
* 不维护持久索引文件：直接扫（一个系列只开一份 CBZ，扫到车号即停）。142 个系列
  量级下是可接受的，且能自动跟上用户手工挪动/删除目录。
* 兼容：老库里没有车号的 CBZ 不报错，只是不进 ``by_id``，仍可被标题兜底命中。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

#: ComicInfo 里的车号：优先 Notes 的 ``jm:<id>``，其次 Web 的 ``/album/<id>/``
_NOTES_ID_RE = re.compile(r"jm\s*[:=]\s*(\d+)", re.IGNORECASE)
_WEB_ID_RE = re.compile(r"/album/(\d+)", re.IGNORECASE)
_NOTES_RE = re.compile(r"<Notes>(.*?)</Notes>", re.IGNORECASE | re.DOTALL)
_WEB_RE = re.compile(r"<Web>(.*?)</Web>", re.IGNORECASE | re.DOTALL)

#: 标题归一化时剔除的标点 / 空白（含中日文括号与常见分隔符）
_PUNCT_RE = re.compile(r"[\[\]【】（）()〈〉《》「」『』\s/\\!！~～\-—–_,，.。:：;；+×*|]+")

_LCMAP_SIMPLIFIED_CHINESE = 0x02000000


def _to_simplified(text: str) -> str:
    """繁转简（Windows LCMapStringEx）；非 Windows / 调用失败则原样返回。"""
    if not text:
        return text
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        n = k32.LCMapStringEx(
            "zh-CN", _LCMAP_SIMPLIFIED_CHINESE, text, len(text), None, 0, None, None, 0
        )
        if n <= 0:
            return text
        buf = ctypes.create_unicode_buffer(n)
        r = k32.LCMapStringEx(
            "zh-CN", _LCMAP_SIMPLIFIED_CHINESE, text, len(text), buf, n, None, None, 0
        )
        return buf.value if r > 0 else text
    except Exception:  # noqa: BLE001 - 非 Windows 环境没有这个 API
        return text


def normalize_title(text: str) -> str:
    """标题归一化：繁转简 + 去标点空白 + 小写，用于标题兜底匹配。"""
    if not text:
        return ""
    return _PUNCT_RE.sub("", _to_simplified(text)).strip().lower()


def extract_album_id(comicinfo_text: str) -> str | None:
    """从 ComicInfo.xml 文本里抽车号（Notes 优先，Web 兜底）。"""
    if not comicinfo_text:
        return None
    notes = _NOTES_RE.search(comicinfo_text)
    if notes:
        m = _NOTES_ID_RE.search(notes.group(1))
        if m:
            return m.group(1)
    web = _WEB_RE.search(comicinfo_text)
    if web:
        m = _WEB_ID_RE.search(web.group(1))
        if m:
            return m.group(1)
    # 兜底：整段里出现 jm:<id>
    m = _NOTES_ID_RE.search(comicinfo_text)
    return m.group(1) if m else None


def _read_comicinfo(cbz: Path) -> str | None:
    """读 CBZ 包根的 ComicInfo.xml；没有 / 打不开返回 None。"""
    try:
        with zipfile.ZipFile(cbz) as zf:
            for name in zf.namelist():
                if name.lower().endswith("comicinfo.xml"):
                    return zf.read(name).decode("utf-8", "replace")
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _series_dirs(library: Path) -> list[Path]:
    """库里**直接装着 CBZ** 的目录（= 系列目录），去重后排序。"""
    if not library.is_dir():
        return []
    parents: set[Path] = set()
    for cbz in library.rglob("*.cbz"):
        if cbz.is_file():
            parents.add(cbz.parent)
    return sorted(parents)


def scan_library(library: Path) -> dict[str, Any]:
    """扫描权威库，返回 ``{by_id, series}``。

    * ``by_id``：``车号 -> {"series", "dir", "album_id"}``
    * ``series``：``[{"series", "dir", "cbz_count", "album_id", "title_norm"}]``
    """
    library = Path(library)
    by_id: dict[str, dict[str, Any]] = {}
    series_rows: list[dict[str, Any]] = []

    for directory in _series_dirs(library):
        try:
            rel = directory.relative_to(library)
            series_name = str(rel) if str(rel) != "." else directory.name
        except ValueError:  # pragma: no cover - 理论上不会发生
            series_name = directory.name
        cbz_files = sorted(p for p in directory.glob("*.cbz") if p.is_file())
        album_id: str | None = None
        # 同一系列里每份 CBZ 都带同一个车号，读前几份即可（避免整库遍历时把
        # 每个 CBZ 都开一遍，142 个系列 × 数十话会明显拖慢）。
        for cbz in cbz_files[:3]:
            album_id = extract_album_id(_read_comicinfo(cbz) or "")
            if album_id:
                break
        row = {
            "series": series_name,
            "dir": str(directory),
            "cbz_count": len(cbz_files),
            "album_id": album_id,
            # 标题归一化只取**叶子目录名**（书名）——
            # 库是「来源名/书名」两层结构（``禁漫 (JMComic)\\My Work``），
            # 拿整条路径归一化会把来源名也算进去（→ ``禁漫jmcomicmywork``），
            # 于是 ``lookup --title "My Work"`` 永远匹配不上明明在库的书。
            "title_norm": normalize_title(directory.name),
        }
        series_rows.append(row)
        if album_id and album_id not in by_id:
            by_id[album_id] = {
                "series": series_name,
                "dir": str(directory),
                "album_id": album_id,
            }

    return {"by_id": by_id, "series": series_rows}


def _title_match(series_rows: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    """标题兜底：先归一化精确匹配，再「长度>6 且互为前缀」取最长。"""
    target = normalize_title(title)
    if not target:
        return None
    exact = [r for r in series_rows if r["title_norm"] == target]
    if exact:
        return exact[0]
    candidates = [
        r
        for r in series_rows
        if len(r["title_norm"]) > 6
        and (r["title_norm"].startswith(target) or target.startswith(r["title_norm"]))
    ]
    if not candidates:
        return None
    # 比**归一化标题**的长度，不是路径长度 —— 路径长只说明来源层深，
    # 与标题匹配质量无关（否则「来源名很长」的书会永远赢）。
    return max(candidates, key=lambda r: len(r["title_norm"]))


def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "series": row["series"],
        "dir": row["dir"],
        "album_id": row.get("album_id"),
        "cbz_count": row.get("cbz_count"),
    }


def lookup(
    library: Path,
    *,
    album_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """判断一本漫画是否在库中：车号优先，标题兜底。

    返回稳定结构，供 CLI 直接塞进信封 ``data``。
    """
    index = scan_library(library)
    by_id: dict[str, dict[str, Any]] = index["by_id"]
    series_rows: list[dict[str, Any]] = index["series"]

    result: dict[str, Any] = {
        "library": str(library),
        "series_count": len(series_rows),
        "id_count": len(by_id),
        "in_library": False,
        "by": None,
        "match": None,
    }

    if album_id:
        hit = by_id.get(str(album_id).strip())
        if hit:
            result.update(
                in_library=True,
                by="album_id",
                match={
                    "series": hit["series"],
                    "dir": hit["dir"],
                    "album_id": hit["album_id"],
                    "cbz_count": next(
                        (r["cbz_count"] for r in series_rows if r["dir"] == hit["dir"]), None
                    ),
                },
            )
            return result

    if title:
        row = _title_match(series_rows, title)
        if row is not None:
            result.update(in_library=True, by="title", match=_public(row))
            return result

    # 没命中：给点候选，方便人肉确认（标题前缀相近的）
    if title:
        target = normalize_title(title)
        near = [
            _public(r)
            for r in series_rows
            if target and len(r["title_norm"]) > 6 and (target in r["title_norm"] or r["title_norm"] in target)
        ][:5]
        result["candidates"] = [c for c in near if c]
    return result
