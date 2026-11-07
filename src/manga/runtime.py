"""运行时环境能力：版本下限 · 依赖探测 · 登录态 · 工作目录。

这些是「体检」类能力，不碰网络、不写业务数据。``manga doctor`` 用它们回答四个问题：

1. **装的 jmcomic 够不够新** —— 低于下限，option 新字段会被静默忽略，表现为「配了不生效」；
2. **缺哪些可选依赖** —— 只给安装命令，**绝不自动 pip install**（红线：���自动改环境）；
3. **当前是「已登录」还是「只有凭据」** —— 后者下载时才会去登录，失败点在下载而不是配置；
4. **工作目录在哪** —— jmcomic 把进度日志写成相对路径，落点就是 CWD。

登录态判定口径**与 jmcomic 2.7.11 的 ``JmOption._is_auth_logged_in`` 一致**：
``cookies`` 非空 **且** 用户标识非空（api 客户端看 ``user_id``，html 客户端看 ``username``）。
只配了 ``username``/``password`` **不算已登录** —— 那只是凭据。
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

#: jmcomic 最低可用版本（option 新字段与 Feature 在 2.7.x 才稳定）
MIN_JMCOMIC_VERSION = "2.7.11"


def finish_plugins(option: Any) -> list[str]:
    """退出前等异步插件收工。

    ⚠ **jmcomic 2.7.11 只定义、从不自动调用** ``option.wait_all_plugins_finish()``
    （全仓 grep：只有定义处 `jm_option.py:1050`，零调用点）。
    自定义异步插件靠 ``enter_wait_list()`` 登记自己，
    不由调用方显式等 → 它们会一直挂着，进程退出时才被强杀。

    这里补上这一步：遍历 ``option.need_wait_plugins``，逐个 ``wait_until_finish()``。
    没有异步插件时是 no-op；上游改了方法名也不会炸（ getattr 判空 + 吞异常）。
    """
    if option is None:
        return []
    waiter = getattr(option, "wait_all_plugins_finish", None)
    if waiter is None:
        return []
    try:
        waiter()
    except Exception:  # noqa: BLE001 - 停机失败不该盖掉命令本身的结果
        return []
    plugins = getattr(option, "need_wait_plugins", None) or []
    return [str(getattr(plugin, "plugin_key", type(plugin).__name__)) for plugin in plugins]

#: 可选 Python 依赖：(导入名, pip 名, 什么时候用得上)
OPTIONAL_DEPENDENCIES: tuple[tuple[str, str, str], ...] = (
    ("img2pdf", "img2pdf", "export --format pdf"),
    ("PIL", "pillow", "export --format longimg"),
    ("pyzipper", "pyzipper", "export 加密 zip"),
    ("py7zr", "py7zr", "export 7z 加密 zip"),
    ("pikepdf", "pikepdf", "export 加密 pdf"),
)

#: 可选外部命令：(命令名, 什么时候用得上, 安装命令)
#: ⚠ 跟 Python 依赖一样**只给命令、绝不自动装**（红线：不替用户改环境）。
OPTIONAL_EXECUTABLES: tuple[tuple[str, str, str], ...] = (
    ("gallery-dl", "通用站下载（P3 适配器的前置）", "uv tool install gallery-dl"),
    ("mangadex-dl", "MangaDex 下载（P3 适配器的前置）", "uv tool install mangadex-dl"),
    ("jmcomic", "jmcomic 原生 CLI（降级路径）", "uv pip install 'jmcomic>=2.7.11'"),
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


# ── 版本 ──────────────────────────────────────────────────────────────────


def parse_version(raw: str | None) -> tuple[int, ...] | None:
    """从 ``"2.7.11"`` 这样的字符串里取三元组；取不到返回 ``None``。"""
    if not raw:
        return None
    match = _VERSION_RE.search(str(raw))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def version_at_least(current: str | None, minimum: str = MIN_JMCOMIC_VERSION) -> bool | None:
    """``current`` 是否 >= ``minimum``；任一版本号无法解析时返回 ``None``（不可判）。"""
    cur = parse_version(current)
    low = parse_version(minimum)
    if cur is None or low is None:
        return None
    return cur >= low


# ── 依赖探测 ──────────────────────────────────────────────────────────────


def module_installed(module: str) -> bool:
    """模块是否可导入（不真正 import，避免副作用）。"""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - 损坏的安装
        return False


def missing_modules(modules: tuple[str, ...] | list[str]) -> list[str]:
    """返回未安装的模块名（保持入参顺序）。"""
    return [name for name in modules if not module_installed(name)]


def dependency_report() -> dict[str, Any]:
    """依赖体检：只报告，**不安装**。

    ``missing_modules`` 是 Python 包，``missing_executables`` 是外部命令，
    两者都给安装/获取命令，由用户自己决定。
    """
    missing_modules_list: list[dict[str, Any]] = []
    for module, pip_name, usage in OPTIONAL_DEPENDENCIES:
        if not module_installed(module):
            missing_modules_list.append(
                {
                    "module": module,
                    "pip": pip_name,
                    "usage": usage,
                    "install": f"uv pip install {pip_name}",
                }
            )

    missing_executables: list[dict[str, Any]] = []
    for command, usage, install in OPTIONAL_EXECUTABLES:
        path = shutil.which(command)
        if not path:
            missing_executables.append({"command": command, "usage": usage, "install": install})

    return {
        "missing_modules": missing_modules_list,
        "missing_executables": missing_executables,
        "install_hint": "缺的可选依赖不影响主链路；需要时按上面的 install 命令自行安装（CLI 不会自动装）",
    }


# ── 登录态 ────────────────────────────────────────────────────────────────


def _resolve_env(value: Any) -> Any:
    """把 ``${JM_USERNAME}`` 这样的占位符换成环境变量值；未设置则置 ``None``。

    与 jmcomic 的 ``JmOption.resolve_auth_env`` 同口径：**缺变量不报错**，
    只是该项没有值。
    """
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value.strip())
        if match:
            return os.environ.get(match.group(1))
    return value


def auth_state(option_file: Path) -> dict[str, Any]:
    """读 option 文件判断登录态（不联网、不触发登录）。

    返回::

        {
          "impl": "api",
          "logged_in": False,
          "has_cookies": False,
          "has_user_id": False,
          "has_username": False,
          "has_password": False,
          "detail": "未登录：没有 cookies（多数本子无需登录）",
        }
    """
    result: dict[str, Any] = {
        "impl": None,
        "logged_in": False,
        "has_cookies": False,
        "has_user_id": False,
        "has_username": False,
        "has_password": False,
        "detail": "",
    }

    try:
        import yaml

        data = yaml.safe_load(Path(option_file).read_text(encoding="utf-8-sig")) or {}
    except Exception as exc:  # noqa: BLE001 - 体检不该因为配置坏掉而崩
        result["detail"] = f"读取 option 失败：{exc}"
        return result

    if not isinstance(data, dict):
        result["detail"] = "option 文件不是合法的映射"
        return result

    client = data.get("client") or {}
    result["impl"] = client.get("impl") if isinstance(client, dict) else None

    auth = data.get("auth") or {}
    if not isinstance(auth, dict):
        auth = {}

    cookies = _resolve_env(auth.get("cookies"))
    if isinstance(cookies, dict):
        cookies = {k: _resolve_env(v) for k, v in cookies.items()}
        cookies = {k: v for k, v in cookies.items() if v is not None}
    has_cookies = bool(cookies)

    user_id = _resolve_env(auth.get("user_id"))
    username = _resolve_env(auth.get("username"))
    password = _resolve_env(auth.get("password"))

    result["has_cookies"] = has_cookies
    result["has_user_id"] = user_id is not None
    result["has_username"] = bool(username)
    result["has_password"] = bool(password)

    impl = result["impl"] or "api"
    # 与 jmcomic 的 _is_auth_logged_in 同口径
    identity_ok = result["has_user_id"] if impl == "api" else result["has_username"]
    result["logged_in"] = has_cookies and identity_ok

    if result["logged_in"]:
        result["detail"] = f"已登录（impl={impl}，cookies + 用户标识齐备）"
    elif has_cookies:
        result["detail"] = (
            f"有 cookies 但缺用户标识（impl={impl} 需要 "
            f"{'auth.user_id' if impl == 'api' else 'auth.username'}）→ 未登录"
        )
    elif result["has_username"] and result["has_password"]:
        result["detail"] = "已配账号密码（凭据），未登录；下载时 jmcomic 会自动登录"
    else:
        result["detail"] = "未登录：没有 cookies（多数本子无需登录；收藏夹/签到/敏感题材才需要）"

    return result


# ── jmcomic 日志改道 ──────────────────────────────────────────────────────


def route_jm_logs_to_stderr() -> bool:
    """把 jmcomic 的日志改道到 **stderr**，保住「stdout 只有一个 JSON」的契约。

    jmcomic 默认把日志挂在 ``sys.stdout`` 上（``jm_config.setup_default_jm_logger``），
    一条 ``[2026-11-07 10:24:52] ...`` 就能让 agent 的 JSON 解析直接失败 ——
    这条契约是本项目的地基，不能被上游的日志破坏。

    返回是否改道成功（没装 jmcomic 时返回 ``False``，不算错误）。
    """
    import logging
    import sys

    try:
        from jmcomic.jm_config import jm_logger
    except Exception:  # noqa: BLE001 - 没装 jmcomic 不是错误
        return False

    for handler in list(jm_logger.handlers):
        jm_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(threadName)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    jm_logger.addHandler(handler)
    jm_logger.propagate = False
    return True


# ── 工作目录 ──────────────────────────────────────────────────────────────


@contextmanager
def working_directory(path: Path) -> Iterator[Path | None]:
    """把进程工作目录临时切到 ``path``，退出时恢复。

    jmcomic 的进度日志写成**相对路径**（``jmcomic-download.log``），
    落点就是当前工作目录 —— 在仓库根跑命令，日志就掉进仓库根。
    这里把 CWD 切到固定目录，日志跟着走。

    只在**调 jmcomic 之前**切换，并且调用方必须先把落点解析成绝对路径
    （否则相对落点会跟着 CWD 跑偏）。

    **目录不可写时不失败**：沙箱或只读环境下建目录会抛 ``PermissionError``，
    那不该让整条下载命令挂掉 —— 退回当前目录，由调用方给一条 warning。
    产出 ``None`` 表示「没能切目录」。
    """
    previous = Path.cwd()
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError:
        yield None
        return

    try:
        os.chdir(path)
    except OSError:
        yield None
        return

    try:
        yield Path(path)
    finally:
        os.chdir(previous)


def fallback_log_dir() -> Path | None:
    """日志目录的备选：系统临时目录下的 ``manga-logs``。

    书库在工作区之外时（沙箱只读/不可写），它比当前工作目录更合适 ——
    至少不会把日志写进仓库根。
    """
    import tempfile

    candidate = Path(tempfile.gettempdir()) / "manga-logs"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return candidate
