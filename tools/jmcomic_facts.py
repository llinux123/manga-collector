"""从**安装的 jmcomic 源码**里提取权威事实，供 references 校准与回归校验。

P2 的验收标准是「每个可配置字段、每个插件 kwarg 都能在 references 里查到」，
但 references 是**手写 markdown** —— 上游一升级就会悄悄腐烂。
所以把「事实」变成可执行的东西：

* ``uv run python tools/jmcomic_facts.py plugins`` —— 打印 21 个插件的
  plugin_key / 类名 / 依赖 / invoke 形参（Markdown 表格，可直接粘进 reference）；
* ``uv run python tools/jmcomic_facts.py dirrule`` —— 打印 DirRule 里
  真的能用的变量（A 前缀 + P 前缀），以及与 ``references/naming.md`` 的差异；
* ``uv run python tools/jmcomic_facts.py check`` —— 校验 references 是否与源码一致，
  有漂移就非零退出（``tests/test_reference_facts.py`` 调它）。

全部只读 introspection：不联网、不实例化实体、不碰磁盘。
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = REPO_ROOT / ".agents" / "skills" / "manga" / "references"

#: 插件框架**注入**的参数 —— 由钩子调用方传，不是用户在 YAML 里配的。
#: 校验 references 时不要求逐个列出（但文档里应说明它们从哪来）。
FRAMEWORK_INJECTED = frozenset(
    {
        "album",
        "photo",
        "downloader",
        "image",
        "extra",
        "pinfo",
        "option",
        "self",
    }
)


# ── 插件 ──────────────────────────────────────────────────────────────────


def plugin_facts() -> list[dict[str, Any]]:
    """每个已注册插件的事实（来源：``JmModuleConfig.REGISTRY_PLUGIN``）。"""
    from jmcomic import jm_plugin

    facts: list[dict[str, Any]] = []
    registry: dict[str, type] = dict(jm_plugin.JmModuleConfig.REGISTRY_PLUGIN)
    for key, cls in sorted(registry.items()):
        deps = getattr(cls, "plugin_dependencies", ())
        dep_text = ", ".join(
            d if isinstance(d, str) else f"{d[0]}{('==' + d[1]) if len(d) > 1 else ''}" for d in deps
        )

        dynamic = ""
        required = getattr(cls, "required_dependencies_for", None)
        if callable(required):
            try:
                result = required({})
                dynamic = ", ".join(
                    d if isinstance(d, str) else f"{d[0]}{('==' + d[1]) if len(d) > 1 else ''}"
                    for d in result
                )
            except Exception:  # noqa: BLE001 - 探测失败就当没有
                dynamic = ""

        kwargs: list[str] = []
        accepts_kwargs = False
        try:
            signature = inspect.signature(cls.invoke)
            for name, param in signature.parameters.items():
                if name == "self":
                    continue
                if param.kind is inspect.Parameter.VAR_KEYWORD:
                    accepts_kwargs = True
                    continue
                if param.kind is inspect.Parameter.VAR_POSITIONAL:
                    continue
                default = "（必填）" if param.default is inspect.Parameter.empty else repr(param.default)
                kwargs.append(f"{name}={default}" if param.default is not inspect.Parameter.empty else f"{name}（必填）")
        except (TypeError, ValueError):  # pragma: no cover
            kwargs = ["<签名不可读>"]

        doc = (cls.__doc__ or "").strip().splitlines()
        facts.append(
            {
                "key": key,
                "class": cls.__name__,
                "deps": dep_text,
                "dynamic_deps": dynamic,
                "kwargs": kwargs,
                "accepts_kwargs": accepts_kwargs,
                "summary": doc[0].strip() if doc else "",
            }
        )
    return facts


def plugin_table_markdown() -> str:
    """21 个插件的 Markdown 表格（可直接粘进 reference）。"""
    lines = [
        "| plugin_key | 类 | invoke 形参（源码校对） | 依赖 |",
        "|---|---|---|---|",
    ]
    for fact in plugin_facts():
        kwargs = ", ".join(fact["kwargs"]) or "（无显式参数）"
        if fact["accepts_kwargs"]:
            kwargs += "，**另收 **kwargs"
        deps = fact["deps"] or "—"
        if fact["dynamic_deps"]:
            deps += f"（按参数动态：{fact['dynamic_deps']}）"
        lines.append(f"| `{fact['key']}` | {fact['class']} | {kwargs} | {deps} |")
    return "\n".join(lines)


# ── DirRule 变量 ──────────────────────────────────────────────────────────


def _prefixed_props(cls: type, prefix: str) -> set[str]:
    """实体类在 DirRule 里能暴露的变量名（前缀化）。

    口径与 ``JmBaseEntity.get_properties_dict()`` 对齐：实例字段 + MRO 上的 property。
    实例字段靠扫 ``__init__`` 里的 ``self.X =`` 取（比签名全：
    ``index`` 这类就是赋值出来的）。
    """
    names: set[str] = set()
    try:
        source = inspect.getsource(cls.__init__)
        for name in re.findall(r"self\.(\w+)\s*(?::[^=]+)?=", source):
            names.add(prefix + name)
    except (OSError, TypeError):  # pragma: no cover
        pass
    for name in inspect.signature(cls.__init__).parameters:
        if name != "self":
            names.add(prefix + name)
    for klass in inspect.getmro(cls):
        for name, attr in klass.__dict__.items():
            if isinstance(attr, property):
                names.add(prefix + name)
    return names


def dirrule_facts() -> dict[str, Any]:
    """DirRule 可用变量的事实。"""
    from jmcomic import jm_entity

    album = _prefixed_props(jm_entity.JmAlbumDetail, "A")
    photo = _prefixed_props(jm_entity.JmPhotoDetail, "P")
    return {
        "album": sorted(album),
        "photo": sorted(photo),
        "all": sorted(album | photo),
    }


def documented_dirrule_vars(path: Path | None = None) -> set[str]:
    """naming.md「变量表」一节里写下的变量名。"""
    path = path or (REFERENCES / "naming.md")
    if not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if "变量表" in line and line.lstrip().startswith("#")),
        None,
    )
    if start is None:
        return set()
    found: set[str] = set()
    for line in lines[start + 1 :]:
        if line.lstrip().startswith("#"):
            break
        if not line.strip().startswith("|"):
            continue
        for cell in line.strip().strip("|").split("|"):
            found.update(re.findall(r"\{?([AP][A-Za-z_][A-Za-z0-9_]*)\}?", cell))
    return found


# ── 校验 ──────────────────────────────────────────────────────────────────


def check_references() -> list[str]:
    """比对 references 与源码，返回漂移清单（空 = 一致）。"""
    problems: list[str] = []

    plugin_doc = REFERENCES / "option-and-plugins.md"
    if plugin_doc.is_file():
        text = plugin_doc.read_text(encoding="utf-8")
        for fact in plugin_facts():
            if f"`{fact['key']}`" not in text:
                problems.append(f"option-and-plugins.md：缺插件 {fact['key']}")
            for kwarg in fact["kwargs"]:
                name = kwarg.split("=")[0].split("（")[0].strip()
                if not name or name.startswith("<"):
                    continue
                if name in FRAMEWORK_INJECTED:
                    continue  # 框架注入，不需要在文档里当用户参数列
                if name not in text:
                    problems.append(f"option-and-plugins.md：插件 {fact['key']} 缺参数 {name}")

    facts = dirrule_facts()
    documented = documented_dirrule_vars()
    missing = sorted(set(facts["all"]) - documented)
    if missing:
        problems.append(f"naming.md：变量表缺 {len(missing)} 个源码可用变量 → {', '.join(missing)}")
    unknown = sorted(documented - set(facts["all"]))
    if unknown:
        problems.append(f"naming.md：变量表写了源码里没有的 {len(unknown)} 个 → {', '.join(unknown)}")

    return problems


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else "plugins"

    if what == "plugins":
        import jmcomic

        print(f"# jmcomic {jmcomic.__version__} · 内置插件 {len(plugin_facts())} 个")
        print()
        print(plugin_table_markdown())
    elif what == "dirrule":
        facts = dirrule_facts()
        print(f"# A {len(facts['album'])} + P {len(facts['photo'])} = 共 {len(facts['all'])} 个")
        print()
        for name in facts["all"]:
            print(f"- {name}")
        print()
        documented = documented_dirrule_vars()
        print("## 与 naming.md 的差异")
        print(f"- 文档缺：{', '.join(sorted(set(facts['all']) - documented)) or '（无）'}")
        print(f"- 文档多：{', '.join(sorted(documented - set(facts['all']))) or '（无）'}")
    elif what == "check":
        problems = check_references()
        if problems:
            print("references 与源码不一致：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print("references 与源码一致 ✅")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
