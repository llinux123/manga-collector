"""mangadex-dl（mangadex-downloader）适配层（P3）。

MangaDex 专项下载器：只做 MangaDex，但**默认写 ComicInfo.xml**
（``--no-metadata`` 可关）—— 这是它跟 gallery-dl 最大的差别，
所以适配层下载完必须**逐个 CBZ 检查里面有没有 ComicInfo.xml**，
把「有没有元数据」变成 agent 可判的字段。

与 gallery-dl 适配层共用的坑：

1. **「退出码 0 但没有产物」的假成功** —— 同样用前后快照对比 CBZ，
   一个都没有 → 抛 ``NO_ARTIFACT``。
2. **落点红线** —— 复用 :func:`.jmcomic.ensure_safe_dest`（共享区 / 权威库一律拒绝）。

本机**默认没装** ``mangadex-dl``（见 ``.agents/skills/manga/references/sources.md``）：
找不到就是找不到，抛 ``TOOL_MISSING``（退出码 4）并给安装命令 ——
**这是正常路径**，不是崩溃；本项目**绝不自动安装**。
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Sequence

from ..envelope import EXIT_ERROR, EXIT_NOTFOUND, MangaError
from .gallery_dl import (
    _cbz_snapshot,
    _child_env,
    _decode,
    _diff_artifacts,
    _run,
    _scratch_dir,
    _tail,
    _temp_base,
)
from .jmcomic import ensure_safe_dest

# ── 常量 ──────────────────────────────────────────────────────────────────

#: 可执行文件名
EXECUTABLE_NAME = "mangadex-dl"

#: 安装命令（pip 系）
INSTALL_COMMANDS: tuple[str, ...] = (
    "uv tool install mangadex-dl",
    "pip install mangadex-dl",
)

#: ComicInfo.xml 的文件名（大小写不敏感）
COMICINFO_NAME = "comicinfo.xml"


# ── 工具定位 ──────────────────────────────────────────────────────────────


def find_executable(*, name: str = EXECUTABLE_NAME) -> Path:
    """定位 mangadex-dl 可执行文件；找不到抛 ``TOOL_MISSING``（退出码 4）。

    本机默认没装 —— 这是**正常状态**，hint 里给安装命令。
    """
    found = shutil.which(name)
    if found:
        return Path(found)

    raise MangaError(
        f"找不到 {name} 可执行文件",
        code="TOOL_MISSING",
        exit_code=EXIT_NOTFOUND,
        hint="安装（CLI 不会自动装）：" + " / ".join(INSTALL_COMMANDS),
        data={"tool": name},
    )


# ── ComicInfo 检查 ───────────────────────────────────────────────────────


def _comicinfo_state(path: Path) -> bool | None:
    """这个 CBZ 里的 ComicInfo.xml 状态。

    只读 zip 的目录表（``namelist``），**不解压**内容 —— 一个几百页的 CBZ
    不该为了看一眼元数据被整个读一遍。

    返回 ``True``=有 / ``False``=没有 / ``None``=文件读不动（不是合法的 zip）。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    return any(Path(name).name.lower() == COMICINFO_NAME for name in names)


def has_comicinfo(path: Path) -> bool:
    """这个 CBZ 里有没有 ``ComicInfo.xml``（读不动的一律当「没有」）。"""
    return _comicinfo_state(path) is True


def _comicinfo_stats(files: list[str]) -> tuple[dict[str, int], list[str]]:
    """统计产物里「有 / 没有 ComicInfo.xml」。

    返回 ``({"with": n, "without": m}, 读不动的文件列表)``。
    读不动的 CBZ 计入 ``without``（它同样没有可用元数据），但单独列出来给 agent 看。
    """
    with_count = 0
    without_count = 0
    unreadable: list[str] = []
    for name in files:
        state = _comicinfo_state(Path(name))
        if state is True:
            with_count += 1
            continue
        without_count += 1
        if state is None:
            unreadable.append(name)
    return {"with": with_count, "without": without_count}, unreadable


# ── 下载 ──────────────────────────────────────────────────────────────────


def download(
    url: str,
    dest: Path,
    *,
    language: str | None = None,
    export_dir: Path | None = None,
    library_dir: Path | None = None,
    timeout: float | None = 3600.0,
    extra_args: Sequence[str] = (),
) -> dict[str, Any]:
    """用 mangadex-dl 下载（``--save-as cbz``），并检查 ComicInfo.xml。

    跑 ``mangadex-dl --save-as cbz --path <dest> [--language <lang>] <url>``。

    返回::

        {
          "dest": "...", "url": "...", "language": "zh" | None,
          "count": 2,                       # 产物数
          "files": ["...cbz"],
          "comicinfo": {"with": 1, "without": 1},
          "comicinfo_unreadable": [],
          "exit_code": 0,
        }

    「退出码 0 但一个 CBZ 都没有」→ 抛 ``NO_ARTIFACT``（假成功）。
    """
    dest = Path(dest).expanduser()
    try:
        dest = dest.resolve()
    except OSError:  # pragma: no cover - 极端路径
        pass

    # 红线：共享区 / 权威库一律拒绝（复用 jmcomic 适配层的守卫）
    dest = ensure_safe_dest(dest, export_dir=export_dir, library_dir=library_dir)

    url = str(url)
    if not url.strip():
        raise MangaError("没有要下载的 URL", code="NO_URLS", exit_code=EXIT_ERROR)

    exe = find_executable()

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MangaError(
            f"落点建不出来：{dest}（{exc}）",
            code="DEST_UNWRITABLE",
            exit_code=EXIT_ERROR,
            data={"dest": str(dest)},
        ) from exc

    before = _cbz_snapshot(dest)

    argv = [str(exe), "--save-as", "cbz", "--path", str(dest), *[str(a) for a in extra_args]]
    if language:
        argv += ["--language", str(language)]
    argv.append(url)

    try:
        with _scratch_dir(_temp_base(dest)) as scratch:
            proc = _run(argv, cwd=scratch, timeout=timeout, env=_child_env(scratch))
    except subprocess.TimeoutExpired as exc:
        raise MangaError(
            f"mangadex-dl 下载超时（{timeout}s）",
            code="TIMEOUT",
            exit_code=EXIT_ERROR,
            hint="MangaDex API 限速；可加 --delay-requests 之类的参数重试",
            data={"dest": str(dest), "url": url, "timeout": timeout},
        ) from exc
    except OSError as exc:
        raise MangaError(
            f"mangadex-dl 启动失败：{exc}",
            code="SPAWN_FAILED",
            exit_code=EXIT_ERROR,
            data={"dest": str(dest), "exe": str(exe)},
        ) from exc

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)

    after = _cbz_snapshot(dest)
    new_files, changed_files = _diff_artifacts(before, after)
    produced = [*new_files, *changed_files]

    if not produced:
        detail = _tail(stderr) or _tail(stdout) or f"退出码 {proc.returncode}"
        if proc.returncode != 0:
            raise MangaError(
                f"mangadex-dl 下载失败（退出码 {proc.returncode}）",
                code="DOWNLOAD_FAILED",
                exit_code=EXIT_ERROR,
                hint=detail or None,
                data={
                    "dest": str(dest),
                    "url": url,
                    "exit_code": proc.returncode,
                },
            )
        # 退出码 0 却一个 CBZ 都没有 —— 假成功
        raise MangaError(
            "mangadex-dl 退出码 0，但没有产出任何 .cbz（假成功）",
            code="NO_ARTIFACT",
            exit_code=EXIT_ERROR,
            hint="检查 URL 是否有效（MangaDex 章节链接形如 /chapter/<uuid>）",
            data={
                "dest": str(dest),
                "url": url,
                "exit_code": proc.returncode,
                "stderr": _tail(stderr),
            },
        )

    stats, unreadable = _comicinfo_stats(produced)

    # P1：产物在，但退出码非 0 → **部分失败**。
    #
    # mangadex-dl 遇到某些章节失败会「写完能写的、退出码非 0」——此前一律报 ok，
    # agent 会以为整本下好了，缺章的 CBZ 之后一路走进库闸门（页数 ≥ 5 就算达标）。
    if proc.returncode != 0:
        detail = _tail(stderr) or _tail(stdout) or f"退出码 {proc.returncode}"
        raise MangaError(
            f"mangadex-dl 退出码 {proc.returncode}（产物已写出，但过程有错误）",
            code="DOWNLOAD_FAILED",
            exit_code=EXIT_ERROR,
            hint=detail or None,
            data={
                "dest": str(dest),
                "url": url,
                "exit_code": proc.returncode,
                "files": produced,
                "stderr": detail,
            },
        )

    # P1：产物里有读不动的 CBZ（不是合法 zip）→ 坏包必须在这一层拦下，
    # 否则它会以「零字节/打不开」的形式混进库里。
    if unreadable:
        raise MangaError(
            f"mangadex-dl 产出 {len(unreadable)} 个读不动的 CBZ（损坏包）",
            code="CORRUPT_ARTIFACT",
            exit_code=EXIT_ERROR,
            hint="删掉这些文件后重跑同一条命令即可重新下载",
            data={
                "dest": str(dest),
                "url": url,
                "unreadable": unreadable[:20],
                "unreadable_count": len(unreadable),
                "files": produced,
            },
        )

    warnings: list[str] = []
    if stats["without"]:
        warnings.append(
            f"{stats['without']} 个 CBZ 里没有 ComicInfo.xml —— 入库闸门会判不合规，"
            "先补元数据再入库"
        )

    return {
        "dest": str(dest),
        "url": url,
        "language": language,
        "count": len(produced),
        "file_count": len(produced),
        "files": produced,
        "new_files": new_files,
        "new_count": len(new_files),
        "changed_files": changed_files,
        "comicinfo": stats,
        "comicinfo_unreadable": unreadable,
        "warnings": warnings,
        "exit_code": proc.returncode,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }
