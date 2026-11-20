"""运行时能力测试（P4 加固）：版本下限 · 登录态 · 依赖探测 · 工作目录。

全部离线：不联网、不安装任何东西、不写业务目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from manga import runtime


def _write_option(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ── 版本下限 ──────────────────────────────────────────────────────────────


class TestVersion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2.7.11", (2, 7, 11)),
            ("v2.7.11", (2, 7, 11)),
            ("2.7.11.dev0", (2, 7, 11)),
            ("2.7", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse_version(self, raw, expected) -> None:
        assert runtime.parse_version(raw) == expected

    def test_current_is_new_enough(self) -> None:
        assert runtime.version_at_least("2.7.11", "2.7.11") is True
        assert runtime.version_at_least("2.8.0", "2.7.11") is True

    def test_older_version_is_rejected(self) -> None:
        assert runtime.version_at_least("2.6.0", "2.7.11") is False

    def test_unparsable_version_is_unknown_not_ok(self) -> None:
        """版本号读不出来 → 不可判（None），不能当成「够新」。"""
        assert runtime.version_at_least(None, "2.7.11") is None
        assert runtime.version_at_least("未知", "2.7.11") is None


# ── 登录态 ────────────────────────────────────────────────────────────────


class TestAuthState:
    def test_cookies_plus_user_id_means_logged_in(self, workdir: Path) -> None:
        option = _write_option(
            workdir / "option.yml",
            "client:\n  impl: api\nauth:\n  cookies:\n    AVS: abc\n  user_id: 12345\n",
        )
        state = runtime.auth_state(option)
        assert state["logged_in"] is True
        assert state["has_cookies"] is True
        assert state["impl"] == "api"

    def test_cookies_without_identity_is_not_logged_in(self, workdir: Path) -> None:
        option = _write_option(
            workdir / "option.yml",
            "client:\n  impl: api\nauth:\n  cookies:\n    AVS: abc\n",
        )
        state = runtime.auth_state(option)
        assert state["logged_in"] is False
        assert "user_id" in state["detail"]

    def test_credentials_alone_are_not_logged_in(self, workdir: Path) -> None:
        """账号密码只是凭据，不是登录态。"""
        option = _write_option(
            workdir / "option.yml",
            "client:\n  impl: api\nauth:\n  username: u\n  password: p\n",
        )
        state = runtime.auth_state(option)
        assert state["logged_in"] is False
        assert state["has_username"] is True
        assert state["has_password"] is True
        assert "自动登录" in state["detail"]

    def test_html_impl_uses_username_as_identity(self, workdir: Path) -> None:
        option = _write_option(
            workdir / "option.yml",
            "client:\n  impl: html\nauth:\n  cookies:\n    AVS: abc\n  username: u\n",
        )
        state = runtime.auth_state(option)
        assert state["logged_in"] is True

    def test_env_placeholder_is_resolved(self, workdir: Path, monkeypatch) -> None:
        monkeypatch.setenv("JM_USERNAME", "envuser")
        monkeypatch.delenv("JM_PASSWORD", raising=False)
        option = _write_option(
            workdir / "option.yml",
            "auth:\n  username: ${JM_USERNAME}\n  password: ${JM_PASSWORD}\n",
        )
        state = runtime.auth_state(option)
        assert state["has_username"] is True
        assert state["has_password"] is False

    def test_broken_option_file_does_not_crash(self, workdir: Path) -> None:
        option = _write_option(workdir / "option.yml", "client: [1, 2\n")
        state = runtime.auth_state(option)
        assert state["logged_in"] is False
        assert state["detail"]


# ── 依赖探测 ──────────────────────────────────────────────────────────────


class TestDependencyReport:
    def test_report_shape(self) -> None:
        report = runtime.dependency_report()
        assert isinstance(report["missing_modules"], list)
        assert isinstance(report["missing_executables"], list)
        assert "install_hint" in report

    def test_standard_modules_are_installed(self) -> None:
        assert runtime.module_installed("json") is True
        assert runtime.missing_modules(["json"]) == []

    def test_missing_module_is_listed_with_install_command(self) -> None:
        report = runtime.dependency_report()
        for item in report["missing_modules"]:
            assert item["install"].startswith("uv pip install ")
            assert item["pip"] and item["usage"]


# ── 工作目录 ──────────────────────────────────────────────────────────────


class TestWorkingDirectory:
    def test_cwd_is_switched_and_restored(self, workdir: Path) -> None:
        before = Path.cwd()
        target = workdir / "logs"
        with runtime.working_directory(target) as inside:
            assert Path.cwd() == target.resolve()
            assert inside == target
        assert Path.cwd() == before

    def test_cwd_is_restored_after_error(self, workdir: Path) -> None:
        before = Path.cwd()
        with pytest.raises(RuntimeError):
            with runtime.working_directory(workdir / "logs"):
                raise RuntimeError("炸了")
        assert Path.cwd() == before

    def test_target_is_created(self, workdir: Path) -> None:
        target = workdir / "deep" / "logs"
        with runtime.working_directory(target):
            assert target.is_dir()

    def test_unwritable_target_degrades_instead_of_crashing(self, workdir: Path) -> None:
        """目录建不出来时不能崩 —— 沙箱/只读环境下整条命令不该因此挂掉。"""
        blocker = workdir / "blocker"
        blocker.write_text("我是个文件，不是目录", encoding="utf-8")
        before = Path.cwd()
        with runtime.working_directory(blocker / "logs") as used:
            assert used is None
        assert Path.cwd() == before

    def test_relative_log_would_land_in_target(self, workdir: Path) -> None:
        """jmcomic 的进度日志是相对路径 → 落点必须跟着 CWD 走。"""
        target = workdir / "logs"
        with runtime.working_directory(target):
            (Path("jmcomic-download.log")).write_text("x", encoding="utf-8")
        assert (target / "jmcomic-download.log").is_file()
        assert not (workdir / "jmcomic-download.log").exists()
        assert os.getcwd() != str(target)

    def test_read_only_directory_is_detected(self, workdir: Path, monkeypatch) -> None:
        """目录能进但**不能写**时不能当成可用 —— 那样日志会静默丢失。

        只 mkdir + chdir 是不够的：Windows 上目录 ACL 可以允许进入、禁止新建文件。
        """
        target = workdir / "readonly"
        target.mkdir()

        real_write_text = Path.write_text

        def fake_write_text(self, *args, **kwargs):
            if self.parent == target:
                raise PermissionError(13, "Permission denied")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fake_write_text)
        before = Path.cwd()
        with runtime.working_directory(target) as used:
            assert used is None, "写不进去的目录不该被当成可用日志目录"
        assert Path.cwd() == before

    def test_probe_file_is_cleaned_up(self, workdir: Path) -> None:
        """写权限探针文件用完要删掉 —— 不能在书库里留垃圾。"""
        target = workdir / "logs"
        with runtime.working_directory(target):
            pass
        leftovers = [p.name for p in target.iterdir()]
        assert leftovers == [], f"探针文件没清干净：{leftovers}"


class TestJmLogRouting:
    def test_jmcomic_logs_go_to_stderr(self) -> None:
        """stdout 只允许有 JSON —— jmcomic 的日志必须被赶到 stderr。"""
        jmcomic = pytest.importorskip("jmcomic")
        from jmcomic.jm_config import jm_logger

        assert runtime.route_jm_logs_to_stderr() is True
        assert jm_logger.handlers, "日志 handler 不该被清空"
        for handler in jm_logger.handlers:
            stream = getattr(handler, "stream", None)
            assert stream is not None
            assert stream is not sys.stdout, "jmcomic 日志不能挂在 stdout 上"

    def test_routing_is_idempotent(self) -> None:
        pytest.importorskip("jmcomic")
        first = runtime.route_jm_logs_to_stderr()
        count = len(__import__("jmcomic.jm_config", fromlist=["jm_logger"]).jm_logger.handlers)
        second = runtime.route_jm_logs_to_stderr()
        count2 = len(__import__("jmcomic.jm_config", fromlist=["jm_logger"]).jm_logger.handlers)
        assert first is second is True
        assert count == count2, "重复改道不能叠加 handler"
