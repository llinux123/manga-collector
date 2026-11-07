"""``manga`` 命令行入口。

**契约（agent 只依赖这一层）**：

* stdout 永远只有一个 JSON 信封：``{"ok", "data", "error", "warnings", "exit"}``；
* 退出码固定：0 成功（可能带 warnings）/ 1 用法错 / 2 执行错 / 4 资源不存在 / 6 红线拒绝；
* 写操作默认 dry-run，必须显式 --apply 才落盘；
* **红线**：灰色来源（禁漫等未授权内容）的产物不得进入 ``Export\\Manga``（Syncthing 共享区）。
  守卫在代码里执行（``adapters.pipeline.ExportGuard``），不依赖调用方自觉。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import typer

from . import __version__
from .adapters import jmcomic as jm_adapter
from .adapters import pipeline
from .config import ENV_WORKSPACE, Paths, resolve_paths
from .envelope import (
    EXIT_ERROR,
    EXIT_NOTFOUND,
    EXIT_OK,
    EXIT_USAGE,
    Envelope,
    MangaError,
    RedLineViolation,
    emit,
)
from . import runtime

app = typer.Typer(
    name="manga",
    help="漫画采集聚合层 · 薄 CLI（Skill 是界面，CLI 是手，agent 是大脑）",
    no_args_is_help=True,
    add_completion=False,
)

# ── 公共选项 ──────────────────────────────────────────────────────────────


def _paths(library_root: str | None, scripts_dir: str | None) -> Paths:
    return resolve_paths(library_root=library_root, scripts_dir=scripts_dir)


def _timestamp() -> str:
    """文件名用的时间戳：``20261107-160653``。"""
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _run(command: str, fn) -> None:
    """执行命令并统一处理异常 → 信封。"""
    try:
        # stdout 只允许出现信封：命令体里任何意外的 print 都改道 stderr
        with contextlib.redirect_stdout(sys.stderr):
            env = fn()
    except MangaError as exc:
        raise typer.Exit(code=emit(exc.to_envelope())) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底：不让异常裸奔到 agent
        env = Envelope(
            ok=False,
            error={"code": "UNEXPECTED", "message": f"{type(exc).__name__}: {exc}", "hint": None},
            exit=EXIT_ERROR,
        )
        raise typer.Exit(code=emit(env)) from exc
    raise typer.Exit(code=emit(env))


def _stage_series_for_verify(p: Paths, series: list[str]) -> Path:
    """把选中的系列**复制**进一个临时「影子库根」，还原成校验脚本认的两层结构。

    校验脚本只认「库根/系列/文件.cbz」：把库根直接指向系列目录，文件就落在
    库根下（深度 0）→ 被判成「文件直接位于库根目录」→ 退出码 2。
    复制一份再验，原库一个字节都不动；验完由调用方删掉。

    ``series`` 可以是顶层目录名（``18comic``），也可以是相对子路径
    （``18comic/繼母與繼姐``）—— 后者会被摆到影子库根的**第一层**，
    深度回到 1，不会触发「目录层级过深」告警。

    影子库根优先放 ``Manga\\Metadata`` 下（与权威库同一卷，复制最快），
    建不出来就退到系统临时目录 —— 用**普通 mkdir** 而不是 ``mkdtemp``：
    后者在 Windows 上会套 0o700 权限，受限进程随后读不了。
    """
    stamp = _timestamp()
    candidates = [p.metadata / f".verify-scope-{stamp}", Path(tempfile.gettempdir()) / f"manga-verify-scope-{stamp}"]

    stage: Path | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".writable"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            stage = candidate
            break
        except OSError:
            continue

    if stage is None:
        raise MangaError(
            "无法创建临时影子库根（增量校验需要一个可写目录）",
            code="STAGE_FAILED",
            exit_code=EXIT_ERROR,
            hint="检查书库目录权限，或设 TEMP 到一个可写目录后重试",
        )

    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    for name in series:
        src = p.library / name
        # 该目录自己就装着 CBZ → 整个搬过去；
        # 否则它是「来源层」（如 18comic），里面还套着系列 → 只搬叶子系列，
        # 让每个叶子系列在影子库根里都是第一层，深度恒为 1。
        leaves = _leaf_series_dirs(src)
        if leaves:
            for leaf in leaves:
                shutil.copytree(leaf, stage / leaf.name)
        else:
            shutil.copytree(src, stage / Path(name).name)
    return stage


def _leaf_series_dirs(root: Path) -> list[Path]:
    """``root`` 下**直接装着 CBZ** 的子目录（没有则空列表）。"""
    if not root.is_dir():
        return []
    leaves = [child for child in sorted(root.iterdir()) if child.is_dir() and any(child.glob("*.cbz"))]
    return leaves


def _series_with_files(library: Path) -> list[str]:
    """库里所有**直接装着 CBZ** 的目录（相对路径），供 agent 选。"""
    if not library.is_dir():
        return []
    found: set[str] = set()
    for cbz in library.rglob("*.cbz"):
        parent = cbz.parent
        try:
            rel = parent.relative_to(library)
        except ValueError:  # pragma: no cover - 不该发生
            continue
        found.add(str(rel) if str(rel) != "." else parent.name)
    return sorted(found)


def _script_output_tail(result: pipeline.ScriptResult, limit: int = 1200) -> str:
    """脚本输出的尾部（给 agent 看真实原因，别只丢一句「有告警」）。"""
    text = (result.stdout or "").strip() or (result.stderr or "").strip()
    if not text:
        return ""
    return text[-limit:]


def _script_result_to_envelope(
    result: pipeline.ScriptResult,
    *,
    data: dict[str, Any] | None = None,
    extra_warnings: list[str] | None = None,
) -> Envelope:
    """把 PS 脚本结果翻译成信封：0 → ok；1 → ok + warnings；2 → 错误。"""
    warnings = list(extra_warnings or [])
    if result.warned:
        warnings.append(f"{result.script} 退出码 1：有冲突或告警（不是错误）")
        tail = _script_output_tail(result)
        if tail:
            # 退出码 1 常常是「真的没干成活」（权限/路径），把原文给 agent 看
            warnings.append(f"{result.script} 输出尾部：{tail}")
    payload: dict[str, Any] = {"script": result.script, "exit_code": result.exit_code}
    payload.update(data or {})
    if result.failed:
        return Envelope(
            ok=False,
            data=payload,
            error={
                "code": "SCRIPT_ERROR",
                "message": f"{result.script} 退出码 {result.exit_code}",
                "hint": (result.stdout or result.stderr).strip()[-2000:] or None,
            },
            warnings=warnings,
            exit=EXIT_ERROR,
        )
    return Envelope(ok=True, data=payload, warnings=warnings, exit=EXIT_OK)


# ── 命令 ──────────────────────────────────────────────────────────────────


@app.command()
def paths(
    library_root: str | None = typer.Option(None, "--library-root", help="书库根目录（默认 %USERPROFILE%\\Documents\\Library）"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir", help="scripts/ 目录"),
) -> None:
    """打印解析后的所有路径（agent 用它确认落点）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        return Envelope(ok=True, data={"paths": p.as_dict()})

    _run("paths", run)


@app.command()
def doctor(
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """环境体检：PowerShell、脚本、jmcomic（导入 + 版本下限）、登录态、可选依赖、代理端口、目录状态。

    只读体检：不联网、不安装任何东西、不索要凭据。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        checks: list[dict[str, Any]] = []
        warnings: list[str] = []

        # 1) PowerShell
        ps = shutil.which("pwsh") or shutil.which("powershell")
        checks.append({"check": "powershell", "ok": bool(ps), "detail": ps or "未找到 pwsh / powershell"})

        # 2) 脚本
        for name in (pipeline.SCRIPT_VERIFY, pipeline.SCRIPT_INGEST, pipeline.SCRIPT_LAYOUT, pipeline.SCRIPT_CBZ):
            found = p.find_script(name)
            checks.append(
                {
                    "check": f"script:{name}",
                    "ok": found is not None,
                    "detail": str(found) if found else f"未找到 {name}",
                }
            )

        # 3) jmcomic（导入 + 版本下限）
        jmcomic_version: str | None = None
        try:
            import jmcomic  # noqa: PLC0415

            jmcomic_version = getattr(jmcomic, "__version__", None)
            enough = runtime.version_at_least(jmcomic_version, runtime.MIN_JMCOMIC_VERSION)
            if enough is False:
                checks.append(
                    {
                        "check": "jmcomic",
                        "ok": False,
                        "detail": f"{jmcomic_version or '未知版本'} 低于下限 {runtime.MIN_JMCOMIC_VERSION}（option 新字段会被静默忽略）",
                    }
                )
            else:
                detail = jmcomic_version or "已安装（无 __version__）"
                if enough is None:
                    detail += f"（版本号无法与下限 {runtime.MIN_JMCOMIC_VERSION} 比对）"
                checks.append({"check": "jmcomic", "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001
            checks.append({"check": "jmcomic", "ok": False, "detail": f"导入失败：{exc}"})

        # 3b) 登录态（只读 option，不联网、不触发登录）
        auth = runtime.auth_state(p.option_file)
        checks.append(
            {
                "check": "auth",
                "ok": True,  # 未登录不是环境故障：多数本子无需登录
                "detail": auth["detail"],
            }
        )
        if not auth["logged_in"] and not (auth["has_username"] and auth["has_password"]):
            warnings.append(
                "未登录：收藏夹 / 签到 / 敏感题材需要登录；"
                "用 setx JM_USERNAME / JM_PASSWORD 配置（CLI 不索要凭据）"
            )

        # 3c) 可选依赖（只报告，不安装）
        deps = runtime.dependency_report()
        for item in deps["missing_modules"]:
            checks.append(
                {
                    "check": f"dep:module:{item['module']}",
                    "ok": True,  # 可选依赖缺失不影响主链路
                    "detail": f"未安装（{item['usage']}）→ {item['install']}",
                }
            )
        for item in deps["missing_executables"]:
            checks.append(
                {
                    "check": f"dep:cmd:{item['command']}",
                    "ok": True,
                    "detail": f"未找到（{item['usage']}）",
                }
            )
        if deps["missing_modules"] or deps["missing_executables"]:
            warnings.append(deps["install_hint"])

        # 4) option 文件与代理端口
        option_file = p.option_file
        if option_file.is_file():
            checks.append({"check": "option", "ok": True, "detail": str(option_file)})
            proxy = _read_proxy(option_file)
            if proxy:
                listening = _port_listening(proxy)
                checks.append(
                    {"check": "proxy", "ok": listening, "detail": f"{proxy} {'可连接' if listening else '不可连接（Clash 没开或端口变了）'}"}
                )
                if not listening:
                    warnings.append(f"代理 {proxy} 不可连接 —— 下载会全部超时，先开 Clash / 改 option")
        else:
            checks.append({"check": "option", "ok": False, "detail": f"缺少 {option_file}"})

        # 4b) manga 工作区（流水线脚本的宿主）
        checks.append(
            {
                "check": "manga-workspace",
                "ok": p.workspace is not None,
                "detail": str(p.workspace) if p.workspace else f"未找到 manga 工作区（设 {ENV_WORKSPACE} 指向它）",
            }
        )
        if p.workspace is None:
            warnings.append(
                f"未找到 manga 工作区 → 找不到 4 个流水线脚本。设环境变量 {ENV_WORKSPACE}=<工作区路径>"
            )

        # 5) 目录状态
        for label, path in (("library", p.library), ("inbox", p.inbox), ("export", p.export), ("raw_jmcomic", p.raw_jmcomic)):
            exists = path.is_dir()
            checks.append({"check": f"dir:{label}", "ok": exists, "detail": str(path)})
            if not exists:
                warnings.append(f"目录不存在：{path}")

        ok = all(c["ok"] for c in checks if c["check"] != "proxy")
        return Envelope(
            ok=ok,
            data={
                "checks": checks,
                "jmcomic_version": jmcomic_version,
                "min_jmcomic_version": runtime.MIN_JMCOMIC_VERSION,
                "auth": auth,
                "dependencies": deps,
            },
            warnings=warnings,
            exit=EXIT_OK,
        )

    _run("doctor", run)


@app.command()
def verify(
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
    min_pages: int = typer.Option(5, "--min-pages", help="每话最少页数，低于此值判为残卷"),
    report_path: str | None = typer.Option(None, "--report-path", help="把问题清单写成 CSV"),
    top_n: int = typer.Option(20, "--top-n", help="控制台显示的问题条数（完整清单用 --report-path 的 CSV）"),
    quiet: bool = typer.Option(False, "--quiet", help="只输出汇总"),
    series: list[str] = typer.Option([], "--series", help="只校验指定系列（可重复），用于增量验收"),
) -> None:
    """只读校验权威库（对应 Test-MangaLibrary.ps1）。

    **这是整库闸门**：库里只要有一条 error，退出码就是 2。
    新入库的内容要单独验收时，用 ``--series <系列名>`` —— 校验脚本只认
    「库根/系列/文件.cbz」的两层结构，直接把库根指向系列目录会被判成
    「文件直接位于库根目录」，所以这里把选中的系列**复制**进一个临时影子库根
    再跑，验完即删（只读原库，不改一个字节）。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        script = p.script(pipeline.SCRIPT_VERIFY)

        series_available = _series_with_files(p.library)

        stage_root: Path | None = None
        if series:
            missing = [name for name in series if not (p.library / name).is_dir()]
            if missing:
                raise MangaError(
                    "找不到系列目录：" + "、".join(missing),
                    code="SERIES_MISSING",
                    exit_code=EXIT_NOTFOUND,
                    hint=(
                        "系列名 = 权威库下的目录名。"
                        + (f"当前库里有：{'、'.join(series_available)}" if series_available else "")
                    ),
                )
            stage_root = _stage_series_for_verify(p, series)

        try:
            roots = [stage_root] if stage_root is not None else [p.library]
            reports: list[dict[str, Any]] = []
            warnings: list[str] = []
            failure: pipeline.ScriptResult | None = None

            for root in roots:
                args: list[str] = [
                    "-LibraryRoot",
                    str(root),
                    "-MinPages",
                    str(min_pages),
                    "-TopN",
                    str(top_n),
                ]
                if report_path:
                    target = Path(report_path)
                    if len(roots) > 1:
                        target = target.with_name(f"{target.stem}-{root.name}{target.suffix}")
                    args += ["-ReportPath", str(target)]
                if quiet:
                    args.append("-Quiet")
                result = pipeline.run_script(script, args)
                report = pipeline.parse_verify_report(result.stdout)
                report["issues_listed"] = len(report.get("issues", []))
                report["issues_note"] = f"问题清单最多列出 {top_n} 条；完整清单用 --report-path 导出 CSV"
                report["root"] = str(root)
                reports.append(report)
                if result.warned:
                    warnings.append(f"{result.script} 退出码 1：{root.name} 有告警（不是错误）")
                    tail = _script_output_tail(result)
                    if tail:
                        warnings.append(f"{result.script} 输出尾部：{tail}")
                if result.failed and failure is None:
                    failure = result

            data: dict[str, Any] = {"reports": reports, "series_available": series_available}
            if len(reports) == 1:
                data["report"] = reports[0]
            else:
                data["report"] = {
                    "roots": [r["root"] for r in reports],
                    "series_count": sum(int(r.get("series_count") or 0) for r in reports),
                    "issues_listed": sum(int(r.get("issues_listed") or 0) for r in reports),
                }
            if stage_root is not None:
                data["scoped_series"] = list(series)
                data["scope_note"] = (
                    f"增量校验：{'、'.join(series)} 被复制到临时影子库根后校验，原库未改动"
                )

            if failure is not None:
                return Envelope(
                    ok=False,
                    data=data,
                    error={
                        "code": "SCRIPT_ERROR",
                        "message": f"{failure.script} 退出码 {failure.exit_code}",
                        "hint": (failure.stdout or failure.stderr).strip()[-2000:] or None,
                    },
                    warnings=warnings,
                    exit=EXIT_ERROR,
                )
            return Envelope(ok=True, data=data, warnings=warnings, exit=EXIT_OK)
        finally:
            if stage_root is not None:
                shutil.rmtree(stage_root, ignore_errors=True)

    _run("verify", run)


@app.command()
def ingest(
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
    inbox_root: str | None = typer.Option(None, "--inbox-root", help="收件箱（默认 <library-root>\\00-Incoming\\Manga）"),
    apply: bool = typer.Option(False, "--apply", help="真正执行；不加就是 dry-run（不落盘）"),
    generate_comicinfo: bool = typer.Option(False, "--generate-comicinfo", help="写 ComicInfo.xml（已存在则不覆盖）"),
    zero_pad: bool = typer.Option(False, "--zero-pad", help="纯数字话号补零到 3 位"),
    backup_root: str | None = typer.Option(None, "--backup-root"),
) -> None:
    """入库整理（对应 Invoke-LibraryIngest.ps1）：重命名 + 写元数据 + 移入权威库。

    **默认 dry-run**；--apply 才落盘（会先备份）。
    运行期间会守住红线：``Export\\Manga`` 不得有任何新增。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        script = p.script(pipeline.SCRIPT_INGEST)
        args: list[str] = [
            "-InboxRoot", str(inbox_root or p.inbox),
            "-LibraryRoot", str(p.library),
        ]
        # 备份与日志都显式指定：脚本的默认值是 CWD 相对（'./backup'）和
        # 「脚本目录\logs」—— 前者会写进仓库根，后者在工作区外写不动，
        # 结果就是退出码 1、文件没动、agent 只能猜「有冲突」。
        args += ["-BackupRoot", str(backup_root or p.backups)]
        args += ["-LogPath", str(p.logs / f"ingest-{_timestamp()}.log")]
        if generate_comicinfo:
            args.append("-GenerateComicInfo")
        if zero_pad:
            args.append("-ZeroPad")
        if apply:
            args.append("-Apply")

        with pipeline.ExportGuard(p.export) as guard:
            result = pipeline.run_script(script, args)
            guard.assert_unchanged()

        warnings: list[str] = []
        if not apply:
            warnings.append("dry-run：未加 --apply，未落盘")
        return _script_result_to_envelope(
            result,
            data={"dry_run": not apply, "library_root": str(p.library), "inbox_root": str(inbox_root or p.inbox)},
            extra_warnings=warnings,
        )

    _run("ingest", run)


@app.command("convert-cbz")
def convert_cbz(
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
    source_root: str | None = typer.Option(None, "--source-root", help="jmcomic 散图目录（默认 <library-root>\\00-Incoming\\_raw_jmcomic）"),
    target_root: str | None = typer.Option(None, "--target-root", help="收件箱（默认 <library-root>\\00-Incoming\\Manga）"),
    apply: bool = typer.Option(False, "--apply", help="真正写文件；不加就是 dry-run"),
    keep_images: bool = typer.Option(False, "--keep-images", help="打包后保留原始图片"),
) -> None:
    """把 jmcomic 下载的章节图片打包成 ``<编号> - <标题>.cbz``（对应 Convert-JmComicToCbz.ps1）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        script = p.script(pipeline.SCRIPT_CBZ)
        args: list[str] = [
            "-SourceRoot", str(source_root or p.raw_jmcomic),
            "-TargetRoot", str(target_root or p.inbox),
        ]
        if keep_images:
            args.append("-KeepImages")
        if apply:
            args.append("-Apply")

        with pipeline.ExportGuard(p.export) as guard:
            result = pipeline.run_script(script, args)
            guard.assert_unchanged()

        warnings = [] if apply else ["dry-run：未加 --apply，未写文件"]
        return _script_result_to_envelope(
            result,
            data={"dry_run": not apply, "source_root": str(source_root or p.raw_jmcomic), "target_root": str(target_root or p.inbox)},
            extra_warnings=warnings,
        )

    _run("convert-cbz", run)


@app.command()
def guard(
    library_root: str | None = typer.Option(None, "--library-root"),
    snapshot: str | None = typer.Option(None, "--snapshot", help="把当前 Export\\Manga 清单写入该文件"),
    compare: str | None = typer.Option(None, "--compare", help="与该快照文件比较，报告新增/删除/变更"),
) -> None:
    """红线守卫：查看 / 快照 / 比对 ``Export\\Manga``（Syncthing 共享区）内容。

    灰色来源的产物**绝不能**出现在这里。比对发现新增时退出码为 6（红线拒绝）。
    """

    def run() -> Envelope:
        p = _paths(library_root, None)
        current = pipeline.snapshot_dir(p.export)
        warnings: list[str] = []

        data: dict[str, Any] = {
            "export_root": str(p.export),
            "file_count": len(current),
            "total_bytes": sum(size for size, _ in current.values()),
        }

        if snapshot:
            Path(snapshot).write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            data["snapshot_written"] = str(snapshot)

        if compare:
            before_raw = json.loads(Path(compare).read_text(encoding="utf-8"))
            before = {k: (v[0], v[1]) for k, v in before_raw.items()}
            diff = pipeline.diff_snapshots(before, current)
            data["diff"] = diff
            if diff["changed"]:
                warnings.append(f"共享区有 {len(diff['changed'])} 个文件内容被改动（不判红线）")
            if diff["added"]:
                raise RedLineViolation(
                    "红线：Export\\Manga 出现新增文件（共享区被污染）",
                    data={"export_root": str(p.export), "added_files": diff["added"][:50], "added_count": len(diff["added"])},
                )
            if diff["removed"]:
                raise RedLineViolation(
                    "红线：Export\\Manga 出现文件丢失",
                    data={"export_root": str(p.export), "removed_files": diff["removed"][:50], "removed_count": len(diff["removed"])},
                )

        return Envelope(ok=True, data=data, warnings=warnings)

    _run("guard", run)


# ── jmcomic 采集命令（P1） ────────────────────────────────────────────────


def _jm_option(p: Paths, *, dest: Path | None = None):
    """按 option 文件构造 JmOption。

    落点由 CLI 决定（option 文件只提供 client/download 行为），
    红线目录（共享区 / 权威库）在构造阶段就被拒。
    """
    return jm_adapter.build_option(
        p.option_file,
        dest=dest,
        export_dir=p.export,
        library_dir=p.library,
    )


def _run_in_log_dir(p: Paths, fn):
    """在固定日志目录下执行 ``fn``，返回 ``(结果, warnings)``。

    日志目录（``<书库根>\\Manga\\Metadata\\logs``）建不出来时**不失败**：
    退到系统临时目录，再不行就用当前目录 —— 并把这件事告诉用户。
    """
    with runtime.working_directory(p.logs) as used:
        if used is not None:
            return fn(), []

    fallback = runtime.fallback_log_dir()
    if fallback is not None:
        with runtime.working_directory(fallback) as used:
            if used is not None:
                return fn(), [f"日志目录不可写（{p.logs}）→ 改用 {used}"]

    return fn(), [f"日志目录不可写（{p.logs}）→ 日志会落在当前工作目录"]


@app.command()
def search(
    query: str = typer.Argument(..., help="关键词（+包含 -排除，空格=AND）"),
    mode: str = typer.Option("site", "--mode", help="site|tag|author|work|actor"),
    page: int = typer.Option(1, "--page", help="页码"),
    limit: int = typer.Option(20, "--limit", help="最多返回条数"),
    impl: str | None = typer.Option(None, "--impl", help="api|html（默认取 option）"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """禁漫搜索（五个维度）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        option = _jm_option(p)
        data = jm_adapter.search(option, query, mode=mode, page=page, limit=limit, impl=impl)
        return Envelope(ok=True, data=data)

    _run("search", run)


@app.command()
def album(
    album_id: str = typer.Argument(..., help="本子 ID（禁漫车号）"),
    impl: str | None = typer.Option(None, "--impl"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """本子详情（含章节列表）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        option = _jm_option(p)
        data = jm_adapter.album_detail(option, album_id, impl=impl)
        return Envelope(ok=True, data=data)

    _run("album", run)


@app.command()
def comments(
    album_id: str = typer.Argument(..., help="本子 ID"),
    page: int = typer.Option(1, "--page"),
    limit: int = typer.Option(50, "--limit"),
    impl: str | None = typer.Option(None, "--impl"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """本子评论（含回评树）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        option = _jm_option(p)
        data = jm_adapter.comments(option, album_id, page=page, limit=limit, impl=impl)
        return Envelope(ok=True, data=data)

    _run("comments", run)


@app.command()
def download(
    ids: list[str] = typer.Argument(..., help="本子 ID（可多个）"),
    dest: str | None = typer.Option(None, "--dest", help="落点（默认 <library-root>\\00-Incoming\\_raw_jmcomic）"),
    photo: bool = typer.Option(False, "--photo", help="ID 是章节 ID 而非本子 ID"),
    apply: bool = typer.Option(False, "--apply", help="真正下载；不加就是 dry-run（只解析章节数）"),
    login: bool = typer.Option(True, "--login/--no-login", help="是否用 option 里的账号自动登录"),
    impl: str | None = typer.Option(None, "--impl"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """下载本子/章节到暂存区。

    **默认 dry-run**（只报章节数与图片数）；加 ``--apply`` 才真正下载。
    落点不得是共享区（``Export\\Manga``）或权威库（``Manga\\Library``）——违反即退出码 6。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        # 落点先解析成绝对路径：切工作目录后相对路径会跟着跑偏
        target = Path(dest).expanduser().resolve() if dest else p.raw_jmcomic
        option = _jm_option(p, dest=target)

        def _do():
            return jm_adapter.download(
                option,
                ids,
                dest=target,
                export_dir=p.export,
                library_dir=p.library,
                photo=photo,
                dry_run=not apply,
                login=login,
                impl=impl,
            )

        # 日志治理：jmcomic 的进度日志是相对路径 → 先切到固定日志目录
        data, warnings = _run_in_log_dir(p, _do)
        if not apply:
            warnings.append("dry-run：未加 --apply，未下载任何图片")
        if data.get("degraded"):
            warnings.append(
                f"{data['degraded']} 个 ID 取不到元数据（站点抽风 / 代理不通 / 本子不存在）——"
                "干跑已降级为最小计划，加 --apply 前先确认"
            )
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("download", run)


@app.command("export")
def export_cmd(
    album_id: str = typer.Argument(..., help="本子 ID"),
    fmt: str = typer.Option("zip", "--format", help="zip|pdf|longimg"),
    dest: str | None = typer.Option(None, "--dest", help="落点（默认 <library-root>\\00-Incoming\\_raw_jmcomic）"),
    filename_rule: str = typer.Option("Atitle", "--filename-rule", help="A 前缀=本子字段，P 前缀=章节字段"),
    login: bool = typer.Option(True, "--login/--no-login"),
    impl: str | None = typer.Option(None, "--impl"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """把已下载的本子导出为 ZIP / PDF / 长图。

    走 jmcomic 的 Feature 机制（命中缓存即可导出，不必重下图片）。
    落点同样受红线约束。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        target = Path(dest).expanduser().resolve() if dest else p.raw_jmcomic
        option = _jm_option(p, dest=target)

        def _do():
            return jm_adapter.export(
                option,
                album_id,
                fmt=fmt,
                dest=target,
                export_dir=p.export,
                library_dir=p.library,
                filename_rule=filename_rule,
                login=login,
                impl=impl,
            )

        data, warnings = _run_in_log_dir(p, _do)
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("export", run)


@app.command()
def domains(
    include_html: bool = typer.Option(True, "--include-html/--no-html", help="是否抓取网页端域名（需联网）"),
) -> None:
    """当前域名池（API 静态池 + 网页端域名）。"""

    def run() -> Envelope:
        data = jm_adapter.domains(include_html=include_html)
        warnings: list[str] = []
        if data.get("html_error"):
            warnings.append(f"网页端域名抓取失败：{data['html_error']}")
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("domains", run)


@app.command("pick-domain")
def pick_domain(
    timeout: float = typer.Option(5.0, "--timeout", help="单域名超时秒数"),
    limit: int = typer.Option(5, "--limit", help="返回几个最快的可用域名"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """探测域名池，挑出最快可用的几个。

    站点域名天天变 —— 下载报「重试耗尽」时先跑这个。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        option = None
        try:
            option = _jm_option(p)
        except Exception:  # noqa: BLE001 - option 缺失也不该挡住域名探测
            pass
        data = jm_adapter.pick_domain(option, timeout=timeout, limit=limit)
        warnings: list[str] = []
        if not data.get("best"):
            warnings.append("没有探测到可用域名 —— 检查代理是否开启，或站点已换域名")
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("pick-domain", run)


@app.command()
def version() -> None:
    """打印版本。"""

    def run() -> Envelope:
        return Envelope(ok=True, data={"version": __version__})

    _run("version", run)


# ── 辅助 ──────────────────────────────────────────────────────────────────


def _read_proxy(option_file: Path) -> str | None:
    """从 option.yml 读出代理地址（``client.postman.meta_data.proxies``）。"""
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(option_file.read_text(encoding="utf-8-sig")) or {}
        proxies = ((data.get("client") or {}).get("postman") or {}).get("meta_data", {}).get("proxies")
        if isinstance(proxies, str) and proxies not in ("system", "null", ""):
            return proxies
    except Exception:  # noqa: BLE001 - 体检命令不应因配置异常而崩
        return None
    return None


def _port_listening(proxy: str) -> bool:
    """检查 ``host:port`` 是否可连接。"""
    host, _, port = proxy.rpartition(":")
    if not host or not port.isdigit():
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=1.5):
            return True
    except OSError:
        return False


def main() -> None:
    """console script 入口。"""
    try:
        app()
    except MangaError as exc:  # pragma: no cover - 兜底
        sys.stdout.write(exc.to_envelope().to_json() + "\n")
        raise SystemExit(exc.exit_code) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
