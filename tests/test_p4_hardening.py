"""P4 加固项的回归测试（全部离线）。

盯三件容易悄悄坏掉的事：

1. **优雅停机**：jmcomic 只定义、从不自动调 ``wait_all_plugins_finish()`` ——
   我们接了，就必须一直接着；
2. **日志治理**：jmcomic 的进度日志写的是**相对路径** → 落点就是 CWD，
   仓库根不该出现 ``jmcomic-download.log``；
3. **不自动装依赖**：CLI 只给安装命令，绝不自己 pip install。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from manga import runtime
from manga.cli import _run_in_log_dir
from manga.config import resolve_paths

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPluginShutdown:
    def test_finish_plugins_waits_for_registered_plugins(self) -> None:
        """登记在 need_wait_plugins 的插件必须被等到。"""
        waited: list[str] = []

        class _Plugin:
            plugin_key = "async_demo"

            def wait_until_finish(self) -> None:
                waited.append("async_demo")

        class _Option:
            need_wait_plugins = [_Plugin()]

            def wait_all_plugins_finish(self) -> None:
                for plugin in self.need_wait_plugins:
                    plugin.wait_until_finish()

        assert runtime.finish_plugins(_Option()) == ["async_demo"]
        assert waited == ["async_demo"]

    def test_finish_plugins_is_noop_without_async_plugins(self) -> None:
        class _Option:
            need_wait_plugins: list = []

            def wait_all_plugins_finish(self) -> None:
                return None

        assert runtime.finish_plugins(_Option()) == []

    def test_finish_plugins_survives_missing_method(self) -> None:
        """老版本 jmcomic 没有这个方法 → 不能炸。"""
        class _Old:
            pass

        assert runtime.finish_plugins(_Old()) == []

    def test_finish_plugins_survives_plugin_error(self) -> None:
        """插件收工时抛异常 → 不能盖掉命令本身的结果。"""
        class _Option:
            need_wait_plugins: list = []

            def wait_all_plugins_finish(self) -> None:
                raise RuntimeError("插件炸了")

        assert runtime.finish_plugins(_Option()) == []

    def test_finish_plugins_handles_none(self) -> None:
        assert runtime.finish_plugins(None) == []


class TestLogGovernance:
    def test_command_runs_in_log_dir(self, library_root: Path) -> None:
        """调 jmcomic 前必须切到固定日志目录 —— 相对路径的日志才不会乱跑。"""
        p = resolve_paths(library_root=library_root)
        seen: dict[str, str] = {}

        def _work() -> dict:
            seen["cwd"] = os.getcwd()
            return {"ok": True}

        _run_in_log_dir(p, _work)
        assert Path(seen["cwd"]).resolve() == p.logs.resolve()
        # 切完要切回来（不能把调用方的 CWD 留在日志目录里）
        assert Path(os.getcwd()).resolve() != p.logs.resolve()

    def test_fallback_when_log_dir_unwritable(self, library_root: Path) -> None:
        """日志目录建不出来 → 退到别处 + 给 warning，而不是让命令崩掉。"""
        p = resolve_paths(library_root=library_root)
        seen: dict[str, str] = {}

        def _work() -> dict:
            seen["cwd"] = os.getcwd()
            return {"ok": True}

        original = runtime.working_directory

        def _fake(path):
            from contextlib import nullcontext

            return nullcontext(None)

        runtime.working_directory = _fake  # type: ignore[assignment]
        try:
            data, warnings = _run_in_log_dir(p, _work)
        finally:
            runtime.working_directory = original  # type: ignore[assignment]

        assert data == {"ok": True}
        assert warnings and "不可写" in warnings[0]

    def test_repo_root_has_no_jmcomic_log(self) -> None:
        """仓库根不该出现 jmcomic-download.log（那是「日志治理」失效的信号）。"""
        stale = REPO_ROOT / "jmcomic-download.log"
        assert not stale.exists(), f"仓库根出现了 {stale.name} —— 日志治理失效了"


class TestNoAutoInstall:
    def test_dependency_report_only_gives_commands(self) -> None:
        """依赖探测**只给命令**，绝不自己装。"""
        report = runtime.dependency_report()
        assert "missing_modules" in report
        for entry in report["missing_modules"]:
            assert entry["install"].startswith("uv pip install")
            assert "usage" in entry
        for entry in report.get("missing_executables", []):
            assert entry["install"]
        assert "不会自动装" in report["install_hint"]

    def test_cli_never_pip_installs(self) -> None:
        """源码里不能出现自动安装的调用。"""
        forbidden = ("pip install", "uv pip install", "ensurepip", "pip.main")
        for path in (REPO_ROOT / "src" / "manga").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                # 只允许出现在**提示字符串**里（给用户看的命令），不能是真正的调用
                for line in text.splitlines():
                    if needle in line and ("subprocess" in line or "run(" in line or "check_call" in line):
                        pytest.fail(f"{path.name} 里疑似自动装依赖：{line.strip()}")
