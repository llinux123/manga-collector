"""收藏夹导出与每日签到。

**红线**：收藏夹里是灰色来源（禁漫）的书单 —— 它的**清单本身**也不能进
Syncthing 共享区（``Export\\Manga``），所以导出落点必须过红线守卫。

**凭据**：
* 账号密码走 jmcomic 的 ``auth``（option 文件 / 环境变量），本模块不碰；
* zip 密码只从**环境变量**读（``MANGA_ZIP_PASSWORD``），**绝不回显、绝不写进信封**；
* 加密打包**进程内**完成（pyzipper AES-256）——上游插件的加密路径会把密码
  拼进 shell 命令并写日志，本项目绝不调用它（P0 修复）。

⚠ 收藏夹与签到都**需要登录态**。没有登录态时 jmcomic 会在调用时抛错 ——
本模块把它翻译成退出码 3（需要凭据），让 agent 知道该去配 ``auth`` 而不是干等。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .envelope import EXIT_AUTH, EXIT_ERROR, MangaError, RedLineViolation

#: zip 密码从哪个环境变量读（**只读环境变量**，不接受命令行明文参数）
ENV_ZIP_PASSWORD = "MANGA_ZIP_PASSWORD"

#: 收藏夹导出默认落点（在书库的元数据区，不碰共享区）
DEFAULT_FAVORITES_DIRNAME = "favorites"


def _ensure_login(option) -> bool:
    """确保已登录；没有就抛 AUTH_REQUIRED。

    登录规则直接问 jmcomic 自己（``_is_auth_logged_in``）：cookies 非空 **且**
    用户标识非空（api 看 ``user_id``，html 看 ``username``）。
    """
    if option.auth.get("username") and option.auth.get("password"):
        option.login_with_option_auth()

    from jmcomic.jm_client_impl import JmApiClient

    target_impl = option.client.impl
    if isinstance(target_impl, type):  # 允许直接传类
        target_impl = target_impl.client_key
    is_api = option.client_key_is_given_type(target_impl, JmApiClient)

    if not option._is_auth_logged_in(is_api=is_api):  # noqa: SLF001 - 上游的登录口径
        raise MangaError(
            "收藏夹/签到需要登录态",
            code="AUTH_REQUIRED",
            exit_code=EXIT_AUTH,
            hint=(
                "在 jmcomic 的 option 里配 auth.username + auth.password，"
                "或设 JM_USERNAME / JM_PASSWORD 环境变量后重试"
            ),
        )
    return True


def export_favorites(
    option,
    *,
    dest: Path,
    export_dir: Path | None = None,
    library_dir: Path | None = None,
    zip_enable: bool = False,
    zip_filepath: Path | None = None,
    zip_password: str | None = None,
    max_retry: int = 2,
) -> dict[str, Any]:
    """导出收藏夹为 CSV（每个收藏夹一个文件），可选再打一个 zip。

    走 jmcomic 的 ``favorite_folder_export`` 插件（``main`` 事件组）。
    """
    dest = _safe_dest(dest, export_dir=export_dir, library_dir=library_dir)
    dest.mkdir(parents=True, exist_ok=True)
    _ensure_login(option)

    if zip_filepath is None:
        zip_filepath = dest / "favorites.zip"
    if zip_enable and zip_password is None:
        zip_password = os.environ.get(ENV_ZIP_PASSWORD)
    if zip_enable and not zip_password:
        raise MangaError(
            "加密 zip 需要密码",
            code="ZIP_PASSWORD_MISSING",
            exit_code=EXIT_ERROR,
            hint=f"设环境变量 {ENV_ZIP_PASSWORD}（不要把密码写在命令行里，会进 shell 历史）",
        )

    try:
        from jmcomic.jm_plugin import FavoriteFolderExportPlugin

        plugin = FavoriteFolderExportPlugin.build(option)
        # ⚠ P0：绝不让上游做 zip —— 它的加密路径把 `-p<密码>` 拼进 shell 命令
        # （subprocess.run(shell=True)）并完整写日志 → 密码进 stderr/日志/进程命令行。
        # CSV 导出照旧走上游；打包一律在本进程内完成（见 _zip_in_process）。
        plugin.invoke(
            save_dir=str(dest),
            zip_enable=False,
            # 上游插件**无条件**对 zip_filepath 做 os.path.abspath() → 传 None 会 TypeError，
            # 所以即使不要 zip 也要给一个合法路径
            zip_filepath=str(zip_filepath),
            zip_password=None,
            delete_original_file=False,
            max_retry=max_retry,
        )
    except MangaError:
        raise
    except Exception as exc:  # noqa: BLE001 - 翻译成带退出码的领域异常
        raise _map_exception(exc, what="收藏夹导出") from exc

    files = [str(f) for f in getattr(plugin, "files", []) or []]
    failed_folders = [str(f) for f in getattr(plugin, "failed_folders", []) or []]

    if zip_enable:
        _zip_in_process(files, Path(zip_filepath), password=zip_password)

    return {
        "dest": str(dest),
        "file_count": len(files),
        "files": files[:20],
        "files_truncated": len(files) > 20,
        "failed_folders": failed_folders,
        "zip": str(zip_filepath) if (zip_enable and zip_filepath) else None,
        "zip_encrypted": bool(zip_enable and zip_password),
    }


def _pyzipper():
    """延迟导入 pyzipper；没装返回 None（调用方翻译成 DEP_MISSING，绝不自动装）。"""
    try:
        import pyzipper

        return pyzipper
    except ImportError:
        return None


def _zip_in_process(files: list[str], zip_path: Path, *, password: str | None) -> None:
    """进程内打包本次导出的文件。

    * 有密码 → pyzipper AES-256（WZ_AES）；密码只存在于内存与 zip 头，
      不进命令行、不进日志、不起子进程。
    * 无密码 → 标准库 zipfile。
    * 只打包 ``files``（本次成功导出的清单），不扫整个目录 ——
      避免把上一轮遗留或写了一半的 csv 混进包里。
    """
    if not files:
        return
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if password:
        pyzipper = _pyzipper()
        if pyzipper is None:
            raise MangaError(
                "加密 zip 缺少可选依赖：pyzipper",
                code="DEP_MISSING",
                exit_code=4,
                hint="uv pip install pyzipper（CLI 不会自动安装）",
            )
        with pyzipper.AESZipFile(
            zip_path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            for f in files:
                zf.write(f, arcname=Path(f).name)
    else:
        import zipfile

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=Path(f).name)


def checkin(option) -> dict[str, Any]:
    """每日签到。

    返回 ``code``：0 = 成功，1 = 今天已经签过。
    """
    _ensure_login(option)
    try:
        client = option.new_jm_client()
        resp = client.daily_checkin()
    except Exception as exc:  # noqa: BLE001
        raise _map_exception(exc, what="签到") from exc

    code = getattr(resp, "code", None)
    if code is None:
        status = getattr(resp, "status", None)
        code = status
    msg = getattr(resp, "msg", None) or getattr(resp, "message", None)

    return {
        "code": code,
        "msg": msg,
        "already_checked_in": code == 1,
        "ok": code == 0,
    }


def _safe_dest(dest: Path, *, export_dir: Path | None, library_dir: Path | None) -> Path:
    """落点守卫：收藏夹清单**不得**进共享区，也不得直接写权威库。"""
    from .adapters.jmcomic import is_inside

    target = Path(dest).expanduser().resolve()
    if export_dir is not None and is_inside(target, Path(export_dir).resolve()):
        raise RedLineViolation(
            "红线：收藏夹清单属于灰色来源，不得进入 Syncthing 共享区 Export\\Manga",
            data={"dest": str(target)},
        )
    if library_dir is not None and is_inside(target, Path(library_dir).resolve()):
        raise RedLineViolation(
            "红线：收藏夹清单不得直接写进权威库 Manga\\Library",
            data={"dest": str(target)},
        )
    return target


def _map_exception(exc: Exception, *, what: str) -> MangaError:
    """把 jmcomic 的异常翻译成带退出码的领域异常。"""
    from .adapters.jmcomic import _map_exception as jm_map  # noqa: PLC0415 - 避免循环导入

    mapped = jm_map(exc, what=what)
    if mapped.exit_code == EXIT_ERROR and "登录" in str(exc):
        return MangaError(
            f"{what}失败：需要登录态",
            code="AUTH_REQUIRED",
            exit_code=EXIT_AUTH,
            hint="在 jmcomic 的 option 里配 auth.username + auth.password",
        )
    return mapped
