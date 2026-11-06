"""``manga`` 命令行入口。

**契约（agent 只依赖这一层）**：

* stdout 永远只有一个 JSON 信封：``{"ok", "data", "error", "warnings", "exit"}``；
* 退出码固定：0 成功（可能带 warnings）/ 1 用法错 / 2 执行错 / 4 资源不存在 / 6 红线拒绝；
* 写操作默认 dry-run，必须显式 --apply 才落盘；
* **红线**：灰色来源（禁漫等未授权内容）的产物不得进入 ``Export\\Manga``（Syncthing 共享区）。
  守卫在代码里执行（``adapters.pipeline.ExportGuard``），不依赖调用方自觉。
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import typer

from . import __version__
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

app = typer.Typer(
    name="manga",
    help="漫画采集聚合层 · 薄 CLI（Skill 是界面，CLI 是手，agent 是大脑）",
    no_args_is_help=True,
    add_completion=False,
)

# ── 公共选项 ──────────────────────────────────────────────────────────────


def _paths(library_root: str | None, scripts_dir: str | None) -> Paths:
    return resolve_paths(library_root=library_root, scripts_dir=scripts_dir)


def _run(command: str, fn) -> None:
    """执行命令并统一处理异常 → 信封。"""
    try:
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
    """环境体检：PowerShell、脚本、jmcomic、代理端口、目录状态。"""

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

        # 3) jmcomic
        try:
            import jmcomic  # noqa: PLC0415

            ver = getattr(jmcomic, "__version__", None)
            checks.append({"check": "jmcomic", "ok": True, "detail": ver or "已安装（无 __version__）"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"check": "jmcomic", "ok": False, "detail": f"导入失败：{exc}"})

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
        return Envelope(ok=ok, data={"checks": checks}, warnings=warnings, exit=EXIT_OK)

    _run("doctor", run)


@app.command()
def verify(
    library_root: str | None = typer.Option(None, "--library-root"),
    scripts_dir: str | None = typer.Option(None, "--scripts-dir"),
    min_pages: int = typer.Option(5, "--min-pages", help="每话最少页数，低于此值判为残卷"),
    report_path: str | None = typer.Option(None, "--report-path", help="把问题清单写成 CSV"),
    top_n: int = typer.Option(20, "--top-n", help="控制台显示的问题条数（完整清单用 --report-path 的 CSV）"),
    quiet: bool = typer.Option(False, "--quiet", help="只输出汇总"),
) -> None:
    """只读校验权威库（对应 Test-MangaLibrary.ps1）。"""

    def run() -> Envelope:
        p = _paths(library_root, scripts_dir)
        script = p.script(pipeline.SCRIPT_VERIFY)
        args: list[str] = ["-LibraryRoot", str(p.library), "-MinPages", str(min_pages), "-TopN", str(top_n)]
        if report_path:
            args += ["-ReportPath", str(report_path)]
        if quiet:
            args.append("-Quiet")
        result = pipeline.run_script(script, args)
        report = pipeline.parse_verify_report(result.stdout)
        report["issues_listed"] = len(report.get("issues", []))
        report["issues_note"] = f"问题清单最多列出 {top_n} 条；完整清单用 --report-path 导出 CSV"
        return _script_result_to_envelope(result, data={"report": report})

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
        if backup_root:
            args += ["-BackupRoot", str(backup_root)]
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
