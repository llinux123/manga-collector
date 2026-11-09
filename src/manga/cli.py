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
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import typer

from . import __version__
from . import actions_gen
from . import favorites as favorites_mod
from . import jobs
from . import servers
from .adapters import gallery_dl
from .adapters import jmcomic as jm_adapter
from .adapters import mangadex_dl
from .adapters import pipeline
from .config import (
    ENV_LIBRARY_ROOT,
    ENV_WORKSPACE,
    Paths,
    default_gallery_dl_config,
    resolve_paths,
)
from .envelope import (
    EXIT_CANCELLED,
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
    _start_cancel_watchdog()
    try:
        # stdout 只允许出现信封：命令体里任何意外的 print 都改道 stderr
        with contextlib.redirect_stdout(sys.stderr):
            env = fn()
    except MangaError as exc:
        env = exc.to_envelope()
        _finish_plugins()
        _finalize_job(env)
        raise typer.Exit(code=emit(env)) from exc
    except Exception as exc:  # noqa: BLE001 - 兜底：不让异常裸奔到 agent
        env = Envelope(
            ok=False,
            error={"code": "UNEXPECTED", "message": f"{type(exc).__name__}: {exc}", "hint": None},
            exit=EXIT_ERROR,
        )
        _finish_plugins()
        _finalize_job(env)
        raise typer.Exit(code=emit(env)) from exc
    _finish_plugins()
    _finalize_job(env)
    raise typer.Exit(code=emit(env))


def _start_cancel_watchdog() -> None:
    """在作业里跑时，盯着「取消标记」，看到就先优雅取消、再强退。

    **两级取消**：先调所有已注册的 ``DownloadControl.cancel()``（给 jmcomic
    一个在下一个检查点优雅停下的机会），等几秒后还没退出就 ``os._exit``。
    """
    job_dir = os.environ.get(jobs.ENV_JOB_DIR)
    if not job_dir:
        return

    def _graceful_cancel() -> None:
        # ① jmcomic 的协作式取消（DownloadControl → 下一个检查点优雅停下）
        for control in _active_controls:
            try:
                control.cancel("用户取消")
            except Exception:  # noqa: BLE001 - 取消回调炸了也得能退
                pass
        # ② 外部下载器（gallery-dl / mangadex-dl）—— 直接杀掉孙进程
        #    （这些进程是本进程起的，Popen.kill 不需要特权）
        try:
            gallery_dl.kill_active()
        except Exception:  # noqa: BLE001
            pass

    try:
        jobs.start_cancel_watchdog(Path(job_dir), on_cancel=_graceful_cancel, grace=5.0)
    except Exception:  # noqa: BLE001 - 看门狗起不来不该影响命令本身
        pass


def _finalize_job(env: Envelope) -> None:
    """在作业里跑时，把终态写进 ``final.json``（不在作业里就是空操作）。

    父进程启动完作业就退出了，没法替子进程收尾 —— 终态只能由**子进程自己**写。
    """
    try:
        jobs.finalize(
            {
                "ok": env.ok,
                "data": env.data,
                "error": env.error,
                "warnings": env.warnings,
                "exit": env.exit,
            }
        )
    except Exception:  # noqa: BLE001 - 写终态失败不该影响命令本身
        pass


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


def _jm_option(p: Paths, *, dest: Path | None = None, option_file: str | None = None):
    """按 option 文件构造 JmOption。

    落点由 CLI 决定（option 文件只提供 client/download 行为），
    红线目录（共享区 / 权威库）在构造阶段就被拒。

    ``option_file``：变体配置文件（``--option``）。不给则用 ``p.option_file``
    （工作区默认或 ``MANGA_OPTION_FILE`` 环境变量）。

    同时把 option 记到 ``_current_options`` —— 命令退出前要调
    ``wait_all_plugins_finish()`` 等异步插件收工（上游只定义、不自动调）。
    """
    chosen = Path(option_file).expanduser() if option_file else p.option_file
    option = jm_adapter.build_option(
        chosen,
        dest=dest,
        export_dir=p.export,
        library_dir=p.library,
    )
    _current_options.append(option)
    return option


#: 本次命令用到的 JmOption（退出前统一等插件收工）
_current_options: list[Any] = []

#: 本次命令创建的 DownloadControl（取消看门狗看到标记后逐个调 cancel()）
_active_controls: list[Any] = []


def _finish_plugins() -> None:
    """命令收尾：等异步插件干完活（见 :func:`runtime.finish_plugins`）。"""
    while _current_options:
        option = _current_options.pop()
        try:
            runtime.finish_plugins(option)
        except Exception:  # noqa: BLE001 - 停机失败不该盖掉命令结果
            pass


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
    query: str | None = typer.Argument(None, help="关键词（+包含 -排除，空格=AND）；用 --ranking / --category 时可省略"),
    mode: str = typer.Option("site", "--mode", help="site|tag|author|work|actor"),
    page: int = typer.Option(1, "--page", help="页码"),
    limit: int = typer.Option(20, "--limit", help="最多返回条数"),
    category: str | None = typer.Option(None, "--category", help="分类：all|doujin|single|short|another|hanman|meiman|doujin_cosplay|3d|english_site"),
    time: str | None = typer.Option(None, "--time", help="时间范围：all|today|week|month"),
    order_by: str | None = typer.Option(None, "--order-by", help="排序：latest|view|picture|like|score|comment"),
    ranking: str | None = typer.Option(None, "--ranking", help="排行榜：day|week|month"),
    impl: str | None = typer.Option(None, "--impl", help="api|html（默认取 option）"),
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """禁漫搜索（关键词五维）+ 分类浏览 + 排行榜。

    * 关键词搜索：``manga search <关键词> [--mode tag|author|work|actor]``
    * 分类浏览：``manga search --category hanman --time week --order-by latest``
    * 排行榜：``manga search --ranking week``
    """

    def run() -> Envelope:
        if ranking is not None and query is not None:
            raise MangaError(
                "--ranking 与关键词互斥",
                code="BAD_USAGE",
                exit_code=EXIT_USAGE,
                hint="排行榜不需要关键词；分类浏览才用 --category",
            )
        if ranking is None and category is None and not query:
            raise MangaError(
                "搜索需要关键词，或指定 --category / --ranking",
                code="BAD_USAGE",
                exit_code=EXIT_USAGE,
            )
        p = _paths(library_root, scripts_dir)
        option = _jm_option(p, option_file=option_file)
        data = jm_adapter.search(
            option,
            query or "",
            mode=mode,
            page=page,
            limit=limit,
            impl=impl,
            category=category,
            time=time,
            order_by=order_by,
            ranking=ranking,
        )
        return Envelope(ok=True, data=data)

    _run("search", run)


@app.command()
def album(
    album_id: str = typer.Argument(..., help="本子 ID（禁漫车号）"),
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """本子详情（含章节列表）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        option = _jm_option(p, option_file=option_file)
        data = jm_adapter.album_detail(option, album_id, impl=impl)
        return Envelope(ok=True, data=data)

    _run("album", run)


@app.command()
def comments(
    album_id: str = typer.Argument(..., help="本子 ID"),
    page: int = typer.Option(1, "--page"),
    limit: int = typer.Option(50, "--limit"),
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """本子评论（含回评树）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        option = _jm_option(p, option_file=option_file)
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
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置（默认用工作区的 option.yml）"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """下载本子/章节到暂存区。

    **默认 dry-run**（只报章节数与图片数）；加 ``--apply`` 才真正下载。
    落点不得是共享区（``Export\\Manga``）或权威库（``Manga\\Library``）——违反即退出码 6。

    **透传**：jmcomic 的高级能力（插件、并发、域名…）不写进 CLI 参数 ——
    用 ``--option <变体.yml>`` 换一份配置即可（见 references/option-and-plugins.md）。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        # 落点先解析成绝对路径：切工作目录后相对路径会跟着跑偏
        target = Path(dest).expanduser().resolve() if dest else p.raw_jmcomic
        option = _jm_option(p, dest=target, option_file=option_file)

        # 协作式取消：作业模式下创建 DownloadControl，注册给取消看门狗
        control = None
        if os.environ.get(jobs.ENV_JOB_DIR) and apply:
            try:
                from jmcomic import DownloadControl

                control = DownloadControl()
                _active_controls.append(control)
            except ImportError:  # pragma: no cover - jmcomic 未装时静默跳过
                pass

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
                on_progress=_job_progress,
                control=control,
            )

        # 作业模式（manga job start download …）：先把条目登记进去，
        # 这样即使中途被取消，也知道「要下哪几个、已完成哪几个」
        jobs.record_progress(items=[str(i) for i in ids], message=f"开始下载 {len(ids)} 个 ID")

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
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置"),
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
        option = _jm_option(p, dest=target, option_file=option_file)

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
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置"),
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
            option = _jm_option(p, option_file=option_file)
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


# ── 收藏夹 / 签到（P3） ───────────────────────────────────────────────────


@app.command()
def favorites(
    dest: str | None = typer.Option(None, "--dest", help="落点（默认 <library-root>\\Manga\\Metadata\\favorites）"),
    make_zip: bool = typer.Option(False, "--zip", help="额外打一个 zip"),
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """导出收藏夹为 CSV（每个收藏夹一个文件），可选再打一个 zip。

    ⚠ 收藏夹是**灰色来源的书单** → 落点不得是共享区（红线，退出码 6）。
    ⚠ 加密 zip 的密码只从环境变量 ``MANGA_ZIP_PASSWORD`` 读，不接受命令行明文。
    需要登录态（退出码 3）。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        target = (
            Path(dest).expanduser().resolve()
            if dest
            else (p.metadata / "favorites").resolve()
        )
        option = _jm_option(p, option_file=option_file)
        data, warnings = _run_in_log_dir(
            p,
            lambda: favorites_mod.export_favorites(
                option,
                dest=target,
                export_dir=p.export,
                library_dir=p.library,
                zip_enable=make_zip,
                zip_password=os.environ.get(favorites_mod.ENV_ZIP_PASSWORD),
            ),
        )
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("favorites", run)


@app.command()
def checkin(
    option_file: str | None = typer.Option(None, "--option", help="jmcomic option 变体配置"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """每日签到。``code`` 0 = 成功，1 = 今天已经签过。需要登录态（退出码 3）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        option = _jm_option(p, option_file=option_file)
        data, warnings = _run_in_log_dir(p, lambda: favorites_mod.checkin(option))
        if data.get("already_checked_in"):
            warnings.append("今天已经签过过了")
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("checkin", run)


# ── 外部下载器（P3） ──────────────────────────────────────────────────────


@app.command("gallery-dl", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def gallery_dl_cmd(
    ctx: typer.Context,
    urls: list[str] = typer.Argument(..., help="要下载的 URL（可多个）"),
    dest: str | None = typer.Option(None, "--dest", help="落点（默认 <library-root>\\00-Incoming\\Manga）"),
    apply: bool = typer.Option(False, "--apply", help="真正下载；不加就是**预检**（只模拟，不落盘）"),
    timeout: float = typer.Option(120.0, "--timeout", help="单次命令超时（秒）"),
    config: str | None = typer.Option(None, "--config", help="gallery-dl 配置文件（透传给 gallery-dl 的 -c）；给了就**替换**默认的合规命名配置"),
    option: list[str] = typer.Option(None, "-o", help="gallery-dl 选项覆盖（透传给 gallery-dl 的 -o key=value）"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """gallery-dl（通用站的事实标准，直接产出 CBZ）。

    **默认预检**：`gallery-dl -s` 只模拟、不落盘 → 判断这个 URL 站不支持。
    不支持的 URL 明确返回「不支持」（退出码 4），**不会**发起下载。
    加 ``--apply`` 才真下载。落点同样受红线约束。

    **默认套用「入库合规命名」配置**（``src/manga/data/gallery-dl-option.json``）：
    gallery-dl 原生的目录格式产出 ``v01 c001`` 这类名字，**过不了**入库闸门
    （命名规范要求 ``<话号> - <标题>.cbz``）。配置按站把模板改成合规形态。
    ``--config`` 给的是**替换**（不是叠加）—— 想用上游原生命名就显式传一个配置。

    **透传**：``--config`` / ``-o`` / ``--`` 之后的参数原样转发给 gallery-dl
    （预检时不透传 —— 预检只回答「支不支持」）。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        target = Path(dest).expanduser().resolve() if dest else p.inbox.resolve()

        # 组装透传参数：--config → -c；-o 原样；-- 之后的原样
        passthrough: list[str] = []
        cfg = Path(config).expanduser() if config else default_gallery_dl_config()
        if cfg.is_file():
            passthrough.extend(["-c", str(cfg)])
        elif config:
            raise MangaError(
                f"找不到 gallery-dl 配置文件：{cfg}",
                code="CONFIG_MISSING",
                exit_code=EXIT_NOTFOUND,
                hint="检查 --config 的路径；或删掉它以用内置的合规命名配置",
            )
        if option:
            for kv in option:
                passthrough.extend(["-o", kv])
        passthrough.extend(ctx.args)

        if not apply:
            checks = [gallery_dl.precheck(url, timeout=timeout) for url in urls]
            unsupported = [c for c in checks if not c.get("supported")]
            data = {
                "dry_run": True,
                "dest": str(target),
                "checks": checks,
                "supported_count": len(checks) - len(unsupported),
                "unsupported_count": len(unsupported),
            }
            if passthrough:
                data["note"] = "预检不应用透传参数（预检只回答「支不支持」）；下载时才会带上"
            if unsupported:
                raise MangaError(
                    "gallery-dl 不支持这些 URL：" + "、".join(str(c.get("url")) for c in unsupported),
                    code="UNSUPPORTED_URL",
                    exit_code=EXIT_NOTFOUND,
                    hint="换源，或人工用 FMD2；详见 references/sources.md",
                    data=data,
                )
            return Envelope(ok=True, data=data, warnings=["dry-run：未加 --apply，未下载任何文件"])

        # 红线：透传参数里的落点不得指向共享区/权威库
        from .passthrough import assert_passthrough_safe

        assert_passthrough_safe(passthrough, export_dir=p.export, library_dir=p.library)

        data = gallery_dl.download(
            urls,
            target,
            export_dir=p.export,
            library_dir=p.library,
            timeout=timeout if timeout >= 3600 else 3600.0,
            extra_args=passthrough,
        )
        return Envelope(ok=True, data=data)

    _run("gallery-dl", run)


@app.command("mangadex-dl", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def mangadex_dl_cmd(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="MangaDex 的作品 URL"),
    dest: str | None = typer.Option(None, "--dest", help="落点（默认 <library-root>\\00-Incoming\\Manga）"),
    language: str | None = typer.Option(None, "--language", help="语言过滤（如 zh；留空 = 不限）"),
    apply: bool = typer.Option(False, "--apply", help="真正下载；不加就是**预检**（只查工具与计划）"),
    timeout: float = typer.Option(3600.0, "--timeout", help="超时（秒）"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """mangadex-downloader（MangaDex 专项，默认写 ComicInfo.xml）。

    **默认预检**：只确认工具在不在、计划怎么跑，不下载。
    加 ``--apply`` 才真下载（产物是 CBZ，带 ComicInfo.xml）。

    **透传**：``--`` 之后的参数原样转发给 mangadex-dl。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        target = Path(dest).expanduser().resolve() if dest else p.inbox.resolve()
        passthrough = list(ctx.args)

        if not apply:
            exe = mangadex_dl.find_executable()  # 找不到会抛 TOOL_MISSING（退出码 4）
            return Envelope(
                ok=True,
                data={
                    "dry_run": True,
                    "dest": str(target),
                    "url": url,
                    "language": language,
                    "executable": str(exe),
                    "command": f"mangadex-dl --save-as cbz --path {target}"
                    + "".join(f" {a}" for a in passthrough)
                    + (f" --language {language}" if language else "")
                    + f" {url}",
                },
                warnings=["dry-run：未加 --apply，未下载任何文件"],
            )

        from .passthrough import assert_passthrough_safe

        assert_passthrough_safe(passthrough, export_dir=p.export, library_dir=p.library)

        data = mangadex_dl.download(
            url,
            target,
            language=language,
            export_dir=p.export,
            library_dir=p.library,
            timeout=timeout,
            extra_args=passthrough,
        )
        return Envelope(ok=True, data=data)

    _run("mangadex-dl", run)


# ── 自动化（P3） ──────────────────────────────────────────────────────────


@app.command("actions-gen")
def actions_gen_cmd(
    dest: str | None = typer.Option(None, "--dest", help="仓库根目录（workflow 写进 <dest>/.github/workflows）"),
    apply: bool = typer.Option(False, "--apply", help="真正写盘；不加只报告将要写什么"),
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
) -> None:
    """生成 GitHub Actions workflow（download_dispatch.yml + export_favorites.yml）。

    **默认 dry-run**：只报告将要写哪些文件；``--apply`` 才落盘。
    同时返回 secrets 清单（只有名字与说明，不含任何凭据）。
    """

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        root = Path(dest).expanduser().resolve() if dest else Path.cwd()
        data = actions_gen.write_workflows(root, apply=apply)
        data["secrets"] = actions_gen.secrets_manifest()
        data["validation"] = actions_gen.validate_workflows()
        if not data["validation"]["ok"]:
            raise MangaError(
                "生成的 workflow 没通过自检",
                code="WORKFLOW_INVALID",
                exit_code=EXIT_ERROR,
                data=data,
            )
        return Envelope(
            ok=True,
            data=data,
            warnings=[] if apply else ["dry-run：未加 --apply，没有写任何文件"],
        )

    _run("actions-gen", run)


server_app = typer.Typer(help="服务端（Komga / Kavita）扫描助手：只读探测 + 扫描触发")
app.add_typer(server_app, name="server")


def _server_auth() -> tuple[tuple[str, str] | None, str | None]:
    """服务端的凭据只从**环境变量**读（不落命令行历史）。"""
    user = os.environ.get("KOMGA_USERNAME")
    password = os.environ.get("KOMGA_PASSWORD")
    auth = (user, password) if user and password else None
    return auth, os.environ.get("KOMGA_API_KEY")


@server_app.command("check")
def server_check(
    base_url: str | None = typer.Option(None, "--base-url", help="服务地址（默认读 KOMGA_BASE_URL / KAVITA_BASE_URL）"),
    timeout: float = typer.Option(8.0, "--timeout"),
) -> None:
    """探测服务端是否可达。**网络失败不报错**，返回 reachable=false + 原因。"""

    def run() -> Envelope:
        url = base_url or os.environ.get("KOMGA_BASE_URL") or os.environ.get("KAVITA_BASE_URL")
        if not url:
            raise MangaError(
                "没给服务地址",
                code="NO_BASE_URL",
                exit_code=EXIT_USAGE,
                hint="用 --base-url，或设 KOMGA_BASE_URL / KAVITA_BASE_URL",
            )
        auth, api_key = _server_auth()
        data = servers.check_server(url, auth=auth, api_key=api_key, timeout=timeout)
        warnings = [] if data.get("reachable") else [f"服务端不可达：{data.get('reason')}"]
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("server check", run)


@server_app.command("libraries")
def server_libraries(
    base_url: str | None = typer.Option(None, "--base-url"),
    timeout: float = typer.Option(8.0, "--timeout"),
) -> None:
    """列出 Komga 的书库（id / 名称 / 根路径）—— 挑一个来扫描。"""

    def run() -> Envelope:
        url = base_url or os.environ.get("KOMGA_BASE_URL")
        if not url:
            raise MangaError(
                "没给服务地址",
                code="NO_BASE_URL",
                exit_code=EXIT_USAGE,
                hint="用 --base-url，或设 KOMGA_BASE_URL",
            )
        auth, api_key = _server_auth()
        return Envelope(ok=True, data=servers.komga_libraries(url, auth=auth, api_key=api_key, timeout=timeout))

    _run("server libraries", run)


@server_app.command("scan")
def server_scan(
    library_id: str = typer.Argument(..., help="要扫描的书库 id"),
    base_url: str | None = typer.Option(None, "--base-url"),
    timeout: float = typer.Option(8.0, "--timeout"),
) -> None:
    """触发 Komga 扫描某个书库（``POST /api/v1/libraries/:id/scan``）。

    需要 ADMIN 权限。**只触发扫描**，不改任何元数据。
    """

    def run() -> Envelope:
        url = base_url or os.environ.get("KOMGA_BASE_URL")
        if not url:
            raise MangaError(
                "没给服务地址",
                code="NO_BASE_URL",
                exit_code=EXIT_USAGE,
                hint="用 --base-url，或设 KOMGA_BASE_URL",
            )
        auth, api_key = _server_auth()
        data = servers.trigger_komga_scan(url, library_id, auth=auth, api_key=api_key, timeout=timeout)
        return Envelope(ok=True, data=data)

    _run("server scan", run)


@server_app.command("kavita-hint")
def server_kavita_hint(
    base_url: str | None = typer.Option(None, "--base-url"),
    library_path: str | None = typer.Option(None, "--library-path", help="Kavita 里的书库路径"),
) -> None:
    """Kavita 的扫描**指引**（本项目不自动做 —— Kavita 没有等价的公开扫描 API）。"""

    def run() -> Envelope:
        url = base_url or os.environ.get("KAVITA_BASE_URL")
        return Envelope(ok=True, data=servers.kavita_scan_hint(url, library_path=library_path))

    _run("server kavita-hint", run)


# ── 作业模型（P3） ────────────────────────────────────────────────────────

job_app = typer.Typer(
    help="长任务作业模型：start / poll / cancel / result（下载这类慢命令别阻塞等）",
    no_args_is_help=True,
)
app.add_typer(job_app, name="job")


def _job_store(library_root: str | None = None) -> jobs.JobStore:
    """作业目录：优先书库里，退到系统临时目录。"""
    p = _paths(library_root, None)
    return jobs.JobStore.create([p.jobs, Path(tempfile.gettempdir()) / "manga-jobs"])


def _infer_items(args: list[str]) -> list[str]:
    """从透传参数里推断「作业条目」。

    取**第一个 ``-`` 开头参数之前**的位置参数 —— 那通常是 ID 列表
    （``manga job start download 404005 390442 --apply``）。
    推断不准也不要紧：``--item`` 可以显式指定，进度只是给人看的。
    """
    items: list[str] = []
    for token in args:
        if token.startswith("-"):
            break
        items.append(token)
    return items


def _job_progress(event: str, item_id: str, error: str | None) -> None:
    """下载进度回调：写进作业的 progress.json。

    不在作业里跑时 :func:`jobs.record_progress` 直接返回 False —— 普通命令不受影响。
    """
    if event == "done":
        jobs.record_progress(done=[item_id], message=f"已完成 {item_id}")
    elif event == "failed":
        jobs.record_progress(failed=[item_id], message=f"{item_id} 失败：{error}")


def _known_commands() -> list[str]:
    """CLI 已注册的命令名（供 `job start` 校验用）。

    ``@app.command()`` 不带名字时，typer 用函数名注册 —— 那些也要算进去。
    """
    names: set[str] = set()
    for info in app.registered_commands:
        name = info.name or getattr(info.callback, "__name__", "")
        if name:
            names.add(name.replace("_", "-"))
    return sorted(names)


@job_app.command("start", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def job_start(
    ctx: typer.Context,
    command: str = typer.Argument(..., help="要跑的命令：download / ingest / verify / convert-cbz …"),
    item: list[str] = typer.Option(None, "--item", help="作业条目（可重复）；不给则从位置参数推断"),
    library_root: str | None = typer.Option(None, "--library-root"),
) -> None:
    """后台启动一个长任务，**立刻返回 job id**（不等待）。

    剩余参数原样透传给子命令::

        manga job start download 404005 390442 --apply
    """

    def run() -> Envelope:
        known = _known_commands()
        if command not in known:
            raise MangaError(
                f"没有这个命令：{command}",
                code="UNKNOWN_COMMAND",
                exit_code=EXIT_USAGE,
                hint="可用命令：" + "、".join(known),
            )
        store = _job_store(library_root)
        extra = list(ctx.args)
        # 书库根用**环境变量**传给子进程，而不是拼命令行参数 ——
        # 不是每个命令都认 `--library-root`（`version` 就不认），拼上去会让子进程直接用法错。
        env_extra = {}
        if library_root:
            env_extra[ENV_LIBRARY_ROOT] = str(Path(library_root).expanduser().resolve())
        items = list(item) if item else _infer_items(ctx.args)
        state = jobs.start(store, command, extra, items=items, env_extra=env_extra)
        return Envelope(
            ok=True,
            data={
                "job_id": state.id,
                "state": state.state,
                "command": state.command,
                "argv": state.argv,
                "pid": state.pid,
                "items": items,
                "poll_hint": f"manga job poll {state.id}",
                "jobs_dir": str(store.root),
            },
            warnings=[f"作业已在后台启动；用 `manga job poll {state.id}` 低频轮询，别一直等"],
        )

    _run("job start", run)


@job_app.command("poll")
def job_poll(
    job_id: str = typer.Argument(..., help="作业 id"),
    library_root: str | None = typer.Option(None, "--library-root"),
) -> None:
    """查作业状态 —— **精简**，几秒就返回，可以低频轮询。"""

    def run() -> Envelope:
        store = _job_store(library_root)
        data = jobs.poll(store, job_id)
        warnings: list[str] = []
        if data["state"] == jobs.STATE_FAILED:
            warnings.append("作业失败 —— 用 `manga job result` 看完整结果")
        return Envelope(ok=True, data=data, warnings=warnings)

    _run("job poll", run)


@job_app.command("cancel")
def job_cancel(
    job_id: str = typer.Argument(..., help="作业 id"),
    library_root: str | None = typer.Option(None, "--library-root"),
) -> None:
    """取消作业：杀掉进程树，并给出**已完成 / 未完成**清单。"""

    def run() -> Envelope:
        store = _job_store(library_root)
        data = jobs.cancel(store, job_id)
        return Envelope(
            ok=True,
            data=data,
            warnings=[f"已取消；完成 {data['completed_count']} 项，未完成 {data['unfinished_count']} 项"],
        )

    _run("job cancel", run)


@job_app.command("result")
def job_result(
    job_id: str = typer.Argument(..., help="作业 id"),
    library_root: str | None = typer.Option(None, "--library-root"),
) -> None:
    """作业最终摘要：子进程的信封 + 逐项结果。

    作业失败 → 退出码 2；作业被取消 → 退出码 5。
    """

    def run() -> Envelope:
        store = _job_store(library_root)
        data = jobs.result(store, job_id)
        state = data.get("state")
        exit_code = EXIT_OK
        if state == jobs.STATE_FAILED:
            exit_code = EXIT_ERROR
        elif state == jobs.STATE_CANCELLED:
            exit_code = EXIT_CANCELLED
        return Envelope(
            ok=state == jobs.STATE_SUCCEEDED,
            data=data,
            warnings=list(data.get("warnings") or []),
            exit=exit_code,
        )

    _run("job result", run)


@job_app.command("list")
def job_list(
    limit: int = typer.Option(20, "--limit", help="最多列几条"),
    library_root: str | None = typer.Option(None, "--library-root"),
) -> None:
    """列出最近的作业。"""

    def run() -> Envelope:
        store = _job_store(library_root)
        states = store.list(limit=limit)
        return Envelope(
            ok=True,
            data={
                "jobs_dir": str(store.root),
                "count": len(states),
                "jobs": [
                    {
                        "job_id": s.id,
                        "command": s.command,
                        "state": s.state,
                        "exit_code": s.exit_code,
                        "items_done": len(s.done),
                        "items_failed": len(s.failed),
                        "created_at": s.created_at,
                        "finished_at": s.finished_at,
                    }
                    for s in states
                ],
            },
        )

    _run("job list", run)


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
