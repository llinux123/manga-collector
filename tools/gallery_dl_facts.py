"""从**安装的 gallery-dl 源码**里提取权威事实，供配置生成与回归校验。

背景：gallery-dl 的默认目录格式产出的是 ``v01 c001`` 这类**卷/话号拼接**的名字
（``{volume:?v/ />02}c{chapter:>03}…``），**过不了** manga 工作区的入库闸门
（命名规范要求 ``<卷或话号> - <标题>.cbz``，且 ``ComicInfo.Number`` 必填）。

修法有两种：

1. 改 gallery-dl 的 ``directory``/``filename`` 模板 → 名字直接合规；
2. 让入库脚本认识 ``v01 c001`` → 但那要改 manga 工作区的脚本（跨仓库，本项目不碰）。

选 (1)。而模板必须**按站点**配 —— 各站提供的关键词不一样
（``{manga}`` / ``{comic}`` / ``{issue}`` / ``{chapter}`` / ``{title}``…），
配错了会渲染成 ``None``（gallery-dl 的 ``keywords-default``）。

本模块把这些事实变成**可执行**的东西：

* ``uv run python tools/gallery_dl_facts.py sites`` —— 打印各站可用的关键词
  （Markdown 表格，可直接粘进 reference）；
* ``uv run python tools/gallery_dl_facts.py config`` —— 生成
  ``src/manga/data/gallery-dl-option.json``（入库合规的目录/文件名模板）；
* ``uv run python tools/gallery_dl_facts.py check`` —— 校验已生成的配置是否
  仍覆盖本机 gallery-dl 的全部「章节站」，有漂移就非零退出
  （``tests/test_gallery_dl_facts.py`` 调它）。

全部只读 introspection：不联网、不下载、不碰磁盘（``config`` 子命令只写仓库内的一个文件）。
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "src" / "manga" / "data" / "gallery-dl-option.json"

#: 入库闸门要求的文件名形态：``<话号> - <标题>.cbz``（``-`` 前是话号，3 位补零）
#: 目录形态：``<系列名>/<话号> - <标题>.cbz`` —— 一层系列目录，**不**带来源层
#: （来源层由 ``manga ingest`` 从收件箱层级推断，这里多一层会变成「层级过深」告警）。
MANGA_DIRECTORY_FMT = ["{manga}", "{chapter:>03} - {title|manga}"]

#: 包内图片名（不影响入库，但要有可读的排序）
MANGA_FILENAME_FMT = "{manga}_c{chapter:>03}{chapter_minor:?//}_{page:>03}.{extension}"

#: 合规命名需要的关键词。缺任何一个 → 该站不能套用通用模板。
REQUIRED_KEYWORDS = ("manga", "chapter")

#: 备选：这些站没有 ``{manga}``，但有等价词，可作为系列名
SERIES_ALIASES = {
    "comic": "manga",
    "series": "manga",
    "album": "manga",
    "title": "manga",
}


def _import_gallery_dl() -> Any:
    try:
        import gallery_dl.extractor  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise SystemExit(
            "本机没装 gallery-dl（读不到源码就提取不了事实）。\n"
            "装法：uv tool install gallery-dl / scoop install gallery-dl"
        ) from exc
    import gallery_dl.extractor as mod

    return mod


def _module_keywords(cls: type) -> set[str]:
    """该提取器**所在模块**里赋值过的关键词。

    gallery-dl 的关键词是**运行时**写进 ``kwdict`` 的，类属性上看不到；
    只能在模块源码里找 ``"keyword"`` 字面量。粒度粗到「模块」，
    对本项目的用途（按站配模板）足够 —— 站与站一一对应。
    """
    module = inspect.getmodule(cls)
    if module is None:
        return set()
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):  # pragma: no cover - 内建模块等
        return set()

    keys: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith('"'):
            continue
        end = stripped.find('"', 1)
        if end < 0:
            continue
        key = stripped[1:end]
        # 关键词只可能是标识符形态
        if key and key.replace("_", "").isalnum() and not key[0].isdigit():
            keys.add(key)
    return keys


def _class_keywords(cls: type) -> set[str]:
    """类自身（含基类链）声明的 ``directory_fmt`` / ``filename_fmt`` 里用到的关键词。"""
    keys: set[str] = set()
    for klass in cls.__mro__:
        for attr in ("directory_fmt", "filename_fmt"):
            fmt = getattr(klass, attr, None)
            if not isinstance(fmt, (tuple, list)):
                continue
            for part in fmt:
                if not isinstance(part, str):
                    continue
                for chunk in part.split("}"):
                    if "{" not in chunk:
                        continue
                    name = chunk.rsplit("{", 1)[-1].split(":")[0].split("!")[0].split("|")[0]
                    if name:
                        keys.add(name)
    return keys


def chapter_sites() -> list[dict[str, Any]]:
    """所有「章节站」的事实：分类、可用关键词、能套模板吗。"""
    mod = _import_gallery_dl()

    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for cls in mod.extractors():
        subcategory = getattr(cls, "subcategory", "")
        if subcategory != "chapter":
            continue
        category = getattr(cls, "category", "")
        key = (category, cls.__name__)
        if key in seen:
            continue
        seen.add(key)

        keywords = _class_keywords(cls) | _module_keywords(cls)
        series = next(
            (alias for alias in ("manga", *SERIES_ALIASES) if alias in keywords),
            "",
        )
        has_chapter = "chapter" in keywords
        facts.append(
            {
                "category": category,
                "class": cls.__name__,
                "series_keyword": series,
                "has_chapter": has_chapter,
                "has_title": "title" in keywords,
                "has_volume": "volume" in keywords,
                "usable": bool(series) and has_chapter,
            }
        )

    facts.sort(key=lambda f: (f["category"], f["class"]))
    return facts


def _template_for(fact: dict[str, Any]) -> tuple[list[str], str]:
    """该站该用哪套模板。

    系列名关键词不叫 ``manga`` 的站（如 ``{comic}``）→ 模板里换成它。
    """
    series = fact["series_keyword"] or "manga"
    directory = [
        "{" + series + "}",
        "{chapter:>03} - {title|" + series + "}",
    ]
    filename = "{" + series + "}_c{chapter:>03}{chapter_minor:?//}_{page:>03}.{extension}"
    return directory, filename


def build_config() -> dict[str, Any]:
    """生成 ``{"extractor": {<category>: {"chapter": {...}}}}`` 配置。

    按 **分类 + 子分类** 下钻（gallery-dl 的配置查找是
    ``extractor.<category>.<subcategory>`` 逐级回退，见 ``config.interpolate``），
    只改「章节」子分类的目录/文件名模板 —— 封面、系列页等子分类保持上游默认。
    """
    per_category: dict[str, dict[str, Any]] = {}

    for fact in chapter_sites():
        if not fact["usable"]:
            continue
        category = fact["category"]
        if not category:
            continue
        directory, filename = _template_for(fact)
        entry = per_category.setdefault(category, {})
        # 同一分类可能有多个章节提取器；模板一致时取第一个，不一致时报出来
        chapter = entry.setdefault("chapter", {})
        if chapter:
            if chapter.get("directory") != directory:
                print(
                    f"[warn] {category}: 多个章节提取器的关键词不一致，"
                    f"取 {fact['class']} 的模板",
                    file=sys.stderr,
                )
            continue
        chapter["directory"] = directory
        chapter["filename"] = filename

    return {"extractor": per_category}


def render_sites_markdown() -> str:
    """各站关键词表（Markdown）。"""
    lines = [
        "| 分类 | 系列名关键词 | chapter | title | volume | 可套通用模板 |",
        "|---|---|---|---|---|---|",
    ]
    for fact in chapter_sites():
        lines.append(
            "| `{category}` | {series} | {ch} | {ti} | {vo} | {ok} |".format(
                category=fact["category"],
                series=fact["series_keyword"] or "—",
                ch="✅" if fact["has_chapter"] else "—",
                ti="✅" if fact["has_title"] else "—",
                vo="✅" if fact["has_volume"] else "—",
                ok="✅" if fact["usable"] else "❌",
            )
        )
    return "\n".join(lines)


def check() -> int:
    """校验已生成的配置仍覆盖本机全部「可套模板」的章节站。

    返回退出码：0 = 一致，1 = 有漂移。
    """
    if not CONFIG_PATH.is_file():
        print(f"缺少配置文件：{CONFIG_PATH}", file=sys.stderr)
        return 1

    try:
        current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"配置文件读不出来：{exc}", file=sys.stderr)
        return 1

    expected = build_config()
    cur_ext = current.get("extractor", {})
    exp_ext = expected.get("extractor", {})

    problems: list[str] = []

    for category, cfg in sorted(exp_ext.items()):
        cur = cur_ext.get(category)
        if cur is None:
            problems.append(f"配置缺分类：{category}")
            continue
        chapter = cur.get("chapter")
        if chapter is None:
            problems.append(f"配置缺子分类：{category}.chapter")
            continue
        if chapter.get("directory") != cfg["chapter"]["directory"]:
            problems.append(
                f"{category}.chapter.directory 与本机 gallery-dl 不一致：\n"
                f"    当前 {chapter.get('directory')}\n"
                f"    期望 {cfg['chapter']['directory']}"
            )
        if chapter.get("filename") != cfg["chapter"]["filename"]:
            problems.append(
                f"{category}.chapter.filename 与本机 gallery-dl 不一致：\n"
                f"    当前 {chapter.get('filename')}\n"
                f"    期望 {cfg['chapter']['filename']}"
            )

    for category in sorted(cur_ext):
        if category not in exp_ext:
            problems.append(
                f"配置里有多余分类：{category}（本机 gallery-dl 已没有「章节」提取器用它）"
            )

    if problems:
        print("配置漂移：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\n修法：uv run python tools/gallery_dl_facts.py config",
            file=sys.stderr,
        )
        return 1

    print(f"配置与本机 gallery-dl 一致（{len(exp_ext)} 个分类）")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "sites"

    if command == "sites":
        print(render_sites_markdown())
        return 0

    if command == "config":
        config = build_config()
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {CONFIG_PATH}（{len(config['extractor'])} 个分类）")
        return 0

    if command == "check":
        return check()

    print(f"未知命令：{command}（可用：sites / config / check）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
