"""references 与上游源码的**同步性**测试（P2 校准的守护）。

P2 的验收标准是「每个可配置字段、每个插件 kwarg 都能在 references 里查到」。
但 references 是手写 markdown —— 上游 jmcomic 一升级就会悄悄腐烂。
所以把「事实」变成可执行断言：jmcomic 升级后跑测试，漂移会当场暴露。

跳过条件：没装 jmcomic 的环境（比如只装 dev 依赖的 CI）直接 skip，不算失败。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

jmcomic_facts = pytest.importorskip("jmcomic_facts", reason="需要 tools/jmcomic_facts.py")


class TestReferencesInSync:
    def test_no_drift_against_installed_jmcomic(self) -> None:
        problems = jmcomic_facts.check_references()
        assert not problems, "references 与源码不一致：\n" + "\n".join(f"  - {p}" for p in problems)

    def test_all_registered_plugins_are_documented(self) -> None:
        facts = jmcomic_facts.plugin_facts()
        assert len(facts) == 21, f"内置插件数变了（{len(facts)} 个），references 需要更新"
        text = (jmcomic_facts.REFERENCES / "option-and-plugins.md").read_text(encoding="utf-8")
        for fact in facts:
            assert f"`{fact['key']}`" in text, f"插件 {fact['key']} 没写进 option-and-plugins.md"

    def test_dirrule_variables_are_documented(self) -> None:
        facts = jmcomic_facts.dirrule_facts()
        documented = jmcomic_facts.documented_dirrule_vars()
        missing = sorted(set(facts["all"]) - documented)
        assert not missing, f"naming.md 变量表漏了：{', '.join(missing)}"

    def test_no_phantom_variables_in_docs(self) -> None:
        facts = jmcomic_facts.dirrule_facts()
        documented = jmcomic_facts.documented_dirrule_vars()
        unknown = sorted(documented - set(facts["all"]))
        assert not unknown, f"naming.md 写了源码里不存在的变量：{', '.join(unknown)}"

    def test_known_typo_is_not_reintroduced(self) -> None:
        """`Aaidoname` 是历史拼写错误，正确的只有 `Aidoname`。"""
        text = (jmcomic_facts.REFERENCES / "naming.md").read_text(encoding="utf-8")
        table = text.split("变量表", 1)[-1]
        # 允许出现在「易错点」的说明里，但不能出现在变量表格行里
        for line in table.splitlines():
            if not line.strip().startswith("|"):
                continue
            assert "Aaidoname" not in line, f"变量表里又出现了错误拼写：{line.strip()}"

    def test_broken_dirrule_form_is_not_recommended(self) -> None:
        """`Bd_Atitle/Pname`（下划线前缀 + 斜杠分隔，混用）会静默污染库。

        注意只针对**混用**形式：
        `Bd_Atitle_Pname`（全下划线）是合法的 —— 规则里没有 `/` 时就按 `_` 切段。

        历史教训：坏写法先出现在 pipeline.md，又在 troubleshooting.md 里活了很久，
        每次「人工校一遍」都可能只改一处 → 所以做成测试。
        """
        mixed = re.compile(r"Bd_Atitle/")
        for path in sorted(jmcomic_facts.REFERENCES.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if not mixed.search(stripped):
                    continue
                # 只允许出现在「反例 / 易错点」语境里
                assert any(
                    marker in stripped
                    for marker in ("❌", "错", "不能", "别", "禁止", "实为", "不是", "污染", "字面量")
                ), f"{path.name} 把错误写法当成了示例：{stripped}"

    def test_no_mojibake_in_references(self) -> None:
        """乱码（例如「打���」）说明文件被截断过，必须当场发现。"""
        for path in sorted(jmcomic_facts.REFERENCES.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            assert "�" not in text, f"{path.name} 里有乱码字符（U+FFFD）"


class TestDocClaims:
    """文档里的**事实性声明**（API/常量/异常/脚本/正则/CLI 命令）必须与代码一致。

    人工复核最容易漏的就是「看起来对、其实已经改名或不存在」的东西。
    """

    def test_doc_claims_match_code_and_scripts(self) -> None:
        doc_claims = pytest.importorskip("doc_claims", reason="需要 tools/doc_claims.py")
        problems = doc_claims.check_all()
        assert not problems, "文档声明与代码/脚本不一致：\n" + "\n".join(f"  - {p}" for p in problems)

    def test_every_reference_has_dispatch_hint(self) -> None:
        doc_claims = pytest.importorskip("doc_claims")
        problems = doc_claims.check_dispatch_hints()
        assert not problems, "\n".join(problems)
