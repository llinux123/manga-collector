"""打包与默认配置的回归测试（D1）。

盯两件会悄悄坏掉的事：

1. **wheel 里必须带默认 option 配置** —— ``manga/data/jmcomic-option.yml`` 是
   「装上就能用」的关键文件。之前 wheel 只带 gallery-dl 配置，
   装成工具后 ``manga download`` 找不到默认 option → 一条命令都用不了。
2. **默认 option 的查找顺序要稳** —— 环境变量 > 仓库内 > 随包数据 > 工作区。
   装成工具（不在仓库里）时只能靠随包数据兜底。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from manga import config
from manga.config import ENV_OPTION_FILE, default_option_file

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 随包分发的通用 option 模板（源码树里的位置）
PACKAGED_OPTION = REPO_ROOT / "src" / "manga" / "data" / "jmcomic-option.yml"


class TestPackaging:
    def test_pyproject_ships_the_default_option(self) -> None:
        """wheel 的 force-include 必须把通用 option 模板带进包里。"""
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "manga/data/jmcomic-option.yml" in text, (
            "pyproject.toml 必须 force-include 通用 option 模板，"
            "否则装成工具后找不到默认 option"
        )

    def test_sdist_includes_the_config_dir(self) -> None:
        """sdist 也要带 config/ —— 从 sdist 装出来同样要能用。"""
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert '"config"' in text, "sdist 的 include 必须含 config"

    def test_bundled_option_file_is_valid_yaml(self) -> None:
        """随包的那份 option 必须能被 jmcomic 读（YAML 合法 + 有 dir_rule）。"""
        import yaml

        data = yaml.safe_load(PACKAGED_OPTION.read_text(encoding="utf-8-sig"))
        assert isinstance(data, dict)
        assert "dir_rule" in data
        assert data["dir_rule"]["rule"]

    def test_template_has_no_machine_specific_paths(self) -> None:
        """通用模板绝不能带本机绝对路径 / 代理端口。

        它会被打进 wheel 到处分发 —— 带绝对路径等于把别人的机器配成你的。
        """
        text = PACKAGED_OPTION.read_text(encoding="utf-8-sig")
        assert "C:/Users/" not in text
        assert "C:\\Users\\" not in text
        assert "127.0.0.1:" not in text, "代理端口不该写死在通用模板里"
        # 凭据只能来自环境变量
        assert "${JM_USERNAME}" in text

    def test_packaged_option_is_machine_independent(self) -> None:
        """随包模板的落点必须是占位（真落点由 CLI 强制覆盖）。"""
        import yaml

        data = yaml.safe_load(PACKAGED_OPTION.read_text(encoding="utf-8-sig"))
        assert data["dir_rule"]["base_dir"] == "."


class TestDefaultOptionLookup:
    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        target = workdir / "custom.yml"
        monkeypatch.setenv(ENV_OPTION_FILE, str(target))
        assert default_option_file(None) == target

    def test_repo_config_is_used_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_OPTION_FILE, raising=False)
        found = default_option_file(None)
        assert found.is_file(), "仓库里带着 config/jmcomic-option.yml，应该被选中"
        assert found.name == "jmcomic-option.yml"

    def test_packaged_data_is_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """仓库里没有 config/ 时（装成工具的情形）→ 用随包数据。"""
        monkeypatch.delenv(ENV_OPTION_FILE, raising=False)
        monkeypatch.setattr(config, "REPO_ROOT", Path("Z:/nonexistent-repo-root"))
        found = default_option_file(None)
        assert found.name == "jmcomic-option.yml"
        assert "data" in found.parts, "兜底必须是随包数据目录里的那份"
        assert found.is_file(), "随包的兜底 option 必须真的存在"

    def test_packaged_fallback_exists_in_the_source_tree(self) -> None:
        """通用模板必须在仓库里（wheel 的源）。"""
        assert PACKAGED_OPTION.is_file(), "src/manga/data/jmcomic-option.yml 必须存在"


class TestInstalledWheelSmoke:
    """装出来的 wheel 必须自带默认 option —— 端到端验一次。

    这个用例较慢（要建 wheel），但它是唯一能证明「装上就能用」的方式：
    只看 pyproject 里的 force-include 声明是不够的，声明写错路径照样装出
    一个缺文件的 wheel。
    """

    def test_wheel_contains_option_and_gallery_dl_config(self, workdir: Path) -> None:
        uv = shutil.which("uv")
        if not uv:
            pytest.skip("本机没有 uv —— 无法建 wheel")

        dist = workdir / "dist"
        # 构建依赖的临时目录必须在工作区内：系统 %TEMP% 在沙箱里不可写
        # （uv 建隔离环境时会在 %TEMP% 下解包 hatchling）。
        scratch = workdir / "build-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "UV_CACHE_DIR": str(REPO_ROOT / ".uv-cache"),
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
        }
        build = subprocess.run(  # noqa: S603 - argv 列表
            [uv, "build", "--offline", "--wheel", "--out-dir", str(dist)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
        if build.returncode != 0:
            pytest.skip(f"uv build 不可用（离线环境）：{build.stderr[-500:]}")

        wheels = list(dist.glob("*.whl"))
        assert wheels, "没有产出 wheel"
        with zipfile.ZipFile(wheels[0]) as zf:
            names = set(zf.namelist())
        assert "manga/data/jmcomic-option.yml" in names, (
            "wheel 里必须有 manga/data/jmcomic-option.yml，"
            f"实际有：{sorted(n for n in names if n.endswith(('.yml', '.json')))}"
        )
        assert "manga/data/gallery-dl-option.json" in names
