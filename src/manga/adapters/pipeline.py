"""既有 PowerShell 脚本的编排层。

这一层只做三件事：

1. **调用**：用 ``pwsh -NoProfile -File`` 跑仓库里的 4 个脚本，捕获退出码；
2. **翻译**：把 PS 的退出码与人类可读输出，翻译成机器可读的结构（退出码 0/1/2 → 语义）；
3. **守门**：红线——灰色来源不得进入 ``Export\\Manga``（Syncthing 共享区）。

退出码语义（与 scripts/README.md 一致）：

===== =====================================================
PS    含义
===== =====================================================
0     成功
1     有冲突 / 跳过，或仅有告警（**不是错误**）
2     存在错误
===== =====================================================
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..envelope import EXIT_ERROR, EXIT_OK, MangaError, RedLineViolation

#: PowerShell 脚本的退出码语义（与 scripts/README.md 一致）
PS_EXIT_OK = 0      # 成功
PS_EXIT_WARN = 1    # 有冲突 / 跳过，或仅有告警（**不是错误**）
PS_EXIT_ERROR = 2   # 存在错误

# ── 脚本名 ────────────────────────────────────────────────────────────────
SCRIPT_VERIFY = "Test-MangaLibrary.ps1"
SCRIPT_INGEST = "Invoke-LibraryIngest.ps1"
SCRIPT_LAYOUT = "Initialize-MangaLayout.ps1"
SCRIPT_EXPORT = "Export-MangaLibrary.ps1"
SCRIPT_CBZ = "Convert-JmComicToCbz.ps1"

#: 红线：这些脚本绝不能作用于灰色来源的产物
_FORBIDDEN_FOR_GREY = (SCRIPT_EXPORT,)

# ── 报告解析 ──────────────────────────────────────────────────────────────
#: ``命名合规率 : 90.9%（10/11）``
_PCT_RE = re.compile(r"^(?P<label>[^:：]+)\s*[:：]\s*(?P<value>.+?)\s*$")
#: ``[Error  ] NumberMismatch Mismatch Series\002 - X.cbz``
_ISSUE_RE = re.compile(r"^\[(?P<sev>Error|Warning|错误|告警)\s*\]\s*(?P<cat>\S+)\s*(?P<msg>.*)$")

#: 已知汇总项 → 稳定字段名
_SUMMARY_KEYS = {
    "库根目录": "library_root",
    "系列目录数": "series_count",
    "扫描文件数": "scanned",
    "命名合规率": "naming_compliance",
    "comicinfo 覆盖率": "comicinfo_coverage",
    "cbz 可读率": "cbz_readable",
    "编号一致性": "number_consistency",
    "页数达标率": "page_count_ok",
    "空卷/残卷": "empty_or_broken",
    "重复入库": "duplicates",
    "分卷提示": "splits",
    "宽度不一致": "width_mismatch",
    "同名文件": "duplicate_names",
    "问题统计": "issue_counts",
}


@dataclass(slots=True)
class ScriptResult:
    """一次脚本调用的结果。"""

    script: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == PS_EXIT_OK

    @property
    def warned(self) -> bool:
        """PS 退出码 1 = 有冲突或告警，但**不是错误**。"""
        return self.exit_code == PS_EXIT_WARN

    @property
    def failed(self) -> bool:
        return self.exit_code == PS_EXIT_ERROR

    def as_dict(self) -> dict[str, Any]:
        return {
            "script": self.script,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr.strip() or None,
        }


@dataclass(slots=True)
class Issue:
    """校验器的一条问题。"""

    severity: str
    category: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "category": self.category, "message": self.message}


def _powershell_exe() -> str:
    """优先 PowerShell 7；回退 Windows PowerShell 5.1。"""
    for exe in ("pwsh", "powershell"):
        if shutil.which(exe):
            return exe
    raise MangaError(
        "找不到 PowerShell（pwsh / powershell）",
        code="PS_MISSING",
        exit_code=4,
        hint="安装 PowerShell 7（pwsh），或确保 Windows PowerShell 5.1 可用",
    )


def run_script(
    script: Path,
    args: Sequence[str] = (),
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> ScriptResult:
    """调用一个 PowerShell 脚本并捕获结果。

    只传 ``-File`` 之后的位置参数，**不拼接成一条命令字符串**，避免注入与转义问题。
    """
    exe = _powershell_exe()
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *[str(a) for a in args]]
    try:
        proc = subprocess.run(  # noqa: S603 - argv 为列表，不走 shell
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MangaError(
            f"脚本超时（{timeout}s）：{script.name}",
            code="TIMEOUT",
            exit_code=EXIT_ERROR,
        ) from exc

    def decode(raw: bytes) -> str:
        for enc in ("utf-8", "gbk", "cp1252"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    return ScriptResult(
        script=script.name,
        exit_code=proc.returncode,
        stdout=decode(proc.stdout),
        stderr=decode(proc.stderr),
    )


def parse_verify_report(stdout: str) -> dict[str, Any]:
    """解析 ``Test-MangaLibrary.ps1`` 的汇总块与问题清单。

    格式来自 scripts/README.md §「输出示例」：

    ```
    ==================== 漫画库校验报告 ====================
    命名合规率 : 90.9%（10/11）
    问题统计   : 错误 6 个 / 告警 4 个
    ---- 问题列表（前 10 条，共 10 条）----
    [Error  ] NumberMismatch Mismatch Series\\002 - X.cbz
    ```
    """
    summary: dict[str, Any] = {}
    issues: list[Issue] = []
    in_issues = False

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        if "问题列表" in line:
            in_issues = True
            continue
        if in_issues:
            m = _ISSUE_RE.match(line.strip())
            if m:
                sev = m.group("sev")
                issues.append(
                    Issue(
                        severity="error" if sev.lower().startswith(("error", "错误")) else "warning",
                        category=m.group("cat"),
                        message=m.group("msg").strip(),
                    )
                )
                continue
            # 续行（缩进的详细说明）→ 并入上一条的 message
            if issues and line.startswith((" ", "\t")) and line.strip():
                issues[-1].message += " " + line.strip()
                continue

        m = _PCT_RE.match(line.strip())
        if not m:
            continue
        label = m.group("label").strip()
        value = m.group("value").strip()
        key = _SUMMARY_KEYS.get(label.lower())
        if key is None:
            continue
        summary[key] = _coerce(value)

    summary["issues"] = [i.as_dict() for i in issues]
    return summary


def _coerce(value: str) -> Any:
    """把汇总项的值转成结构化形式。

    覆盖校验器的实际输出形态：

    * ``97.4%（754/774）``          → ``{"percent": 97.4, "detail": "754/774"}``
    * ``n/a（0/0 文件名编号 == ...）`` → ``{"percent": None, "detail": "0/0 ..."}``
    * ``18（零字节 / 无图片页 / ...）`` → ``{"count": 18, "detail": "..."}``
    * ``0 组（Number+Volume 组合键）`` → ``{"count": 0, "detail": "..."}``
    * ``3``                          → ``3``
    """
    pct = re.match(r"^(?P<pct>[\d.]+)%(?:\s*[（(](?P<part>[^）)]*)[）)])?$", value)
    if pct:
        out: dict[str, Any] = {"percent": float(pct.group("pct"))}
        if pct.group("part"):
            out["detail"] = pct.group("part")
        return out

    na = re.match(r"^n/a\s*[（(](?P<part>[^）)]*)[）)]$", value)
    if na:
        return {"percent": None, "detail": na.group("part")}

    cnt = re.match(r"^(?P<n>-?\d+)\s*(?:个|组|个系列)?\s*[（(](?P<part>[^）)]*)[）)]$", value)
    if cnt:
        return {"count": int(cnt.group("n")), "detail": cnt.group("part")}

    num = re.match(r"^(-?\d+)\s*(?:个|组|个系列)?$", value)
    if num:
        return int(num.group(1))

    return value


# ── 红线闸门 ──────────────────────────────────────────────────────────────


def snapshot_dir(root: Path) -> dict[str, tuple[int, int]]:
    """快照目录内容：``相对路径 -> (文件大小, mtime_ns)``。

    目录不存在时返回空字典（不报错——共享区可能还没建）。
    """
    if not root.is_dir():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            stat = p.stat()
            out[str(p.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return out


def diff_snapshots(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> dict[str, list[str]]:
    """比较两次快照，列出新增 / 删除 / 变更。"""
    before_keys, after_keys = set(before), set(after)
    return {
        "added": sorted(after_keys - before_keys),
        "removed": sorted(before_keys - after_keys),
        "changed": sorted(k for k in before_keys & after_keys if before[k] != after[k]),
    }


@dataclass(slots=True)
class ExportGuard:
    """红线守卫：确保 ``Export\\Manga``（Syncthing 共享区）不被灰色来源污染。

    用法::

        with ExportGuard(paths.export) as guard:
            ...执行入库...
        guard.assert_unchanged()   # 或由 __exit__ 自动检查
    """

    export_root: Path
    _before: dict[str, tuple[int, int]] = field(default_factory=dict)
    _armed: bool = False

    def __enter__(self) -> ExportGuard:
        self._before = snapshot_dir(self.export_root)
        self._armed = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            # 已经出错时不再叠加红线错误，保留原始异常
            return False
        self.assert_unchanged()
        return False

    def assert_unchanged(self) -> None:
        """共享区出现新增或丢失文件 → 红线违规。

        **只改内容（changed）不算违规**：红线管的是「灰色来源的产物不得进入共享区」，
        即「有没有新文件进来」；共享区里已有文件被改动是另一类问题，只作提示。
        """
        if not self._armed:
            return
        after = snapshot_dir(self.export_root)
        diff = diff_snapshots(self._before, after)
        if diff["added"]:
            raise RedLineViolation(
                "红线：灰色来源的产物不得进入 Syncthing 共享区 Export\\Manga",
                data={
                    "export_root": str(self.export_root),
                    "added_files": diff["added"][:50],
                    "added_count": len(diff["added"]),
                },
            )
        if diff["removed"]:
            raise RedLineViolation(
                "红线：共享区 Export\\Manga 出现文件丢失",
                data={
                    "export_root": str(self.export_root),
                    "removed_files": diff["removed"][:50],
                    "removed_count": len(diff["removed"]),
                },
            )


def assert_not_grey_source_export(script_name: str) -> None:
    """禁止把导出脚本用于灰色来源。"""
    if script_name in _FORBIDDEN_FOR_GREY:
        raise RedLineViolation(
            f"红线：禁止对灰色来源调用 {script_name}（Export\\Manga 是 Syncthing 共享区）",
            data={"script": script_name},
        )
