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
        assert captured["zip_password"] == "s3cret-value"
        assert "s3cret-value" not in json.dumps(data, ensure_ascii=False)
        assert data["zip_encrypted"] is True


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
