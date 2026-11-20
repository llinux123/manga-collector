"""路径与配置解析。

本项目（manga-collector）只负责**采集侧**。书库侧的流水线脚本
（``Test-MangaLibrary.ps1`` / ``Invoke-LibraryIngest.ps1`` / ``Export-MangaLibrary.ps1`` /
``Initialize-MangaLayout.ps1``）与 jmcomic 的转换脚本（``Convert-JmComicToCbz.ps1``）
**属于 manga 工作区**，本项目通过 ``--scripts-dir`` / ``MANGA_WORKSPACE`` 引用它们，
不复制、不改写 —— 单一事实来源仍在 manga 工作区。

书库路径口径与 manga 工作区一致（见其 README「架构一览」）：

```
<LibraryRoot>\\
├── 00-Incoming\\Manga\\        # 漫画收件箱（待整理）
├── 00-Incoming\\_raw_jmcomic\\ # jmcomic 下载暂存（不参与 Syncthing）
├── Manga\\Library\\            # 权威库：一话一个 CBZ + 根目录 ComicInfo.xml
├── Manga\\Metadata\\           # 校验报告与操作日志
└── Export\\Manga\\             # 干净导出 → Syncthing 共享给手机（唯一共享区）
```

覆盖优先级：命令行参数 > 环境变量 > 探测到的默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .envelope import MangaError

ENV_LIBRARY_ROOT = "MANGA_LIBRARY_ROOT"
ENV_SCRIPTS_DIR = "MANGA_SCRIPTS_DIR"
ENV_WORKSPACE = "MANGA_WORKSPACE"
ENV_OPTION_FILE = "MANGA_OPTION_FILE"
#: gallery-dl 配置文件（覆盖默认的「入库合规命名」模板）
ENV_GALLERY_DL_CONFIG = "MANGA_GALLERY_DL_CONFIG"
#: 来源层目录名（如「禁漫 (JMComic)」）—— 库内「来源名/书名」两层结构的来源名
ENV_SOURCE_PREFIX = "MANGA_SOURCE_PREFIX"

#: 默认来源层：禁漫（灰色来源，产物只进权威库，绝不进共享区）
DEFAULT_SOURCE_PREFIX = "禁漫 (JMComic)"

#: 本机默认书库根目录
DEFAULT_LIBRARY_ROOT = Path.home() / "Documents" / "Library"

#: 本项目根目录（src/manga/config.py → 上溯三层）
REPO_ROOT = Path(__file__).resolve().parents[2]

#: manga 工作区里用来判定「这就是那个工作区」的标志文件
_WORKSPACE_MARKER = Path("scripts") / "Test-MangaLibrary.ps1"


def _looks_like_workspace(path: Path) -> bool:
    return (path / _WORKSPACE_MARKER).is_file()


def find_workspace() -> Path | None:
    """定位 manga 工作区（脚本与 jmcomic 配置的宿主）。

    探测顺序：

    1. 环境变量 ``MANGA_WORKSPACE``；
    2. 本项目的同级目录 ``../manga``；
    3. 历史默认位置 ``~/Downloads/dsh/manga``。

    找不到时返回 ``None``（调用方给出可操作的错误提示）。
    """
    env = os.environ.get(ENV_WORKSPACE)
    if env:
        candidate = Path(env).expanduser()
        return candidate if _looks_like_workspace(candidate) else None

    for candidate in (REPO_ROOT.parent / "manga", Path.home() / "Downloads" / "dsh" / "manga"):
        if _looks_like_workspace(candidate):
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class Paths:
    """一组解析好的路径。"""

    library_root: Path
    scripts_dir: Path
    profile_dir: Path
    workspace: Path | None
    option_file: Path
    source_prefix: str = DEFAULT_SOURCE_PREFIX

    # ── 派生路径 ────────────────────────────────────────────────────────
    @property
    def inbox(self) -> Path:
        """漫画收件箱：所有下载器统一落点。"""
        return self.library_root / "00-Incoming" / "Manga"

    @property
    def raw_jmcomic(self) -> Path:
        """jmcomic 散图暂存区（在收件箱旁边，不参与 Syncthing）。"""
        return self.library_root / "00-Incoming" / "_raw_jmcomic"

    @property
    def library(self) -> Path:
        """权威库：唯一事实来源。"""
        return self.library_root / "Manga" / "Library"

    @property
    def metadata(self) -> Path:
        """工具产物：校验报告、操作日志。"""
        return self.library_root / "Manga" / "Metadata"

    @property
    def logs(self) -> Path:
        """运行日志目录：jmcomic 的进度日志固定落在这里。

        jmcomic 把进度日志写成**相对路径**（``jmcomic-download.log``），
        落点就是进程工作目录 → 调 jmcomic 之前先切到本目录，
        日志就不会掉进仓库根。
        """
        return self.metadata / "logs"

    @property
    def export(self) -> Path:
        """干净导出目录 → Syncthing 共享给手机。

        ⚠ 灰色来源（禁漫等未授权内容）**禁止**进入此目录。
        """
        return self.library_root / "Export" / "Manga"

    @property
    def archive(self) -> Path:
        return self.library_root / "Archive" / "Manga"

    @property
    def backups(self) -> Path:
        return self.library_root / "Backups" / "Manga"

    @property
    def jobs(self) -> Path:
        """长任务作业目录：``manga job start`` 的状态、进度、日志都落在这里。

        作业状态是**临时**的 —— 书库不可写时会退到系统临时目录（见
        :func:`manga.jobs.JobStore.create`），不该因为落点不可写就整个失败。
        """
        return self.metadata / "jobs"

    # ── 脚本 ────────────────────────────────────────────────────────────
    def script(self, name: str) -> Path:
        """定位 PowerShell 脚本：先本项目，再 manga 工作区。"""
        found = self.find_script(name)
        if found is not None:
            return found
        searched = "、".join(str(d / name) for d in self._script_dirs())
        raise MangaError(
            f"找不到脚本：{name}",
            code="SCRIPT_MISSING",
            exit_code=4,
            hint=(
                f"已搜索：{searched}。"
                f"用 --scripts-dir 指定，或设环境变量 {ENV_SCRIPTS_DIR}；"
                f"若脚本在 manga 工作区，设 {ENV_WORKSPACE} 指向它"
            ),
        )

    def find_script(self, name: str) -> Path | None:
        """按顺序找脚本；找不到返回 ``None``。"""
        for directory in self._script_dirs():
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None

    def has_script(self, name: str) -> bool:
        """脚本是否存在（体检用，不抛异常）。"""
        return self.find_script(name) is not None

    def _script_dirs(self) -> tuple[Path, ...]:
        dirs: list[Path] = [self.scripts_dir]
        if self.workspace is not None:
            dirs.append(self.workspace / "jmcomic")  # Convert-JmComicToCbz.ps1 在这
        dirs.append(REPO_ROOT / "scripts")
        return tuple(dirs)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "library_root": str(self.library_root),
            "inbox": str(self.inbox),
            "raw_jmcomic": str(self.raw_jmcomic),
            "library": str(self.library),
            "metadata": str(self.metadata),
            "logs": str(self.logs),
            "export": str(self.export),
            "jobs": str(self.jobs),
            "scripts_dir": str(self.scripts_dir),
            "profile_dir": str(self.profile_dir),
            "workspace": str(self.workspace) if self.workspace else None,
            "option_file": str(self.option_file),
            "source_prefix": self.source_prefix,
        }


def default_scripts_dir(workspace: Path | None) -> Path:
    """默认脚本目录：manga 工作区的 ``scripts/``；找不到就退到本项目。"""
    if workspace is not None:
        return workspace / "scripts"
    return REPO_ROOT / "scripts"


def default_profile_dir() -> Path:
    """profile 目录：``%APPDATA%\\manga\\profiles``。"""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / "manga" / "profiles"


def default_option_file(workspace: Path | None) -> Path:
    """默认 option 文件：优先本项目自带的模板，其次 manga 工作区的实机配置。"""
    env = os.environ.get(ENV_OPTION_FILE)
    if env:
        return Path(env).expanduser()
    bundled = REPO_ROOT / "config" / "jmcomic-option.yml"
    if bundled.is_file():
        return bundled
    if workspace is not None:
        return workspace / "jmcomic" / "option.yml"
    return bundled


def default_gallery_dl_config() -> Path:
    """默认 gallery-dl 配置：随包分发的「入库合规命名」模板。

    gallery-dl 的默认目录格式产出 ``v01 c001`` 这类名字，**过不了** manga
    工作区的入库闸门（命名规范要求 ``<话号> - <标题>.cbz``）。
    本文件把各站的 ``directory``/``filename`` 模板改成合规形态。

    生成与校验：``uv run python tools/gallery_dl_facts.py config|check``。
    """
    env = os.environ.get(ENV_GALLERY_DL_CONFIG)
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent / "data" / "gallery-dl-option.json"


def resolve_paths(
    library_root: str | Path | None = None,
    scripts_dir: str | Path | None = None,
    profile_dir: str | Path | None = None,
    option_file: str | Path | None = None,
    source_prefix: str | None = None,
) -> Paths:
    """按「参数 > 环境变量 > 探测到的默认值」的优先级解析路径。"""

    def pick(explicit: str | Path | None, env_name: str, default: Path) -> Path:
        if explicit:
            return Path(explicit).expanduser()
        env = os.environ.get(env_name)
        if env:
            return Path(env).expanduser()
        return default

    workspace = find_workspace()
    root = pick(library_root, ENV_LIBRARY_ROOT, DEFAULT_LIBRARY_ROOT)
    return Paths(
        library_root=root.resolve() if root.exists() else root,
        scripts_dir=pick(scripts_dir, ENV_SCRIPTS_DIR, default_scripts_dir(workspace)).resolve(),
        profile_dir=pick(profile_dir, "", default_profile_dir()).resolve(),
        workspace=workspace,
        option_file=pick(option_file, "", default_option_file(workspace)),
        source_prefix=(
            source_prefix
            if source_prefix is not None
            else (os.environ.get(ENV_SOURCE_PREFIX) or DEFAULT_SOURCE_PREFIX)
        ),
    )
