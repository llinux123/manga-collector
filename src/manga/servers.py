"""Komga / Kavita 扫描助手（P3 · 服务端）。

计划原文：「Komga ``POST /api/v1/libraries/:id/scan`` 与 Kavita 手动扫描的
**提示与检查**（不自动挂载、不自动改元数据）」。

本模块的边界（**只读 + 只给提示**）：

* **会做的**：探测服务端是否可达、是 Komga 还是 Kavita；列出 Komga 的库
  （id / name / root path）供人挑；按需触发**一次** Komga 扫描；给出 Kavita
  的人工扫描指引；
* **不会做的**：不自动挂载目录、不自动改元数据、不自动扫完回写、
  不做任何删除/重建操作。本模块的 HTTP 方法被硬限制在
  ``GET`` 与 ``POST``（``POST`` 仅用于 Komga 的 scan 端点）。

依据（``references/servers.md``）：

* Komga 1.30.0：``POST /api/v1/libraries/:id/scan`` → **202**，需 **ADMIN** 权限，
  认证用 Basic 或 ``X-API-Key``；
* Kavita 0.9.x：**没有**等价的公开扫描 API（UI 的 Scan Library / Analyze 没有
  稳定可依赖的 REST 端点）；默认开 Folder Watching，约 10 分钟延迟。

⚠ 远端盘上「手动 Analyze」会强制重算（重 IO）→ 触发时机由人决定，
本项目只给指引，不代劳。

**契约**：

* 成功返回 ``dict``（直接进 JSON 信封的 data 字段）；
* 失败抛 :class:`~manga.envelope.MangaError`（带 code 与退出码）；
* **网络失败不抛裸异常**：``check_server`` 永远返回
  ``{"reachable": False, ...}`` + 可读原因 —— agent 要的是信息，不是异常；
* 只用标准库 ``urllib``（不引入 requests / httpx）。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .envelope import EXIT_ERROR, EXIT_NOTFOUND, EXIT_USAGE, MangaError

#: 本模块**只允许**的 HTTP 方法：GET（读）与 POST（仅 Komga 的扫描触发）。
#: 硬限制在代码里 —— 这模块没有能力做删除 / 重建 / 改元数据。
ALLOWED_HTTP_METHODS: tuple[str, ...] = ("GET", "POST")

#: Komga 的库列表端点（相对 base_url）
KOMGA_LIBRARIES_PATH = "/api/v1/libraries"

#: Komga 触发扫描的端点模板
KOMGA_SCAN_PATH = "/api/v1/libraries/{library_id}/scan"


# ── 内部：HTTP ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _HttpResult:
    """一次成功（2xx/3xx）的 HTTP 响应。"""

    status: int
    body: str
    headers: dict[str, str]


class _RequestFailure(Exception):
    """一次失败的请求（网络错误或 4xx/5xx）。

    不直接抛给调用方：由各个公开函数翻译成 ``MangaError`` 或可读的 dict。
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


def _auth_headers(
    *,
    auth: tuple[str, str] | None = None,
    api_key: str | None = None,
) -> dict[str, str]:
    """组认证头：Basic（账号密码）与 ``X-API-Key``（API Key）二选一或同时给。"""
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "manga-collector",
    }
    if auth is not None:
        if not isinstance(auth, (tuple, list)) or len(auth) != 2:
            raise MangaError(
                "auth 必须是 (用户名, 密码) 二元组",
                code="USAGE",
                exit_code=EXIT_USAGE,
                hint="例：auth=('admin', '你的密码')；或改用 api_key=",
            )
        user, password = auth
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _normalize_base(base_url: str) -> str:
    """校验并规范化 base_url（去掉尾部斜杠）。"""
    if not isinstance(base_url, str) or not base_url.strip():
        raise MangaError(
            "base_url 不能为空",
            code="USAGE",
            exit_code=EXIT_USAGE,
            hint="例：http://127.0.0.1:25600（Komga）/ http://127.0.0.1:5000（Kavita）",
        )
    url = base_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MangaError(
            f"base_url 不是合法的 http(s) 地址：{base_url!r}",
            code="USAGE",
            exit_code=EXIT_USAGE,
            hint="例：http://127.0.0.1:25600",
        )
    return url


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 8,
) -> _HttpResult:
    """发一次 HTTP 请求（标准库 urllib）。

    失败统一转成 :class:`_RequestFailure`（带状态码与原因），不抛裸异常。
    """
    verb = method.upper()
    if verb not in ALLOWED_HTTP_METHODS:
        raise MangaError(
            f"本模块不允许的 HTTP 方法：{verb}",
            code="METHOD_NOT_ALLOWED",
            exit_code=EXIT_USAGE,
            hint=f"只允许：{'、'.join(ALLOWED_HTTP_METHODS)}（不自动改元数据 / 不删除 / 不重建）",
        )

    request = urllib.request.Request(url, data=data, method=verb)
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 只允许 http(s)
            raw = response.read()
            status = getattr(response, "status", None)
            if status is None:  # pragma: no cover - 老式 response
                status = response.getcode()
            resp_headers = dict(response.headers.items()) if getattr(response, "headers", None) else {}
            return _HttpResult(
                status=int(status),
                body=raw.decode("utf-8", errors="replace"),
                headers=resp_headers,
            )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读不出正文也无所谓
            body = ""
        raise _RequestFailure(
            f"HTTP {exc.code} {exc.reason}",
            status=int(exc.code),
            body=body,
        ) from exc
    except urllib.error.URLError as exc:
        raise _RequestFailure(f"连接失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise _RequestFailure(f"连接超时（{timeout}s）") from exc
    except OSError as exc:
        raise _RequestFailure(f"连接失败：{exc}") from exc


def _json_or_none(body: str) -> Any:
    """把响应体解析成 JSON；解析不了返回 ``None``（不抛）。"""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


def _library_summary(item: dict[str, Any]) -> dict[str, Any]:
    """把 Komga 的 library 对象压成「给人挑」的几项。"""
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "root": item.get("root"),
        "available": item.get("available"),
    }


# ── 公开 API ──────────────────────────────────────────────────────────────


def check_server(
    base_url: str,
    *,
    auth: tuple[str, str] | None = None,
    api_key: str | None = None,
    timeout: float = 8,
) -> dict[str, Any]:
    """探测服务端是否可达、是 Komga 还是 Kavita。

    探测顺序：

    1. ``GET {base}/api/v1/libraries`` —— Komga 专属路径：
       200 且是 JSON 数组 → 就是 Komga；
       401/403 → 路径存在但没带对凭据 → 仍是 Komga（认证问题）；
       404 → 不是 Komga（Kavita 会这样）；
    2. ``GET {base}/`` —— 判活 + 指纹：正文里有 ``kavita`` / ``komga`` 就认类型。

    **网络失败不抛异常**：返回 ``reachable=False`` + 可读的 ``reason``。
    （只有 base_url 本身不合法才抛 :class:`MangaError` —— 那是用法错误。）

    返回::

        {"reachable": bool, "kind": "komga"|"kavita"|"unknown", "base_url": str,
         "status": int|None, "auth_ok": bool|None, "reason": str, "detail": str,
         "checked": [...], "libraries": [...]}
    """
    base = _normalize_base(base_url)
    headers = _auth_headers(auth=auth, api_key=api_key)

    result: dict[str, Any] = {
        "base_url": base,
        "reachable": False,
        "kind": "unknown",
        "status": None,
        "auth_ok": None,
        "reason": "",
        "detail": "",
        "checked": [],
        "libraries": [],
    }

    # ① Komga 探针
    lib_status: int | None = None
    lib_list: Any = None
    lib_auth_failed = False
    lib_failure: _RequestFailure | None = None
    try:
        response = _request("GET", base + KOMGA_LIBRARIES_PATH, headers=headers, timeout=timeout)
    except _RequestFailure as exc:
        lib_failure = exc
        lib_status = exc.status
        if exc.status in (401, 403):
            # 路径存在但被拒 → 是 Komga，只是凭据不对
            lib_auth_failed = True
    else:
        lib_status = response.status
        result["checked"].append("GET /api/v1/libraries")
        payload = _json_or_none(response.body)
        if isinstance(payload, list):
            lib_list = payload

    # ② 根路径：判活 + 指纹（Kavita 没有稳定的库列表 API）
    root_status: int | None = None
    root_body = ""
    root_failure: _RequestFailure | None = None
    try:
        root = _request("GET", base + "/", headers=headers, timeout=timeout)
    except _RequestFailure as exc:
        root_failure = exc
        root_status = exc.status
    else:
        root_status = root.status
        root_body = root.body.lower()
        result["checked"].append("GET /")

    # ③ 判定
    got_http = lib_status is not None or root_status is not None
    result["reachable"] = got_http
    if lib_status is not None:
        result["status"] = lib_status
    elif root_status is not None:
        result["status"] = root_status

    if lib_list is not None:
        result["kind"] = "komga"
        result["auth_ok"] = True
        result["libraries"] = [_library_summary(item) for item in lib_list if isinstance(item, dict)]
        result["reason"] = "Komga 可达，认证通过"
        result["detail"] = (
            f"共 {len(result['libraries'])} 个库；"
            "用 komga_libraries 拿 id/name/root，再调 trigger_komga_scan"
        )
        return result

    if lib_auth_failed:
        result["kind"] = "komga"
        result["auth_ok"] = False
        result["reason"] = f"服务器可达，但认证失败（HTTP {lib_status}）"
        result["detail"] = (
            "Komga 触发扫描需要 ADMIN 权限的账号，或用 X-API-Key"
            "（后台 → Settings → API Key）；也可能是用户名/密码写错"
        )
        return result

    if "kavita" in root_body:
        result["kind"] = "kavita"
    elif "komga" in root_body:
        result["kind"] = "komga"

    if got_http:
        result["reason"] = f"服务端可达（HTTP {result['status']}）"
        if result["kind"] == "kavita":
            result["detail"] = "Kavita 没有公开的扫描 API → 用 kavita_scan_hint 给人工指引"
        elif result["kind"] == "komga":
            result["detail"] = "Komga 可用：komga_libraries 列库 → trigger_komga_scan 触发扫描"
        else:
            result["detail"] = (
                "认不出是 Komga 还是 Kavita：/api/v1/libraries 没返回库列表，"
                "根路径正文里也没有指纹"
            )
        return result

    failure = lib_failure or root_failure
    reason = failure.message if failure is not None else "未知错误"
    result["reason"] = f"连不上 {base}：{reason}"
    result["detail"] = (
        "检查服务是否在跑、端口对不对（本机防火墙 / 容器端口映射）、"
        "以及 base_url 里的协议（http 还是 https）"
    )
    return result


def komga_libraries(
    base_url: str,
    *,
    auth: tuple[str, str] | None = None,
    api_key: str | None = None,
    timeout: float = 8,
) -> dict[str, Any]:
    """列出 Komga 的库（id / name / root path），供人挑要扫哪个。

    返回::

        {"base_url": str, "libraries": [{"id", "name", "root", "available"}],
         "count": int, "hint": str}

    连不上 / 认证失败会抛 :class:`MangaError`（code 分别是
    ``SERVER_UNREACHABLE`` / ``AUTH_FAILED``，退出码 2）。
    """
    base = _normalize_base(base_url)
    headers = _auth_headers(auth=auth, api_key=api_key)

    try:
        response = _request("GET", base + KOMGA_LIBRARIES_PATH, headers=headers, timeout=timeout)
    except _RequestFailure as exc:
        if exc.status in (401, 403):
            raise MangaError(
                f"Komga 认证失败（HTTP {exc.status}）",
                code="AUTH_FAILED",
                exit_code=EXIT_ERROR,
                hint=(
                    "用有 ADMIN 权限的账号，或设置 API Key"
                    "（Komga 后台 → Settings → API Key → 作为 X-API-Key 头发送）"
                ),
                data={"base_url": base, "status": exc.status},
            ) from exc
        raise MangaError(
            f"取不到 Komga 库列表：{exc.message}",
            code="SERVER_UNREACHABLE",
            exit_code=EXIT_ERROR,
            hint="确认 Komga 在跑、端口对不对；或先跑 check_server 看可达性",
            data={"base_url": base},
        ) from exc

    payload = _json_or_none(response.body)
    if not isinstance(payload, list):
        raise MangaError(
            "Komga 库列表返回格式异常（不是 JSON 数组）",
            code="BAD_RESPONSE",
            exit_code=EXIT_ERROR,
            hint="确认 base_url 指向的是 Komga 而不是别的服务",
            data={"base_url": base, "status": response.status},
        )

    libraries = [_library_summary(item) for item in payload if isinstance(item, dict)]
    return {
        "base_url": base,
        "libraries": libraries,
        "count": len(libraries),
        "hint": "挑一个库，把它的 id 交给 trigger_komga_scan；root 是服务器侧的目录（不是本机路径）",
    }


def trigger_komga_scan(
    base_url: str,
    library_id: str | int,
    *,
    auth: tuple[str, str] | None = None,
    api_key: str | None = None,
    timeout: float = 8,
) -> dict[str, Any]:
    """触发一次 Komga 库扫描：``POST {base}/api/v1/libraries/{id}/scan``。

    需要 **ADMIN** 权限（Basic 或 ``X-API-Key``）。成功返回 202。

    失败会抛 :class:`MangaError`：

    * 401/403 → code ``AUTH_FAILED``（提示权限 / API Key）；
    * 404 → code ``LIBRARY_NOT_FOUND``（退出码 4）；
    * 连不上 → code ``SERVER_UNREACHABLE``。

    ⚠ 扫描是异步的，且**不要边下边扫**（会重复入库或刮削到半成品）。
    """
    base = _normalize_base(base_url)
    headers = _auth_headers(auth=auth, api_key=api_key)

    if library_id is None or (isinstance(library_id, str) and not library_id.strip()):
        raise MangaError(
            "library_id 不能为空",
            code="USAGE",
            exit_code=EXIT_USAGE,
            hint="用 komga_libraries 列出库，取其中的 id",
        )

    quoted = urllib.parse.quote(str(library_id), safe="")
    url = base + KOMGA_SCAN_PATH.format(library_id=quoted)

    try:
        response = _request("POST", url, headers=headers, data=b"", timeout=timeout)
    except _RequestFailure as exc:
        if exc.status in (401, 403):
            raise MangaError(
                f"Komga 拒绝触发扫描：权限不足（HTTP {exc.status}）",
                code="AUTH_FAILED",
                exit_code=EXIT_ERROR,
                hint=(
                    "触发扫描需要 ADMIN 权限：用管理员账号登录，"
                    "或在 Komga 后台 → Settings → API Key 生成 Key 后用 X-API-Key 头"
                ),
                data={"base_url": base, "library_id": library_id, "status": exc.status},
            ) from exc
        if exc.status == 404:
            raise MangaError(
                f"找不到 library_id={library_id}（HTTP 404）",
                code="LIBRARY_NOT_FOUND",
                exit_code=EXIT_NOTFOUND,
                hint="用 komga_libraries 列出实际的库 id",
                data={"base_url": base, "library_id": library_id},
            ) from exc
        raise MangaError(
            f"触发 Komga 扫描失败：{exc.message}",
            code="SERVER_UNREACHABLE",
            exit_code=EXIT_ERROR,
            hint="确认 Komga 在跑；也可以在 UI 上手动 Scan",
            data={"base_url": base, "library_id": library_id},
        ) from exc

    return {
        "ok": True,
        "base_url": base,
        "library_id": library_id,
        "status": response.status,
        "message": "扫描已触发（Komga 在后台处理）",
        "note": (
            "扫描是异步的：稍后在 Komga UI 或 GET /api/v1/libraries/{id}/series 看结果；"
            "别边下边扫（会重复入库或刮削到半成品）"
        ),
    }


def kavita_scan_hint(
    base_url: str | None = None,
    *,
    library_path: str | None = None,
) -> dict[str, Any]:
    """Kavita 扫描的**人工指引**（本函数不做任何 HTTP 请求，更不做任何写操作）。

    返回::

        {"ok": True, "kind": "kavita", "automated": False, "action": "manual",
         "server": str|None, "library_path": str|None,
         "instructions": [...], "why_not_automated": [...], "notes": [...],
         "hint": str}
    """
    server = base_url.strip().rstrip("/") if isinstance(base_url, str) and base_url.strip() else None

    instructions = [
        f"打开 Kavita 的 Web UI{('：' + server) if server else ''}",
        "进入「Library / 库」页面，选中要扫的库，点「Scan Library」",
        "想强制重算元数据（文件名 / ComicInfo 改过之后）再点「Analyze」",
        "等进度条跑完，刷新库页面确认新章节出现",
        "本项目的验收口径不变：改完还要 Refresh metadata / Analyze，否则页面仍是旧标题",
    ]

    why_not_automated = [
        "Kavita 没有等价的公开扫描 API：UI 的 Scan Library / Analyze 没有稳定可依赖的 REST 端点，"
        "本项目不猜私有端点（猜错会变成误操作）",
        "Kavita 默认开 Folder Watching，约 10 分钟延迟；本项目按 ADR 约定不开 watch"
        "（网络盘会漏事件），所以宁可给指引也不自动轮询",
        "手动 Analyze 会强制重算（重 IO，远程盘慎用）→ 什么时候触发由人决定",
        "本项目对服务端只做只读检查与提示：绝不自动挂载、绝不自动改元数据、"
        "绝不扫完回写（回写等于替人做刮削决策）",
    ]

    notes = [
        "别边下边扫：会重复入库或刮削到半成品",
        "Kavita 只看分钟级目录 mtime → 重命名 / 移动目录后先跑全库扫描",
        "归档内元数据会覆盖解析结果 → 文件名与 ComicInfo 必须一致",
    ]

    return {
        "ok": True,
        "kind": "kavita",
        "automated": False,
        "action": "manual",
        "server": server,
        "library_path": library_path,
        "instructions": instructions,
        "why_not_automated": why_not_automated,
        "notes": notes,
        "hint": "Kavita 的扫描只能人工点：本项目只给指引，不代劳（详见 why_not_automated）",
    }


__all__ = [
    "ALLOWED_HTTP_METHODS",
    "KOMGA_LIBRARIES_PATH",
    "KOMGA_SCAN_PATH",
    "check_server",
    "kavita_scan_hint",
    "komga_libraries",
    "trigger_komga_scan",
]
