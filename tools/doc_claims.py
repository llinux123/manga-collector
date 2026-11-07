"""文档声明核对器：把 references 里的「事实性声明」拿去源码/脚本里验一遍。

人工复核最容易漏的是**看起来对、其实已经改名或不存在**的东西：
API 方法、常量、异常类、CLI 子命令、PS 脚本名、校验器的命名正则、option 字段。
这个脚本把它们全部抽出来逐条核对，漂移就报出来。

用法：``uv run python tools/doc_claims.py check``
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = REPO_ROOT / ".agents" / "skills" / "manga" / "references"


def _read(name: str) -> str:
    path = REFERENCES / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _dotnet_regex(pattern: str) -> str:
    """把 .NET 命名组 ``(?<name>...)`` 翻译成 Python 的 ``(?P<name>...)``。"""
    return re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", pattern)


# ── jmcomic.md：API / 常量 / 异常 ─────────────────────────────────────────


def check_jmcomic_api() -> list[str]:
    import jmcomic
    from jmcomic import jm_client_interface, jm_config, jm_entity, jm_exception, jm_feature, jm_option

    text = _read("jmcomic.md")
    problems: list[str] = []

    # 客户端接口基类：2.7.11 里叫 JmcomicClient
    client_api = getattr(jm_client_interface, "JmcomicClient", None)
    if client_api is None:
        return ["jmcomic.md：jmcomic.jm_client_interface 里找不到客户端基类"]

    option_api = jm_option.JmOption

    # client.<method>(  → JmClientInterface 上必须真有
    for name in sorted(set(re.findall(r"\bclient\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text))):
        if not hasattr(client_api, name):
            problems.append(f"jmcomic.md：client.{name}() 在 JmClientInterface 上不存在")

    # option.<method>(  → JmOption 上必须真有
    for name in sorted(set(re.findall(r"\boption\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text))):
        if not hasattr(option_api, name):
            problems.append(f"jmcomic.md：option.{name}() 在 JmOption 上不存在")

    # JmMagicConstants.X / JmModuleConfig.X
    for holder in ("JmMagicConstants", "JmModuleConfig"):
        cls = getattr(jm_config, holder, None)
        if cls is None:
            problems.append(f"jmcomic.md：jmcomic 里没有 {holder}")
            continue
        for name in sorted(set(re.findall(rf"\b{holder}\.([A-Z][A-Z0-9_]*)\b", text))):
            if not hasattr(cls, name):
                problems.append(f"jmcomic.md：{holder}.{name} 不存在")

    # 异常类名（异常表里的第一列）
    exception_names = set(re.findall(r"^\| `([A-Za-z_][A-Za-z0-9_]*Exception)` \|", text, re.M))
    for name in sorted(exception_names):
        if not hasattr(jm_exception, name):
            problems.append(f"jmcomic.md：异常类 {name} 在 jmcomic.jm_exception 里不存在")

    # 顶层导出的函数/类
    for name in sorted(
        set(
            re.findall(r"^(?:album, dler = )?(download_album_async|download_photo_async|download_batch_async)", text, re.M)
        )
    ):
        if not hasattr(jmcomic, name):
            problems.append(f"jmcomic.md：jmcomic 顶层没有 {name}")

    for name in ("JmSyncRuntime", "JmAsyncRuntime", "DownloadControl", "jm_task_context", "DownloadCancelledException"):
        if not hasattr(jmcomic, name):
            problems.append(f"jmcomic.md：jmcomic 顶层没有 {name}")

    # Feature.export_*
    for name in sorted(set(re.findall(r"\bFeature\.(export_[a-z_]+)", text))):
        if not hasattr(jm_feature.Feature, name):
            problems.append(f"jmcomic.md：Feature.{name} 不存在（内置只有 export_pdf/export_zip/export_long_img）")

    # 实体字段（§4 实体三层字段）
    for cls_name, prefix in (("JmAlbumDetail", "A"), ("JmPhotoDetail", "P")):
        cls = getattr(jm_entity, cls_name, None)
        if cls is None:
            problems.append(f"jmcomic.md：jmcomic 里没有 {cls_name}")
            continue

    return problems


# ── option-and-plugins.md：option 字段 ───────────────────────────────────


def check_option_fields() -> list[str]:
    from jmcomic.jm_option import JmOption

    text = _read("option-and-plugins.md")
    defaults = JmOption.default_dict()
    problems: list[str] = []

    # 取 yaml 代码块里的顶层键
    block = re.search(r"```yaml\n(.*?)```", text, re.S)
    if not block:
        return ["option-and-plugins.md：找不到 option 的 yaml 代码块"]

    known_top = set(defaults) | {"version", "plugins"}
    for line in block.group(1).splitlines():
        if not line or line.startswith(" ") or line.startswith("#") or line.startswith("-"):
            continue
        key = line.split(":")[0].strip()
        if key and key not in known_top:
            problems.append(f"option-and-plugins.md：option 顶层字段 `{key}` 不在默认结构里")

    # 嵌套字段抽查
    nested = {
        "auth": defaults["auth"],
        "client": defaults["client"],
        "download": defaults["download"],
        "dir_rule": defaults["dir_rule"],
    }
    # plugins 下除 valid/dependencies_strategy 外，其余是**用户自定义的事件名**
    # （after_init / after_album / ...），不由上游定义，所以不参与核对
    for section, keys in re.findall(r"^(\w+):\n((?:  \w[\w-]*:.*\n)+)", block.group(1), re.M):
        if section == "plugins":
            continue
        default_section = nested.get(section)
        if default_section is None:
            continue
        for key in re.findall(r"^  ([\w-]+):", keys, re.M):
            if key not in default_section:
                problems.append(f"option-and-plugins.md：option.{section}.{key} 不在默认结构里")

    return problems


# ── pipeline.md：脚本名 / 退出码 / 命名正则 ───────────────────────────────


def check_pipeline() -> list[str]:
    from manga.adapters import pipeline as pl
    from manga.config import find_workspace

    text = _read("pipeline.md")
    problems: list[str] = []

    workspace = find_workspace()
    if workspace is None:
        return ["pipeline.md：找不到 manga 工作区，跳过脚本核对"]

    scripts = {
        "Test-MangaLibrary.ps1": pl.SCRIPT_VERIFY,
        "Invoke-LibraryIngest.ps1": pl.SCRIPT_INGEST,
        "Initialize-MangaLayout.ps1": pl.SCRIPT_LAYOUT,
        "Export-MangaLibrary.ps1": pl.SCRIPT_EXPORT,
        "Convert-JmComicToCbz.ps1": pl.SCRIPT_CBZ,
    }
    for mentioned, constant in scripts.items():
        if mentioned not in text:
            problems.append(f"pipeline.md：没提到脚本 {mentioned}")
        if constant != mentioned:
            problems.append(f"pipeline.py：{constant} 与文档里的 {mentioned} 对不上")
        found = False
        for directory in (workspace / "scripts", workspace / "jmcomic"):
            if (directory / mentioned).is_file():
                found = True
                break
        if not found:
            problems.append(f"pipeline.md：工作区里找不到 {mentioned}")

    # 命名正则：文档里引的必须**逐字**包含脚本里的两条内置正则
    verify_script = workspace / "scripts" / "Test-MangaLibrary.ps1"
    if verify_script.is_file():
        source = verify_script.read_text(encoding="utf-8", errors="ignore")
        quoted = re.findall(r"`(\^[^`]*\$)`", text)
        quoted_set = set(quoted)
        for variable, label in (
            ("$NamingPattern", "合规正则"),
            ("$script:FilenameNumberPattern", "编号提取正则"),
        ):
            match = re.search(re.escape(variable) + r"\s*=\s*'([^']+)'", source)
            if not match:
                problems.append(f"pipeline.md：校验器里找不到 {variable}")
                continue
            actual = match.group(1)
            if actual not in quoted_set:
                problems.append(
                    f"pipeline.md：引用的{label}与校验器不一致\n"
                    f"      文档: {'有但内容不同' if any(q.startswith('^') for q in quoted) else '没引用'}\n"
                    f"      脚本: {actual}"
                )

        # naming.md 的「合规 / 违规」清单必须与校验器正则一致
        naming = _read("naming.md")
        for label, expect_ok in (("合规", True), ("违规", False)):
            for line in naming.splitlines():
                stripped = line.strip()
                if not stripped.startswith(("-", "*")) or f"**{label}**" not in stripped:
                    continue
                for sample in re.findall(r"`([^`]*\.cbz)`", stripped):
                    ok = bool(re.match(_dotnet_regex(actual), sample))
                    if ok != expect_ok:
                        problems.append(
                            f"naming.md：{label}清单里的 `{sample}` 与校验器判定不符"
                            f"（正则认为 {'合规' if ok else '违规'}）"
                        )
    return problems


# ── 全局：CLI 子命令 ─────────────────────────────────────────────────────


def check_cli_commands() -> list[str]:
    from manga.cli import app

    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    text = result.stdout or ""

    problems: list[str] = []
    for name in ("paths", "doctor", "verify", "ingest", "convert-cbz", "guard", "search", "album", "comments",
                 "download", "export", "domains", "pick-domain"):
        if name not in text:
            problems.append(f"CLI 里没有子命令 {name}")
    return problems


# ── 全局：references 的「何时读我」提示 ───────────────────────────────────


def check_dispatch_hints() -> list[str]:
    problems: list[str] = []
    for path in sorted(REFERENCES.glob("*.md")):
        head = path.read_text(encoding="utf-8").splitlines()[:3]
        if not any("何时读我" in line for line in head):
            problems.append(f"{path.name}：开头没有「何时读我」分派提示")
    return problems


# ── 汇总 ──────────────────────────────────────────────────────────────────


def check_all() -> list[str]:
    problems: list[str] = []
    for check in (
        check_jmcomic_api,
        check_option_fields,
        check_pipeline,
        check_cli_commands,
        check_dispatch_hints,
    ):
        try:
            problems.extend(check())
        except Exception as exc:  # noqa: BLE001 - 核对器自身出错也要报
            problems.append(f"核对器 {check.__name__} 自身出错：{type(exc).__name__}: {exc}")
    return problems


def main() -> int:
    problems = check_all()
    if problems:
        print("文档声明与代码/脚本不一致：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("文档声明与代码/脚本一致 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
