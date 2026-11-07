"""GitHub Actions workflow 生成器的单元测试（不联网、不写盘）。"""

from __future__ import annotations

import json
import re

import yaml

from manga.actions_gen import (
    REQUIRED_DISPATCH_INPUTS,
    generate_workflows,
    secrets_manifest,
    validate_workflow,
    validate_workflows,
    write_workflows,
)
from manga.envelope import MangaError


def _trigger(doc: dict) -> dict:
    """取出 ``on:`` 段。

    ⚠ PyYAML 用的是 YAML 1.1 解析器：裸键 ``on`` 被解析成布尔 ``True``，
    所以 ``yaml.safe_load(text)["on"]`` 会 KeyError —— 两种都得认。
    """
    for key in ("on", True):
        if key in doc:
            value = doc[key]
            return value if isinstance(value, dict) else {}
    return {}


class TestGeneratedYaml:
    def test_both_workflows_are_generated(self) -> None:
        files = generate_workflows()
        assert set(files) == {"download_dispatch.yml", "export_favorites.yml"}
        assert all(text.strip() for text in files.values())

    def test_yaml_parses(self) -> None:
        for name, text in generate_workflows().items():
            doc = yaml.safe_load(text)
            assert isinstance(doc, dict), f"{name} 顶层不是映射"
            assert doc.get("name"), f"{name} 缺少 name"

    def test_download_dispatch_has_exactly_ten_inputs(self) -> None:
        text = generate_workflows()["download_dispatch.yml"]
        doc = yaml.safe_load(text)
        inputs = _trigger(doc)["workflow_dispatch"]["inputs"]
        # 用解析结果数，不是数字符串
        assert len(inputs) == 10
        assert len(REQUIRED_DISPATCH_INPUTS) == 10
        assert set(inputs) == set(REQUIRED_DISPATCH_INPUTS)

    def test_every_input_has_description_required_default_type(self) -> None:
        text = generate_workflows()["download_dispatch.yml"]
        doc = yaml.safe_load(text)
        inputs = _trigger(doc)["workflow_dispatch"]["inputs"]
        for name, spec in inputs.items():
            assert isinstance(spec, dict), name
            for key in ("description", "required", "default", "type"):
                assert key in spec, f"{name} 缺少 {key}"
            assert spec["type"] in ("boolean", "choice", "number", "string")

    def test_workflow_dispatch_trigger_exists(self) -> None:
        for name, text in generate_workflows().items():
            doc = yaml.safe_load(text)
            trigger = _trigger(doc)
            assert "workflow_dispatch" in trigger, f"{name} 缺少 on.workflow_dispatch"

    def test_export_favorites_has_schedule_and_dispatch(self) -> None:
        doc = yaml.safe_load(generate_workflows()["export_favorites.yml"])
        trigger = _trigger(doc)
        assert "schedule" in trigger
        crons = [entry["cron"] for entry in trigger["schedule"]]
        assert crons and all(re.fullmatch(r"\S+ \S+ \S+ \S+ \S+", cron) for cron in crons)
        assert "workflow_dispatch" in trigger

    def test_download_dispatch_has_steps_structure(self) -> None:
        doc = yaml.safe_load(generate_workflows()["download_dispatch.yml"])
        jobs = doc["jobs"]
        assert jobs
        steps = next(iter(jobs.values()))["steps"]
        assert steps, "steps 为空"
        # 装工具 → 跑 manga CLI → 上传产物
        assert any("actions/checkout" in str(step.get("uses", "")) for step in steps)
        assert any("manga " in str(step.get("run", "")) for step in steps)
        assert any("actions/upload-artifact" in str(step.get("uses", "")) for step in steps)


class TestRedLine:
    def test_grey_source_is_never_uploaded(self) -> None:
        text = generate_workflows()["download_dispatch.yml"]
        doc = yaml.safe_load(text)
        steps = next(iter(doc["jobs"].values()))["steps"]
        uploads = [s for s in steps if "actions/upload-artifact" in str(s.get("uses", ""))]
        assert uploads, "没有上传步骤"
        for step in uploads:
            assert step.get("if"), "上传步骤没有条件判断"
            assert "jmcomic" in str(step["if"]), "上传步骤没有排除灰色来源"

    def test_grey_source_redline_is_written_down(self) -> None:
        text = generate_workflows()["download_dispatch.yml"]
        assert "红线" in text
        assert "Export" in text and "发布" in text


class TestSecretsManifest:
    def test_manifest_is_not_empty(self) -> None:
        manifest = secrets_manifest()
        assert manifest
        for item in manifest:
            assert item["name"]
            assert item["description"]
            assert isinstance(item["required"], bool)

    def test_manifest_has_expected_secret_names(self) -> None:
        names = {item["name"] for item in secrets_manifest()}
        for expected in (
            "MANGA_LIBRARY_ROOT",
            "MANGA_OPTION_FILE",
            "JM_USERNAME",
            "JM_PASSWORD",
            "KOMGA_API_KEY",
        ):
            assert expected in names

    def test_manifest_contains_no_real_credentials(self) -> None:
        allowed_keys = {"name", "description", "required", "used_by"}
        for item in secrets_manifest():
            assert set(item) <= allowed_keys, f"出现了不该有的字段：{set(item) - allowed_keys}"
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", item["name"])

        blob = json.dumps(secrets_manifest(), ensure_ascii=False)
        # 明显是「凭据值」的样式：token / key / 密码的赋值
        assert not re.search(
            r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|cookie|bearer)\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9+/_\-]{8,}",
            blob,
        ), "secrets 清单里出现了疑似凭据值"
        for pattern in (r"ghp_[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{12,}", r"-----BEGIN"):
            assert not re.search(pattern, blob), f"出现了疑似真实凭据：{pattern}"

    def test_manifest_has_no_value_field(self) -> None:
        for item in secrets_manifest():
            assert "value" not in item
            assert "secret" not in item


class TestValidateWorkflow:
    def test_generated_workflows_are_valid(self) -> None:
        report = validate_workflows()
        assert report["ok"], report["reports"]
        assert report["reports"]["download_dispatch.yml"]["input_count"] == 10

    def test_valid_workflow_returns_ok(self) -> None:
        report = validate_workflow(generate_workflows()["download_dispatch.yml"])
        assert report["ok"], report["errors"]
        assert report["errors"] == []
        assert report["input_count"] == 10
        assert "workflow_dispatch" in report["triggers"]

    def test_broken_yaml_returns_not_ok(self) -> None:
        report = validate_workflow("on: workflow_dispatch\n  inputs: [oops\n")
        assert report["ok"] is False
        assert report["errors"]

    def test_missing_workflow_dispatch_is_an_error(self) -> None:
        report = validate_workflow("name: x\non:\n  push:\n    branches: [main]\njobs: {}\n")
        assert report["ok"] is False
        assert any("workflow_dispatch" in err for err in report["errors"])

    def test_wrong_input_count_is_an_error(self) -> None:
        doc = {
            "name": "x",
            "on": {"workflow_dispatch": {"inputs": {"source": {"description": "d", "required": True, "default": "a", "type": "string"}}}},
            "jobs": {"j": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo hi"}]}},
        }
        report = validate_workflow(yaml.safe_dump(doc))
        assert report["ok"] is False
        assert any("inputs" in err for err in report["errors"])

    def test_non_mapping_is_an_error(self) -> None:
        report = validate_workflow("- just\n- a\n- list\n")
        assert report["ok"] is False


class TestWriteWorkflows:
    def test_dry_run_writes_nothing(self, workdir) -> None:
        result = write_workflows(workdir)
        assert result["dry_run"] is True
        assert result["written"] == []
        assert not (workdir / ".github").exists()

    def test_apply_writes_both_files(self, workdir) -> None:
        result = write_workflows(workdir, apply=True)
        assert result["dry_run"] is False
        assert len(result["written"]) == 2
        for name in ("download_dispatch.yml", "export_favorites.yml"):
            path = workdir / ".github" / "workflows" / name
            assert path.is_file()
            assert yaml.safe_load(path.read_text(encoding="utf-8"))


class TestSelfCheck:
    def test_generate_workflows_validates_itself(self, monkeypatch) -> None:
        """坏模板必须抛 MangaError，而不是把坏 YAML 交出去。"""
        import manga.actions_gen as actions_gen

        monkeypatch.setattr(actions_gen, "_DOWNLOAD_WORKFLOW", "on: [broken\n")
        try:
            actions_gen.generate_workflows()
        except MangaError as exc:
            assert exc.code == "WORKFLOW_INVALID"
            assert exc.exit_code == 2
        else:  # pragma: no cover - 只有坏模板才会走到
            raise AssertionError("坏模板没有抛 MangaError")
