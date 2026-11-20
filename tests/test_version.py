"""版本号单一来源测试。

之前版本号写死在两处（``pyproject.toml`` 与 ``src/manga/__init__.py``），
改一处忘另一处 → ``manga version`` 报 0.2.0，装好的包元数据却是 0.1.0。
现在版本只认**发行元数据**（``importlib.metadata``），源码树里跑时用兜底常量。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from manga import DIST_NAME, __version__
from manga.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml 里必须有 version"
    return match.group(1)


class TestSingleSourceVersion:
    def test_runtime_version_matches_pyproject(self) -> None:
        """运行时版本必须与 pyproject.toml 一致。"""
        assert __version__ == _pyproject_version()

    def test_fallback_constant_matches_pyproject(self) -> None:
        """源码树里直接跑（没装成包）时靠这个兜底常量 —— 也必须一致。"""
        from manga import _FALLBACK_VERSION

        assert _FALLBACK_VERSION == _pyproject_version()

    def test_version_is_not_duplicated_in_source(self) -> None:
        """源码里不允许再出现第二个硬编码版本号。"""
        init_text = (REPO_ROOT / "src" / "manga" / "__init__.py").read_text(encoding="utf-8")
        # 只允许 _FALLBACK_VERSION 这一处常量
        assignments = re.findall(r'__version__\s*=\s*"([^"]+)"', init_text)
        assert assignments == [], "版本号只能来自 importlib.metadata，不要再硬编码"

    def test_cli_version_reports_the_same(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["data"]["version"] == __version__

    def test_dist_name_matches_pyproject_name(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
        assert match
        assert DIST_NAME == match.group(1)
