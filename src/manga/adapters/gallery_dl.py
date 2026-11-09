"""gallery-dl 适配层（P3）。

把外部 ``gallery-dl`` CLI 包成统一的 dict / :class:`MangaError`，
并把三个「工具特有的坑」挡在适配层里，不外泄给 agent：

1. **PyInstaller 的父目录报错** —— 本机的 gallery-dl 是 PyInstaller 打包版
   （onefile）：运行时会被解压到 ``%TEMP%`` 下的 ``_MEIxxxxxx``。
   在受限环境（系统临时目录不可写）里会报
   ``[PYI-xxxxx:ERROR] Failed to create parent directory structure.``
   这**不是下载失败**，是环境问题。
   ⚠ 解压位置由 **TEMP / TMP** 决定，**与 cwd 无关**（本机实测：换 cwd 没用）。
   所以每次调用都把子进程的 ``TEMP`` / ``TMP`` 指到一个**存在且可写**的目录
   （指向不存在的目录会改报 ``Could not create temporary directory!``），
   并把这段 stderr 识别成 ``ENV_PYINSTALLER``（退出码 2），而不是 ``DOWNLOAD_FAILED``。

2. **「退出码 0 但没有产物」的假成功** —— gallery-dl 退出码 0 不代表下到了东西。
   适配层在跑之前快照 dest 下的 ``.cbz``，跑之后对比；只有真的多出产物才算成功，
   否则抛 ``NO_ARTIFACT``。这与 jmcomic 适配层的「manifest 取错属性 → 成功但 0 张图」
   是同一个坑（参见 ``adapters/jmcomic.py`` 的 ``_manifest_objects``）。

3. **预检绝不落盘** —— :func:`precheck` 只跑 ``-s``（simulate）+ ``--no-input``，
   命令行里**不出现** ``-d`` / ``--cbz``，所以「这个 URL 没有提取器」可以被明确判出来，
   而不会顺手下一个文件。

落点红线复用 :func:`.jmcomic.ensure_safe_dest`（共享区 ``Export\\Manga`` 与权威库
``Manga\\Library`` 一律拒绝），不复制一份。

约定（与 ``adapters/jmcomic.py`` 一致）：

* 成功返回 dict（直接进 JSON 信封的 ``data`` 字段）；
* 失败抛 :class:`~manga.envelope.MangaError`，``code`` + ``exit_code`` 决定 agent 的后续动作；
* **绝不 print 到 stdout**（stdout 只能有一个 JSON 信封）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..envelope import EXIT_ERROR, EXIT_NOTFOUND, MangaError
from .jmcomic import ensure_safe_dest

# ── 常量 ──────────────────────────────────────────────────────────────────

#: 可执行文件名
EXECUTABLE_NAME = "gallery-dl"

#: 安装命令（两条路都给：pip/uv 系 与 Windows scoop）
INSTALL_COMMANDS: tuple[str, ...] = (
    "uv tool install gallery-dl",
    "scoop install gallery-dl",
)

#: 备用定位路径（``shutil.which`` 找不到时的兜底）
#:
#: scoop 的安装布局是 ``<scoop>/apps/<pkg>/current/<exe>.exe``（``current`` 是个软链），
#: 并且 ``<scoop>/shims/`` 下有一个转发用的 exe。两条都列上。
FALLBACK_PATHS: tuple[Path, ...] = (
    Path.home() / "scoop" / "apps" / "gallery-dl" / "current" / "gallery-dl.exe",
    Path.home() / "scoop" / "apps" / "gallery-dl" / "current" / "gallery-dl",
    Path.home() / "scoop" / "shims" / "gallery-dl.exe",
)

#: PyInstaller 打包版的报错片段（受限环境下建不了父目录）
PYINSTALLER_ERROR = "failed to create parent directory structure"

#: 子进程临时目录名（放在可写工作目录里，用完即删）
SCRATCH_DIRNAME = ".manga-tmp"

#: 「这个 URL 没有提取器」的判定关键词（小写比较）
UNSUPPORTED_PATTERNS: tuple[str, ...] = (
    "no suitable extractor found",
    "unsupported url",
    "no extractor",
    "unable to extract",
)

#: ``Using extractor 'xxx'``（verbose 输出里的站点名）
_EXTRACTOR_RE = re.compile(r"using extractor ['\"](?P<name>[^'\"]+)['\"]", re.IGNORECASE)

#: ``Title: xxx``
_TITLE_RE = re.compile(r"^\s*title\s*[:=]\s*(?P<title>.+?)\s*$", re.IGNORECASE | re.MULTILINE)

#: gallery-dl 的退出码是**位掩码**（见上游 ``job.py`` 的 ``self.status |= N``）：
#:
#: ======  ==========================================================
#: 位      含义
#: ======  ==========================================================
#: 1       意外异常
#: 2       OSError（读数据失败）
#: 4       **部分文件下载失败**（``Failed to download xxx``）
#: 8       挑战页（Cloudflare 等）
#: 16      认证 / 授权失败
#: 32      输入 / 配置错误（含「该 URL 没有提取器」）
#: 128     解析失败（JSONDecodeError）
#: ======  ==========================================================
#:
#: ⚠ **位 4 最要命**：产物 CBZ **已经写出来了**，只是少了几页。
#: 上游退出码非 0 但产物存在 → **不能**当成功（那会让缺页的 CBZ 进库）。
_EXIT_PARTIAL = 4
_EXIT_AUTH = 16
_EXIT_INPUT = 32

#: ``Failed to download xxx``（位 4 的日志行）
_FAILED_RE = re.compile(r"failed to download\s+(?P<name>\S+)", re.IGNORECASE)

#: simulate 输出的目录树行：``└─ Series/001 - Chapter.cbz``
_TREE_RE = re.compile(r"^[│\s]*[├└]─\s*(?P<label>.+?)\s*$")


# ── 工具定位 ──────────────────────────────────────────────────────────────


def find_executable(
    *,
    name: str = EXECUTABLE_NAME,
    fallbacks: Sequence[Path] | None = None,
) -> Path:
    """定位 gallery-dl 可执行文件。

    顺序：``shutil.which`` → :data:`FALLBACK_PATHS`（scoop 布局）→ 报错。

    找不到是**正常的环境状态**（退出码 4），不是崩溃：hint 里给安装命令，
    本项目**绝不自动安装**。
    """
    found = shutil.which(name)
    if found:
        return Path(found)

    candidates = FALLBACK_PATHS if fallbacks is None else tuple(fallbacks)
    searched: list[str] = []
    for candidate in candidates:
        path = Path(candidate)
        searched.append(str(path))
        try:
            if path.is_file():
                return path
        except OSError:  # pragma: no cover - 权限异常按「没有」处理
            continue

    raise MangaError(
        f"找不到 {name} 可执行文件",
        code="TOOL_MISSING",
        exit_code=EXIT_NOTFOUND,
        hint="安装（CLI 不会自动装）：" + " / ".join(INSTALL_COMMANDS),
        data={"tool": name, "searched": searched},
    )


def _writable_dir(preferred: Path | None = None) -> Path:
    """挑一个**可写**目录：子进程临时目录（TEMP）不可用时的备选落点。

    PyInstaller 打包的 gallery-dl（scoop 版）会把运行时解压到临时目录
    （``_MEIxxxxxx``）—— 解压位置由 **TEMP / TMP** 决定，不受 cwd 影响。
    在受限环境里（DSH 沙箱）系统临时目录不可写 → 报
    ``Failed to create parent directory structure``。

    候选顺序：当前目录 → 调用方指定的目录（落点）→ 系统临时目录。
    当前目录排在前面，是为了**别把工具的解压垃圾留在书库里**。
    """
    candidates: list[Path] = [Path.cwd()]
    if preferred is not None:
        path = Path(preferred)
        candidates.append(path if path.is_dir() else path.parent)
    candidates.append(Path(tempfile.gettempdir()))

    for candidate in candidates:
        try:
            if candidate.is_dir() and os.access(candidate, os.W_OK):
                return candidate
        except OSError:  # pragma: no cover - 权限异常按「不可用」处理
            continue
    return Path.cwd()


# ── 底层执行 ──────────────────────────────────────────────────────────────


def _decode(raw: Any) -> str:
    """把子进程输出解成文本（Windows 控制台可能是 GBK）。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _tail(text: str, limit: int = 800) -> str:
    """输出尾部（给 agent 看真实原因，别只丢一句「有告警」）。"""
    stripped = (text or "").strip()
    return stripped[-limit:] if len(stripped) > limit else stripped


@contextmanager
def _scratch_dir(base: Path) -> Iterator[Path]:
    """给子进程准备一个**存在且可写**的临时目录（用完即删）。

    PyInstaller（onefile）把运行时解压到 ``%TEMP%`` 下的 ``_MEIxxxxxx``。
    TEMP 必须指向一个**已存在**的目录 —— 指向不存在的目录会改报
    ``Could not create temporary directory!``；所以这里先建好再指过去。

    用一个子目录（而不是直接把 TEMP 指到落点/仓库根）是为了隔离：
    ``_MEIxxxxxx`` 会被删在它自己家里，不留在落点或仓库里。

    目录名带 PID 与随机后缀：两次调用不会共用同一个临时目录
    （并发跑时，后一次清理不会把前一次的临时目录删掉）。
    """
    scratch = Path(base) / f"{SCRATCH_DIRNAME}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 建不出来就退回 base 本身（它已被判定为可写）
        yield Path(base)
        return
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _temp_base(workdir: Path) -> Path:
    """子进程临时目录（TEMP/TMP）的落点。

    优先**系统临时目录** —— PyInstaller 的解压垃圾留在系统 temp 里最干净
    （不会污染落点/仓库）；系统临时目录不可写时退回工作目录，
    那正是受限环境要的「换个可写目录」。
    """
    system_temp = Path(tempfile.gettempdir())
    try:
        if system_temp.is_dir() and os.access(system_temp, os.W_OK):
            return system_temp
    except OSError:  # pragma: no cover - 权限异常按「不可用」处理
        pass
    return workdir


def _child_env(temp_dir: Path) -> dict[str, str]:
    """子进程环境：``TEMP`` / ``TMP`` / ``TMPDIR`` 都指到 ``temp_dir``。

    ⚠ PyInstaller 版 gallery-dl 的运行时解压位置由**临时目录**决定（不是 cwd）——
    受限环境里系统临时目录不可写时它必然报
    ``Failed to create parent directory structure``。本机实测：换 cwd 没用，换 TEMP 才有用。
    """
    env = os.environ.copy()
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["TMPDIR"] = str(temp_dir)
    return env


#: 本进程里**活着的**外部下载子进程（取消时由作业看门狗调用 :func:`kill_active`）。
#:
#: 为什么需要它：作业子进程在 ``os._exit`` 强退时，它启动的 gallery-dl/mangadex-dl
#: **孙进程会被留下继续跑**。这两个适配器起的子进程都登记在这里，
#: 看门狗的取消回调先把它们杀掉，再退自己。
_ACTIVE_PROCESSES: list[subprocess.Popen] = []

#: 测试接缝：``_run`` 用它而不是直接 ``subprocess.Popen``，方便替身。
_POPEN = subprocess.Popen


def kill_active() -> None:
    """杀掉所有还在跑的外部下载子进程（``Popen.kill`` 不需要特权 —— 是我们自己起的）。"""
    for proc in list(_ACTIVE_PROCESSES):
        try:
            if proc.poll() is None:
                proc.kill()
        except (OSError, AttributeError):
            pass


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float | None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """跑外部命令（argv 列表，不走 shell）。

    ``stdout`` / ``stderr`` 走管道捕获 —— 外部工具的噪音**不能**污染
    「stdout 只有一个 JSON 信封」这条契约。

    ``env`` 用来把子进程的临时目录指到可写位置（见 :func:`_child_env`）。

    进程会登记进 :data:`_ACTIVE_PROCESSES`，取消时由 :func:`kill_active` 清理。
    """
    proc = _POPEN(  # noqa: S603 - argv 为列表，不走 shell
        list(argv),
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    _ACTIVE_PROCESSES.append(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        try:
            _ACTIVE_PROCESSES.remove(proc)
        except ValueError:  # pragma: no cover - 已被 kill_active 清掉
            pass


def _is_pyinstaller_error(text: str) -> bool:
    """这段输出是不是 PyInstaller 的「建不了父目录」报错？"""
    return PYINSTALLER_ERROR in (text or "").lower()


def _raise_if_environment_problem(
    stdout: str,
    stderr: str,
    *,
    what: str,
    cwd: Path,
    url: str | None = None,
    temp_dir: Path | None = None,
) -> None:
    """PyInstaller 报错 → 环境问题（``ENV_PYINSTALLER``），不是下载失败。"""
    combined = f"{stdout}\n{stderr}"
    if not _is_pyinstaller_error(combined):
        return
    raise MangaError(
        f"gallery-dl 在受限环境下启动失败（{what}）：PyInstaller 建不了父目录",
        code="ENV_PYINSTALLER",
        exit_code=EXIT_ERROR,
        hint=(
            "这是环境问题，不是 URL / 下载失败：PyInstaller 版 gallery-dl 要往临时目录"
            "解压运行时，受限环境里临时目录不可写就会这样。"
            "本适配层已把 TEMP/TMP 指到可写目录；仍失败说明该目录也不可写 —— "
            "换用非 PyInstaller 版（uv tool install gallery-dl）即可"
        ),
        data={
            "url": url,
            "cwd": str(cwd),
            "temp_dir": str(temp_dir) if temp_dir is not None else None,
            "stderr": _tail(stderr),
        },
    )


# ── 输出解析 ──────────────────────────────────────────────────────────────


def _unsupported_reason(text: str) -> str | None:
    """这段输出里有没有「这个 URL 没有提取器」的判定？"""
    low = (text or "").lower()
    for pattern in UNSUPPORTED_PATTERNS:
        index = low.find(pattern)
        if index < 0:
            continue
        # 取包含该关键词的那一行作为原因（给 agent 看原话）
        start = text.rfind("\n", 0, index) + 1
        end = text.find("\n", index)
        line = text[start : end if end > 0 else len(text)].strip()
        return line or pattern
    return None


def _extract_site(text: str, url: str) -> str | None:
    """站点名：优先取 verbose 输出里的 extractor 名，退化到 URL 主机名。"""
    match = _EXTRACTOR_RE.search(text or "")
    if match:
        return match.group("name")
    host = re.search(r"https?://([^/\s]+)", url or "")
    if host:
        name = host.group(1).lower()
        return name[4:] if name.startswith("www.") else name
    return None


def _extract_title(text: str) -> str | None:
    """标题：优先取 ``Title:``，退化到 simulate 目录树里最深的那层目录。"""
    match = _TITLE_RE.search(text or "")
    if match:
        title = match.group("title").strip()
        if title:
            return title

    directories: list[str] = []
    for line in (text or "").splitlines():
        tree = _TREE_RE.match(line)
        if not tree:
            continue
        label = tree.group("label").strip()
        is_dir = label.endswith("/") or label.endswith("\\")
        parts = [p.strip() for p in re.split(r"[/\\]", label) if p.strip()]
        if not parts:
            continue
        # 最后一段是文件名，它前面的是目录
        directories.extend(parts if is_dir else parts[:-1])

    return directories[-1] if directories else None


# ── 产物确认 ──────────────────────────────────────────────────────────────


def _cbz_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """递归快照 ``.cbz`` 文件：路径 -> (大小, mtime_ns)。

    必须**递归**：gallery-dl 的 ``-d`` 是 base dir，CBZ 会落在子目录里。
    """
    out: dict[str, tuple[int, int]] = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for path in root.rglob("*.cbz"):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:  # pragma: no cover - 并发删除
            continue
        out[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return out


def _diff_artifacts(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> tuple[list[str], list[str]]:
    """对比两次快照：返回 (新增, 被覆盖)。"""
    new = sorted(set(after) - set(before))
    changed = sorted(path for path in set(after) & set(before) if after[path] != before[path])
    return new, changed


# ── 预检 ──────────────────────────────────────────────────────────────────


def precheck(
    url: str,
    *,
    cwd: Path | None = None,
    timeout: float | None = 120.0,
) -> dict[str, Any]:
    """URL 预检：**只模拟，不下载**。

    跑的是 ``gallery-dl -s --no-input <url>``（``-s`` = simulate）——
    命令行里**没有** ``-d`` / ``--cbz``，所以这条路径绝对不会落盘。

    ``cwd``：子进程工作目录 / 临时目录的**备选**落点（系统临时目录不可写时用）。

    返回::

        {
          "url": "...",
          "supported": True,        # 有没有提取器
          "status": "supported",    # supported | unsupported
          "reason": None,           # 不支持时的原话
          "site": "mangadex.org",   # 抠不到为 None
          "title": "Some Title",    # 抠不到为 None
          "exit_code": 0,
        }

    不支持 → ``supported=False`` 且 ``reason`` 里有 gallery-dl 的原话；
    其它失败（网络、站点抽风）→ 抛 :class:`MangaError`（``code="PRECHECK_FAILED"``）。
    """
    exe = find_executable()
    workdir = _writable_dir(cwd)
    temp_dir: Path | None = None

    # ⚠ 预检路径里绝不能出现会落盘的参数
    argv = [str(exe), "-s", "--no-input", str(url)]

    try:
        with _scratch_dir(_temp_base(workdir)) as scratch:
            temp_dir = scratch
            proc = _run(argv, cwd=scratch, timeout=timeout, env=_child_env(scratch))
    except subprocess.TimeoutExpired as exc:
        raise MangaError(
            f"gallery-dl 预检超时（{timeout}s）：{url}",
            code="TIMEOUT",
            exit_code=EXIT_ERROR,
            hint="站点太慢或网络不通；先在浏览器里确认这个 URL 能打开",
            data={"url": url, "timeout": timeout},
        ) from exc
    except OSError as exc:
        raise MangaError(
            f"gallery-dl 启动失败：{exc}",
            code="SPAWN_FAILED",
            exit_code=EXIT_ERROR,
            data={"url": url, "exe": str(exe)},
        ) from exc

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)

    _raise_if_environment_problem(
        stdout, stderr, what="预检", cwd=workdir, url=url, temp_dir=temp_dir
    )

    combined = f"{stdout}\n{stderr}"
    unsupported = _unsupported_reason(combined)
    if unsupported is not None:
        return {
            "url": str(url),
            "supported": False,
            "status": "unsupported",
            "reason": unsupported,
            "site": _extract_site(combined, str(url)),
            "title": _extract_title(stdout),
            "exit_code": proc.returncode,
            "stderr": _tail(stderr),
        }

    if proc.returncode != 0:
        raise MangaError(
            f"gallery-dl 预检失败（退出码 {proc.returncode}）：{url}",
            code="PRECHECK_FAILED",
            exit_code=EXIT_ERROR,
            hint=_tail(stderr) or _tail(stdout) or None,
            data={"url": url, "exit_code": proc.returncode, "cwd": str(workdir)},
        )

    return {
        "url": str(url),
        "supported": True,
        "status": "supported",
        "reason": None,
        "site": _extract_site(combined, str(url)),
        "title": _extract_title(stdout),
        "exit_code": proc.returncode,
        "stderr": _tail(stderr),
    }


# ── 下载 ──────────────────────────────────────────────────────────────────


def download(
    urls: Sequence[str] | str,
    dest: Path,
    *,
    export_dir: Path | None = None,
    library_dir: Path | None = None,
    cwd: Path | None = None,
    timeout: float | None = 3600.0,
    extra_args: Sequence[str] = (),
) -> dict[str, Any]:
    """用 gallery-dl 下载（``--cbz`` 直接产出 CBZ），并**确认产物**。

    跑 ``gallery-dl --cbz -d <dest> <url...>``。``-d`` 是 base dir，
    CBZ 会落在子目录里 → 产物检查是**递归**的。

    ``cwd``：子进程工作目录 / 临时目录的**备选**落点（系统临时目录不可写时用）。

    返回::

        {
          "dest": "...", "urls": [...],
          "count": 1,               # 产物数（新增 + 被覆盖）
          "files": ["...cbz"],      # 产物绝对路径
          "new_files": [...],       # 本次新增
          "new_count": 1,
          "changed_files": [...],   # 本次被覆盖
          "exit_code": 0,
        }

    「退出码 0 但一个 CBZ 都没有」→ 抛 ``NO_ARTIFACT``（假成功，本项目反复踩的坑）。
    """
    dest = Path(dest).expanduser()
    try:
        dest = dest.resolve()
    except OSError:  # pragma: no cover - 极端路径
        pass

    # 红线：共享区 / 权威库一律拒绝（复用 jmcomic 适配层的守卫）
    dest = ensure_safe_dest(dest, export_dir=export_dir, library_dir=library_dir)

    url_list = [str(urls)] if isinstance(urls, str) else [str(u) for u in urls]
    if not url_list:
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
    workdir = _writable_dir(cwd if cwd is not None else dest)
    temp_dir: Path | None = None

    argv = [str(exe), "--cbz", "-d", str(dest), *[str(a) for a in extra_args], *url_list]

    try:
        with _scratch_dir(_temp_base(workdir)) as scratch:
            temp_dir = scratch
            proc = _run(argv, cwd=scratch, timeout=timeout, env=_child_env(scratch))
    except subprocess.TimeoutExpired as exc:
        raise MangaError(
            f"gallery-dl 下载超时（{timeout}s）",
            code="TIMEOUT",
            exit_code=EXIT_ERROR,
            hint="站点太慢或网络不通；缓存会保留，重跑即可续传",
            data={"dest": str(dest), "urls": url_list, "timeout": timeout},
        ) from exc
    except OSError as exc:
        raise MangaError(
            f"gallery-dl 启动失败：{exc}",
            code="SPAWN_FAILED",
            exit_code=EXIT_ERROR,
            data={"dest": str(dest), "exe": str(exe)},
        ) from exc

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)

    _raise_if_environment_problem(
        stdout, stderr, what="下载", cwd=workdir, url=url_list[0], temp_dir=temp_dir
    )

    after = _cbz_snapshot(dest)
    new_files, changed_files = _diff_artifacts(before, after)
    produced = [*new_files, *changed_files]

    if not produced:
        detail = _tail(stderr) or _tail(stdout) or f"退出码 {proc.returncode}"
        if proc.returncode != 0:
            raise MangaError(
                f"gallery-dl 下载失败（退出码 {proc.returncode}）",
                code="DOWNLOAD_FAILED",
                exit_code=EXIT_ERROR,
                hint=detail or None,
                data={
                    "dest": str(dest),
                    "urls": url_list,
                    "exit_code": proc.returncode,
                    "cwd": str(workdir),
                },
            )
        # 退出码 0 却一个 CBZ 都没有 —— 假成功
        raise MangaError(
            "gallery-dl 退出码 0，但没有产出任何 .cbz（假成功）",
            code="NO_ARTIFACT",
            exit_code=EXIT_ERROR,
            hint=(
                "检查 URL 是否有效、站点是否需要登录；"
                "也可能产物落在了别处（-d 只是 base dir，CBZ 在子目录里）"
            ),
            data={
                "dest": str(dest),
                "urls": url_list,
                "exit_code": proc.returncode,
                "stderr": _tail(stderr),
            },
        )

    # 产物在，但退出码非 0 → **部分失败**。
    #
    # 最常见的是退出码位 4：某个文件下载失败（网络抖动 / SSL 断连），
    # 但 CBZ **已经被写出来了**，只是少了几页。报 ok 会让缺页的 CBZ 进库，
    # 之后入库闸门也查不出来（页数只要 ≥ 5 就「达标」）→ 必须在这一层拦下。
    if proc.returncode != 0:
        failed = [m.group("name") for m in _FAILED_RE.finditer(stderr)]
        detail = _tail(stderr)
        if proc.returncode & _EXIT_AUTH:
            raise MangaError(
                "gallery-dl 认证 / 授权失败（退出码含位 16）",
                code="AUTH_FAILED",
                exit_code=EXIT_ERROR,
                hint=(
                    "该站需要登录：按站点配 gallery-dl 的凭据"
                    "（配置文件里的 username/password 或 cookies），"
                    "不要把凭据写在命令行里"
                ),
                data={
                    "dest": str(dest),
                    "urls": url_list,
                    "exit_code": proc.returncode,
                    "files": produced,
                    "stderr": detail,
                },
            )
        if proc.returncode & _EXIT_PARTIAL:
            raise MangaError(
                "gallery-dl 部分文件下载失败（退出码含位 4）：产物已写出，但不完整",
                code="PARTIAL_DOWNLOAD",
                exit_code=EXIT_ERROR,
                hint=(
                    "缺页的文件："
                    + ("、".join(failed[:10]) or "（见 stderr）")
                    + "。**重跑同一条命令即可续传**（已下的不会重来）"
                ),
                data={
                    "dest": str(dest),
                    "urls": url_list,
                    "exit_code": proc.returncode,
                    "files": produced,
                    "failed_files": failed,
                    "stderr": detail,
                },
            )
        raise MangaError(
            f"gallery-dl 退出码 {proc.returncode}（产物已写出，但过程有错误）",
            code="DOWNLOAD_FAILED",
            exit_code=EXIT_ERROR,
            hint=detail or None,
            data={
                "dest": str(dest),
                "urls": url_list,
                "exit_code": proc.returncode,
                "files": produced,
                "cwd": str(workdir),
            },
        )

    return {
        "dest": str(dest),
        "urls": url_list,
        "count": len(produced),
        "file_count": len(produced),
        "files": produced,
        "new_files": new_files,
        "new_count": len(new_files),
        "changed_files": changed_files,
        "exit_code": proc.returncode,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }
