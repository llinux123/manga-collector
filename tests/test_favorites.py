"""收藏夹 / 签到的测试（P3）—— 全部离线，不碰网络。

重点是**红线**与**凭据处理**：
* 收藏夹清单是灰色来源的书单 → 不得进共享区（退出码 6）；
* zip 密码只从环境变量读，且**绝不回显**；
* 没有登录态 → 退出码 3（需要凭据），而不是干等。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from manga import favorites
from manga.cli import app
from manga.envelope import EXIT_AUTH, EXIT_ERROR, EXIT_REFUSED, MangaError, RedLineViolation

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
OPTION_FILE = REPO_ROOT / "config" / "jmcomic-option.yml"


def _invoke(*args: str):
    result = runner.invoke(app, list(args))
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload


class _FakeOption:
    """够用的假 option：只提供 favorites 模块用到的那几个接口。"""

    def __init__(self, *, username: str | None = None, password: str | None = None) -> None:
        self.auth = {"username": username, "password": password, "cookies": None, "user_id": None}
        self.client = type("C", (), {"impl": "api"})()
        self._logged_in = False

    def login_with_option_auth(self, *args, **kwargs) -> None:
        self._logged_in = True
        self.auth["cookies"] = {"AVS": "x"}
        self.auth["user_id"] = "1"

    def client_key_is_given_type(self, client_key, ctype) -> bool:
        return client_key == "api"

    def _is_auth_logged_in(self, is_api: bool) -> bool:
        return bool(self.auth.get("cookies")) and (self.auth.get("user_id") if is_api else self.auth.get("username"))

    def new_jm_client(self, *args, **kwargs):
        raise AssertionError("离线测试不该建客户端")


class TestRedLine:
    def test_shared_area_is_refused(self, workdir: Path) -> None:
        export_dir = workdir / "Export" / "Manga"
        export_dir.mkdir(parents=True)
        with pytest.raises(RedLineViolation) as excinfo:
            favorites._safe_dest(export_dir / "favorites.csv", export_dir=export_dir, library_dir=None)
        assert excinfo.value.exit_code == EXIT_REFUSED

    def test_library_is_refused(self, workdir: Path) -> None:
        library = workdir / "Manga" / "Library"
        library.mkdir(parents=True)
        with pytest.raises(RedLineViolation):
            favorites._safe_dest(library / "favorites.csv", export_dir=None, library_dir=library)

    def test_normal_dir_is_allowed(self, workdir: Path) -> None:
        target = workdir / "Manga" / "Metadata" / "favorites"
        assert favorites._safe_dest(target, export_dir=workdir / "Export", library_dir=workdir / "lib") == target


class TestAuth:
    def test_missing_login_raises_auth_required(self) -> None:
        option = _FakeOption()
        with pytest.raises(MangaError) as excinfo:
            favorites._ensure_login(option)
        assert excinfo.value.code == "AUTH_REQUIRED"
        assert excinfo.value.exit_code == EXIT_AUTH

    def test_credentials_trigger_login(self) -> None:
        option = _FakeOption(username="u", password="p")
        favorites._ensure_login(option)
        assert option._logged_in is True


class TestZipPassword:
    def test_missing_password_is_rejected(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(favorites.ENV_ZIP_PASSWORD, raising=False)
        with pytest.raises(MangaError) as excinfo:
            favorites.export_favorites(
                _FakeOption(username="u", password="p"),
                dest=workdir / "fav",
                zip_enable=True,
            )
        assert excinfo.value.code == "ZIP_PASSWORD_MISSING"
        assert excinfo.value.exit_code == EXIT_ERROR

    def test_password_is_never_echoed(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """密码只从环境变量读，且不该出现在任何返回值 / 错误消息里。"""
        monkeypatch.setenv(favorites.ENV_ZIP_PASSWORD, "s3cret-value")
        captured: dict = {}

        class _Plugin:
            @classmethod
            def build(cls, option):
                return cls()

            def invoke(self, **kwargs):
                captured.update(kwargs)

            files = []
            failed_folders = []

        import jmcomic.jm_plugin as jm_plugin

        monkeypatch.setattr(jm_plugin, "FavoriteFolderExportPlugin", _Plugin)
        data = favorites.export_favorites(
            _FakeOption(username="u", password="p"),
            dest=workdir / "fav",
            zip_enable=True,
        )
        # P0 修复：上游插件绝不能拿到密码或 zip_enable=True（它会拼 shell 命令并写日志）
        assert captured.get("zip_password") is None
        assert captured.get("zip_enable") is False
        assert "s3cret-value" not in json.dumps(data, ensure_ascii=False)
        assert data["zip_encrypted"] is True


class TestInProcessZip:
    """P0 回归：加密打包必须进程内完成 —— 密码不进 shell、不进日志、不进 spawn 参数。"""

    CANARY = "canary-pw-$(echo injected);&|`x`"

    def _install_plugin(self, monkeypatch: pytest.MonkeyPatch, workdir: Path, captured: dict) -> None:
        class _Plugin:
            @classmethod
            def build(cls, option):
                return cls()

            def invoke(self, **kwargs):
                captured.update(kwargs)
                save_dir = Path(kwargs["save_dir"])
                save_dir.mkdir(parents=True, exist_ok=True)
                csv = save_dir / "folder-1.csv"
                csv.write_text("id,author,name\n1,a,t\n", encoding="utf-8")
                self.files = [str(csv)]

            files: list = []
            failed_folders: list = []

        import jmcomic.jm_plugin as jm_plugin

        monkeypatch.setattr(jm_plugin, "FavoriteFolderExportPlugin", _Plugin)

    def test_password_never_reaches_shell_or_logs(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        pyzipper = pytest.importorskip("pyzipper")
        monkeypatch.setenv(favorites.ENV_ZIP_PASSWORD, self.CANARY)
        captured: dict = {}
        self._install_plugin(monkeypatch, workdir, captured)

        spawned: list = []
        import subprocess

        real_run = subprocess.run

        def spy_run(cmd, *args, **kwargs):
            spawned.append(cmd)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy_run)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("加密打包不得起子进程"))
        monkeypatch.setattr("os.system", lambda cmd: pytest.fail(f"不得走 shell：{cmd}"))

        data = favorites.export_favorites(
            _FakeOption(username="u", password="p"),
            dest=workdir / "fav",
            zip_enable=True,
        )
        out = capsys.readouterr()
        blob = json.dumps(data, ensure_ascii=False) + out.out + out.err + repr(spawned) + repr(captured)
        assert self.CANARY not in blob, "密码出现在了输出/日志/spawn 参数里"
        assert data["zip_encrypted"] is True

        # 产物真的是 AES 加密 zip，且能用密码读回
        zip_path = Path(data["zip"])
        assert zip_path.is_file()
        with pyzipper.AESZipFile(zip_path) as zf:
            zf.setpassword(self.CANARY.encode("utf-8"))
            names = zf.namelist()
            assert "folder-1.csv" in names
            assert b"id,author,name" in zf.read("folder-1.csv")

    def test_missing_pyzipper_gives_dep_missing(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(favorites.ENV_ZIP_PASSWORD, "pw")
        captured: dict = {}
        self._install_plugin(monkeypatch, workdir, captured)
        monkeypatch.setattr(favorites, "_pyzipper", lambda: None)
        with pytest.raises(MangaError) as excinfo:
            favorites.export_favorites(
                _FakeOption(username="u", password="p"),
                dest=workdir / "fav",
                zip_enable=True,
            )
        assert excinfo.value.code == "DEP_MISSING"
        assert excinfo.value.exit_code == 4
        assert "pyzipper" in (excinfo.value.hint or "")

    def test_zip_without_password_stays_in_process(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无密码 zip 也不走上游（统一进程内 zipfile），产物可读。

        注：``export_favorites`` 的既有契约是 zip_enable=True 必须有密码；
        这里直接测 ``_zip_in_process`` 的无密码分支（内部 API 允许 None）。
        """
        import zipfile

        monkeypatch.delenv(favorites.ENV_ZIP_PASSWORD, raising=False)
        csv = workdir / "folder-1.csv"
        csv.write_text("id,author,name\n1,a,t\n", encoding="utf-8")
        zip_path = workdir / "plain.zip"
        favorites._zip_in_process([str(csv)], zip_path, password=None)
        with zipfile.ZipFile(zip_path) as zf:
            assert "folder-1.csv" in zf.namelist()


class TestCli:
    def test_favorites_without_login_exits_3(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """没有登录态 → 退出码 3（需要凭据），不是 2。"""
        monkeypatch.delenv("JM_USERNAME", raising=False)
        monkeypatch.delenv("JM_PASSWORD", raising=False)
        code, payload = _invoke("favorites", "--library-root", str(workdir / "lib"))
        assert code == EXIT_AUTH
        assert payload["error"]["code"] == "AUTH_REQUIRED"

    def test_checkin_without_login_exits_3(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JM_USERNAME", raising=False)
        monkeypatch.delenv("JM_PASSWORD", raising=False)
        code, payload = _invoke("checkin", "--library-root", str(workdir / "lib"))
        assert code == EXIT_AUTH
        assert payload["error"]["code"] == "AUTH_REQUIRED"

    def test_favorites_to_shared_area_exits_6(self, workdir: Path) -> None:
        """把收藏夹清单导进共享区 → 红线拒绝（退出码 6）。"""
        library = workdir / "lib"
        (library / "Export" / "Manga").mkdir(parents=True)
        code, payload = _invoke(
            "favorites",
            "--library-root",
            str(library),
            "--dest",
            str(library / "Export" / "Manga" / "fav"),
        )
        assert code == EXIT_REFUSED
        assert payload["error"]["code"] == "RED_LINE"
