"""CI workflow 的回归测试（D3）。

CI 是「防止悄悄坏掉」的最后一层闸门 —— 它自己坏了就等于没有闸门。
盯四件事：

1. **CI 文件存在**且 YAML 合法；
2. **跑测试**（不是「跑一半」）；
3. **跑两个事实核对器**（references / jmcomic 事实表漂移会当场报）；
4. CI 里的 workflow **自己也过红线与注入自检**（不能用一份坏模板去守门）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from manga.actions_gen import validate_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _trigger(doc: dict) -> dict:
    for key in ("on", True):
        if key in doc:
            value = doc[key]
            return value if isinstance(value, dict) else {}
    return {}


class TestCiWorkflowExists:
    def test_ci_file_is_present(self) -> None:
        assert CI_PATH.is_file(), f"CI workflow 不存在：{CI_PATH}"

    def test_ci_yaml_parses(self) -> None:
        doc = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
        assert isinstance(doc, dict)
        assert doc.get("jobs"), "CI 没有 jobs"

    def test_ci_triggers_on_push_and_pull_request(self) -> None:
        doc = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
        trigger = _trigger(doc)
        assert "push" in trigger
        assert "pull_request" in trigger

    def test_ci_pins_read_only_permissions(self) -> None:
        doc = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
        assert doc.get("permissions") == {"contents": "read"}


class TestCiRunsTheGates:
    def _runs(self) -> str:
        doc = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
        chunks: list[str] = []
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                chunks.append(str(step.get("run", "")))
                chunks.append(str(step.get("uses", "")))
        return "\n".join(chunks)

    def test_ci_runs_pytest(self) -> None:
        assert "pytest" in self._runs(), "CI 必须跑 pytest"

    def test_ci_runs_both_fact_checks(self) -> None:
        runs = self._runs()
        assert "tools/doc_claims.py check" in runs, "CI 必须跑文档声明核对"
        assert "tools/jmcomic_facts.py check" in runs, "CI 必须跑 jmcomic 事实核对"

    def test_ci_validates_generated_workflows(self) -> None:
        """CI 自己也得守门：生成的 workflow 必须过红线与注入自检。"""
        assert "actions-gen --check" in self._runs()

    def test_ci_does_not_run_live_downloads(self) -> None:
        """CI 里绝不能有真实下载（灰色来源 + 无凭据 → 必然失败）。"""
        runs = self._runs()
        for forbidden in ("manga download", "manga gallery-dl", "manga mangadex-dl"):
            assert forbidden not in runs, f"CI 里不该出现真实下载：{forbidden}"


class TestCommittedWorkflowsAreClean:
    """仓库里**已提交**的 workflow 也得过自检（红线 + 脚本注入）。"""

    def _workflow_dir(self) -> Path:
        return REPO_ROOT / ".github" / "workflows"

    def test_committed_workflows_have_no_input_injection(self) -> None:
        for path in sorted(self._workflow_dir().glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in doc.get("jobs", {}).values():
                for step in job.get("steps", []):
                    assert "${{ inputs." not in str(step.get("run", "")), (
                        f"{path.name}: run: 里出现了 ${{{{ inputs. ... }}}} —— 脚本注入风险"
                    )

    def test_generated_workflows_pass_the_validator(self) -> None:
        """生成器产出的模板必须自检通过（含新增的注入检查）。"""
        for path in sorted(self._workflow_dir().glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            report = validate_workflow(text)
            assert report["ok"], f"{path.name} 自检不过：{report['errors']}"
