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
import threading
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
        """失败 = 非 0/1 的退出码。

        PS 约定 2 = 存在错误；但脚本异常终止、被终止信号杀死、命令不存在
        会以其它码退出 —— 这些一律判失败，绝不能包装成成功信封（P1 修复）。
        exit 1 是否真为「告警」由信封层结合输出判别（见
        ``cli._script_result_to_envelope``）：没有任何已知汇总块且 stderr
        非空时视为异常终止。
        """
        return self.exit_code not in (PS_EXIT_OK, PS_EXIT_WARN)

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


#: 脚本调用默认超时（秒）。三个真实负载（verify 全库扫描 / ingest 移动大文件 /
#: convert-cbz 批量打包）在慢盘上都可能跑几分钟，这个值只为「真挂死」兜底；
#: 调用方可显式覆盖，传 ``None`` 表示不限时。
DEFAULT_SCRIPT_TIMEOUT: float = 3600.0

#: 解析报告里明细清单的最大条数 —— 信封要进 agent 的模型上下文，明细必须截断
#: （与 verify 的「问题列表前 N 条」口径一致）。
REPORT_DETAIL_CAP = 20

# ── 活动脚本子进程注册表（取消用）─────────────────────────────────────────
# ``run_script`` 用 Popen 起脚本进程；「取消」发生在**另一个线程**
# （cli._start_cancel_watchdog → _graceful_cancel）。必须能从那里摸到进程
# 句柄，否则 CLI 被 os._exit 后 pwsh 变孤儿——继续写包、继续执行
# Remove-Item 删源图，而作业状态已经是 cancelled（状态分叉）。
_PROC_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set = set()


def _register_process(proc: subprocess.Popen) -> None:
    with _PROC_LOCK:
        _ACTIVE_PROCESSES.add(proc)


def _unregister_process(proc: subprocess.Popen) -> None:
    with _PROC_LOCK:
        _ACTIVE_PROCESSES.discard(proc)


def kill_active() -> int:
    """杀掉所有仍在运行的脚本子进程（取消看门狗用），返回杀掉的个数。

    进程是本进程创建的，``Popen.kill`` 不需要特权。杀掉后主线程的
    ``communicate()`` 立刻返回（退出码是终止码，不再是 0/1/2 语义）——
    取消流程随后会 ``os._exit``，这个返回值不会被当正常结果使用。
    脚本侧必须能承受「中途被杀」：工作区脚本写 CBZ 用 tmp + 原子落位。
    """
    with _PROC_LOCK:
        procs = [p for p in _ACTIVE_PROCESSES if p.poll() is None]
    killed = 0
    for proc in procs:
        try:
            proc.kill()
            killed += 1
        except OSError:
            continue
    return killed


def run_script(
    script: Path,
    args: Sequence[str] = (),
    *,
    cwd: Path | None = None,
    timeout: float | None = DEFAULT_SCRIPT_TIMEOUT,
) -> ScriptResult:
    """调用一个 PowerShell 脚本并捕获结果。

    只传 ``-File`` 之后的位置参数，**不拼接成一条命令字符串**，避免注入与转义问题。

    ``timeout`` 默认 :data:`DEFAULT_SCRIPT_TIMEOUT`（防挂死兜底），显式传
    ``None`` 不限时。超时 → 杀掉子进程并抛 ``TIMEOUT``。进程句柄全程注册在
    活动表里，取消看门狗（:func:`kill_active`）随时能杀掉它。
    """
    exe = _powershell_exe()
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *[str(a) for a in args]]
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv 为列表，不走 shell
            argv,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise MangaError(
            f"无法启动脚本进程：{script.name}（{exc}）",
            code="PS_START_FAILED",
            exit_code=EXIT_ERROR,
            hint="确认 pwsh / powershell 可用（manga doctor）",
        ) from exc

    _register_process(proc)
    try:
        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_raw, stderr_raw = proc.communicate()
            raise MangaError(
                f"脚本超时（{timeout}s）：{script.name}",
                code="TIMEOUT",
                exit_code=EXIT_ERROR,
                hint="已杀掉脚本进程；若在写文件，检查落点是否留下 .tmp 残件",
            )
    finally:
        _unregister_process(proc)

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
        stdout=decode(stdout_raw),
        stderr=decode(stderr_raw),
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


# ── CBZ 打包报告解析（Convert-JmComicToCbz.ps1）──────────────────────────
#: ``[打包] Some Album\001 - X.cbz   （12 张图，源：第01話）``
_CBZ_PACKED_RE = re.compile(
    r"^\[打包\]\s*(?P<series>[^\\\r\n]+?)\\(?P<file>.+?)\s{2,}（(?P<images>\d+)\s*张图，源：(?P<source>.+)）$"
)
#: ``[跳过] 已存在同名包，不覆盖： 001 - X.cbz``
_CBZ_SKIP_RE = re.compile(r"^\[跳过\]\s*已存在同名包，不覆盖：\s*(?P<file>.+?)$")
#: ``[错误] 001 - X.cbz：<原因>``（文件名可含「：」，不拆字段，整行保留）
_CBZ_ERROR_RE = re.compile(r"^\[错误\]\s*(?P<rest>.+?)$")
#: ``汇总：计划/成功 3，跳过 1，错误 0。目标目录：D:\...\Manga``
_CBZ_SUMMARY_RE = re.compile(
    r"^汇总：计划/成功\s*(?P<ok>\d+)\s*，\s*跳过\s*(?P<skip>\d+)\s*，\s*错误\s*(?P<err>\d+)\s*。目标目录：(?P<target>.*)$"
)
#: ``源目录下没有找到图片目录…`` → 零工作量也 exit 0，必须显式标出
_CBZ_NOTHING_MARK = "源目录下没有找到图片目录"


def parse_cbz_report(stdout: str) -> dict[str, Any]:
    """解析 ``Convert-JmComicToCbz.ps1`` 的输出 → 结构化报告。

    字段命名刻意诚实：PS 对 dry-run 的计划项也 ``$ok++``，所以
    ``planned_or_succeeded`` 在 dry-run 时是**计划数**、``--apply`` 时是成功数。
    明细截断到 :data:`REPORT_DETAIL_CAP` 条；``parsed=False`` 表示输出认不出
    （旧版工作区脚本）——调用方应回退到「列目录核对」。
    """
    packed: list[dict[str, Any]] = []
    skipped_files: list[str] = []
    error_lines: list[str] = []
    packed_total = skipped_total = error_total = 0
    counts: dict[str, int] | None = None
    target: str | None = None
    nothing_to_do = False

    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _CBZ_NOTHING_MARK in line:
            nothing_to_do = True
            continue
        m = _CBZ_SUMMARY_RE.match(line)
        if m:
            counts = {
                "planned_or_succeeded": int(m.group("ok")),
                "skipped": int(m.group("skip")),
                "errors": int(m.group("err")),
            }
            target = m.group("target").strip() or None
            continue
        m = _CBZ_PACKED_RE.match(line)
        if m:
            packed_total += 1
            if len(packed) < REPORT_DETAIL_CAP:
                packed.append(
                    {
                        "series": m.group("series"),
                        "file": m.group("file"),
                        "images": int(m.group("images")),
                        "source": m.group("source"),
                    }
                )
            continue
        m = _CBZ_SKIP_RE.match(line)
        if m:
            skipped_total += 1
            if len(skipped_files) < REPORT_DETAIL_CAP:
                skipped_files.append(m.group("file"))
            continue
        m = _CBZ_ERROR_RE.match(line)
        if m:
            error_total += 1
            if len(error_lines) < REPORT_DETAIL_CAP:
                error_lines.append(m.group("rest"))
            continue

    report: dict[str, Any] = {
        "parsed": bool(counts or packed_total or skipped_total or error_total or nothing_to_do),
        "nothing_to_do": nothing_to_do,
        "truncated": packed_total > len(packed)
        or skipped_total > len(skipped_files)
        or error_total > len(error_lines),
        "packed": packed,
        "skipped_files": skipped_files,
        "error_lines": error_lines,
    }
    if counts:
        report.update(counts)
    if target:
        report["target_root"] = target
    return report


# ── 入库报告解析（Invoke-LibraryIngest.ps1）──────────────────────────────
#: ``[  1] 入库 来源/书名/001 - X.cbz``（tag：入库 / 冲突 / 跳过）
_INGEST_PLAN_RE = re.compile(r"^\[\s*\d+\]\s*(?P<tag>入库|冲突|跳过)\s+(?P<src>.+?)$")
#: ``      原因: ...`` / ``      备注   : ...`` —— 挂到最近一条计划上
_INGEST_NOTE_RE = re.compile(r"^\s*(?:原因|备注)\s*[:：]\s*(?P<note>.+?)$")
#: 汇总块标签 → 稳定字段名（dry-run 没有 实际入库/写入元数据/错误/日志 四行）
_INGEST_SUMMARY_KEYS = {
    "扫描文件": "scanned",
    "待入库": "planned_moves",
    "冲突跳过": "conflicts",
    "跳过(损坏)": "skipped_corrupt",
    "实际入库": "moved",
    "写入元数据": "comicinfo_written",
    "错误": "errors",
    "日志": "log_path",
}
_INGEST_NOTHING_MARK = "没有找到可处理的 CBZ"


def parse_ingest_report(stdout: str) -> dict[str, Any]:
    """解析 ``Invoke-LibraryIngest.ps1`` 的输出 → 结构化报告。

    dry-run 时 ``moved`` / ``comicinfo_written`` / ``errors`` / ``log_path``
    不会出现（脚本只在 ``-Apply`` 下打印）。计划明细截断到
    :data:`REPORT_DETAIL_CAP` 条；``parsed=False`` 表示输出认不出。
    """
    summary: dict[str, Any] = {}
    plans: list[dict[str, Any]] = []
    plans_total = 0
    nothing_to_do = False

    for raw_line in (stdout or "").splitlines():
        if not raw_line.strip():
            continue
        if _INGEST_NOTHING_MARK in raw_line:
            nothing_to_do = True
            continue
        m = _INGEST_PLAN_RE.match(raw_line.strip())
        if m:
            plans_total += 1
            if len(plans) < REPORT_DETAIL_CAP:
                plans.append({"tag": m.group("tag"), "source": m.group("src")})
            continue
        m = _INGEST_NOTE_RE.match(raw_line.rstrip())
        if m:
            if plans and plans[-1].get("note") is None:
                plans[-1]["note"] = m.group("note")
            continue
        m = _PCT_RE.match(raw_line.strip())
        if m:
            key = _INGEST_SUMMARY_KEYS.get(m.group("label").strip())
            if key:
                summary[key] = _coerce(m.group("value").strip())

    return {
        "parsed": bool(summary or plans_total or nothing_to_do),
        "nothing_to_do": nothing_to_do,
        "truncated": plans_total > len(plans),
        "plans": plans,
        **summary,
    }


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
