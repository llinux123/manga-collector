"""jmcomic 适配层（P1）。

把 jmcomic 2.7.11 的能力包成 CLI 可调用的形式，并守住三条线：

1. **懒加载** —— 导入本模块不联网、不读配置；client 按需创建。
2. **落点可控 + 红线** —— 下载/导出的落点不得落在
   ``Export\\Manga``（Syncthing 共享区）或 ``Manga\\Library``（权威库，
   直写会绕过校验闸门）；触发即 :class:`RedLineViolation`（退出码 6）。
   **红线在代码里，不在提示词里。**
3. **错误可判** —— 认证→3、资源不存在→4、其余→2。

配置层面的第二个陷阱：``dir_rule.rule`` 的**分隔符与写法必须配套**
（``Bd_Atitle/Pname`` 会产出字面量目录 ``Bd_Atitle`` → 所有章节被误归到
同名系列，静默污染库）。:func:`validate_dir_rule` 在下载前就拦下它。

网络相关的一律不在这里做重试策略；重试交给 jmcomic 的 ``advanced_retry``
插件（配置在 option 文件里）。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

from ..envelope import EXIT_AUTH, EXIT_ERROR, EXIT_NOTFOUND, MangaError, RedLineViolation

# ── 常量 ──────────────────────────────────────────────────────────────────

#: 搜索维度 → client 方法名
SEARCH_MODES: dict[str, str] = {
    "site": "search_site",
    "tag": "search_tag",
    "author": "search_author",
    "work": "search_work",
    "actor": "search_actor",
}

#: 导出格式 → Feature 属性名
EXPORT_FORMATS: dict[str, str] = {
    "zip": "export_zip",
    "pdf": "export_pdf",
    "longimg": "export_long_img",
}

#: 导出格式 → 该 Feature 接受的落点参数名（空 = 跟随 base_dir）
EXPORT_DIR_KWARGS: dict[str, str] = {
    "zip": "zip_dir",
    "pdf": "pdf_dir",
    "longimg": "",
}


# ── 工具 ──────────────────────────────────────────────────────────────────

def _jmcomic():
    """延迟导入 jmcomic（测试里要能替换）。"""
    try:
        import jmcomic
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于环境问题
        raise MangaError(
            "未安装 jmcomic",
            code="DEP_MISSING",
            exit_code=EXIT_ERROR,
            hint="uv pip install 'jmcomic>=2.7.11'",
        ) from exc
    return jmcomic


def _safe(value: Any, default: Any = None) -> Any:
    """取属性并容忍异常（上游实体字段在不同 impl 下可能缺失）。"""
    try:
        return value() if callable(value) else value
    except Exception:  # pragma: no cover - 防御性
        return default


def is_inside(child: Path, parent: Path) -> bool:
    """``child`` 是否在 ``parent`` 之内（含相等）。"""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def ensure_safe_dest(
    dest: Path,
    *,
    export_dir: Path | None = None,
    library_dir: Path | None = None,
) -> Path:
    """落点红线检查。

    拒绝两类落点：

    * ``Export\\Manga`` —— Syncthing 共享区，灰色来源不得进入（红线）；
    * ``Manga\\Library`` —— 权威库，直写会绕过入库校验闸门。

    两者都以退出码 6 拒绝，并把越界对象写进 ``data``，让 agent 能自述原因。
    """
    dest = Path(dest)

    if export_dir is not None and is_inside(dest, export_dir):
        raise RedLineViolation(
            f"落点位于 Syncthing 共享区：{dest}",
            data={
                "dest": str(dest),
                "forbidden": str(export_dir),
                "rule": "灰色来源不得进入 Export\\Manga",
            },
        )

    if library_dir is not None and is_inside(dest, library_dir):
        raise RedLineViolation(
            f"落点位于权威库：{dest}",
            data={
                "dest": str(dest),
                "forbidden": str(library_dir),
                "rule": "下载器不得直写 Manga\\Library（须经 ingest 校验闸门）",
            },
        )

    return dest


def split_rule(rule: str) -> list[str]:
    """按 jmcomic 的规则切段（见 ``DirRule.split_rule_dsl``）。"""
    if "/" in rule:
        parts = rule.split("/")
    elif "_" in rule:
        parts = rule.split("_")
    else:
        parts = [rule]
    return [p.strip() for p in parts]


def validate_dir_rule(rule: str, *, expected_levels: int = 2) -> list[str]:
    """校验 ``dir_rule.rule`` 能渲染出「本子 / 章节」两层。

    拒绝两种写法：

    * **字面量前缀**（``Bd_Atitle/Pname``）—— 分隔符是 ``/`` 时按 ``/`` 切段，
      首段 ``Bd_Atitle`` 既不等于 ``Bd``、又不以 ``A``/``P`` 开头 → 落到
      f-string 解析器被原样输出，生成字面量目录 ``Bd_Atitle``；
    * **层级不对** —— 转换脚本 ``Convert-JmComicToCbz.ps1`` 假设两层结构。

    正确写法：``Bd/Atitle/Pname``、``Bd_Atitle_Pname``、``Bd / {Atitle} / {Pname}``。
    """
    segments = [s for s in split_rule(rule) if s]
    if not segments or segments[0] != "Bd":
        segments = ["Bd", *segments]
    body = segments[1:]

    literal = [s for s in body if s.lower().startswith("bd_")]
    if literal:
        raise MangaError(
            f"dir_rule.rule 含字面量前缀 {literal}：{rule}",
            code="DIR_RULE_LITERAL",
            exit_code=EXIT_ERROR,
            hint="分隔符与写法不能混用：用 Bd/Atitle/Pname 或 Bd_Atitle_Pname，不要 Bd_Atitle/Pname",
        )

    if len(body) != expected_levels:
        raise MangaError(
            f"dir_rule.rule 应渲染出 {expected_levels} 层（本子/章节），实际 {len(body)} 层：{rule}",
            code="DIR_RULE_LEVELS",
            exit_code=EXIT_ERROR,
            hint="建议 Bd/Atitle/Pname",
        )

    return segments


# ── option / client ───────────────────────────────────────────────────────

def build_option(
    option_file: Path,
    *,
    dest: Path | None = None,
    export_dir: Path | None = None,
    library_dir: Path | None = None,
):
    """按 option 文件构造 :class:`JmOption`，并强制落点。

    落点由 CLI 决定（option 文件只提供 client/download 行为），这样才能保证
    「下载落点永远在 ``00-Incoming`` 旁边」且不碰红线目录。
    """
    jm = _jmcomic()

    option_file = Path(option_file)
    if not option_file.is_file():
        raise MangaError(
            f"找不到 option 文件：{option_file}",
            code="OPTION_MISSING",
            exit_code=EXIT_NOTFOUND,
            hint="用 --option 指定，或设环境变量 MANGA_OPTION_FILE",
        )

    try:
        option = jm.create_option_by_file(str(option_file))
    except Exception as exc:
        raise MangaError(
            f"option 文件解析失败：{option_file}（{exc}）",
            code="OPTION_INVALID",
            exit_code=EXIT_ERROR,
            hint="检查 YAML 缩进与字段拼写",
        ) from exc

    validate_dir_rule(_safe(lambda: option.dir_rule.rule_dsl, "") or "")

    if dest is not None:
        ensure_safe_dest(dest, export_dir=export_dir, library_dir=library_dir)
        option.dir_rule.base_dir = str(dest)

    return option


def make_client(option, *, impl: str | None = None, domain_list: Sequence[str] | None = None):
    """创建一个 jmcomic client（不登录；登录由下载路径上的 ``login=True`` 负责）。"""
    kwargs: dict[str, Any] = {}
    if impl:
        kwargs["impl"] = impl
    if domain_list:
        kwargs["domain_list"] = list(domain_list)
    return option.new_jm_client(**kwargs)


# ── 检索 ──────────────────────────────────────────────────────────────────

def search(
    option,
    query: str,
    *,
    mode: str = "site",
    page: int = 1,
    limit: int = 20,
    impl: str | None = None,
    domain_list: Sequence[str] | None = None,
) -> dict[str, Any]:
    """站内搜索（site/tag/author/work/actor 五个维度）。"""
    if mode not in SEARCH_MODES:
        raise MangaError(
            f"未知搜索维度：{mode}",
            code="BAD_MODE",
            exit_code=EXIT_ERROR,
            hint="可选：" + "、".join(SEARCH_MODES),
        )

    client = make_client(option, impl=impl, domain_list=domain_list)
    method = getattr(client, SEARCH_MODES[mode])
    result = method(query, page)

    items: list[dict[str, Any]] = []
    for album_id, title in result.iter_id_title():
        items.append({"id": str(album_id), "title": title})
        if len(items) >= limit:
            break

    return {
        "query": query,
        "mode": mode,
        "page": page,
        "total": _safe(lambda: result.total),
        "page_count": _safe(lambda: result.page_count),
        "count": len(items),
        "items": items,
    }


# ── 详情 / 评论 ───────────────────────────────────────────────────────────

def album_detail(
    option,
    album_id: str,
    *,
    impl: str | None = None,
    domain_list: Sequence[str] | None = None,
) -> dict[str, Any]:
    """本子详情（含章节列表）。"""
    client = make_client(option, impl=impl, domain_list=domain_list)
    album = client.get_album_detail(str(album_id))

    episodes = []
    for photo in _safe(lambda: album.episode_list, []) or []:
        episodes.append(
            {
                "id": str(_safe(lambda: photo.photo_id)),
                "index": _safe(lambda: photo.album_index),
                "name": _safe(lambda: photo.name),
                "images": len(_safe(lambda: photo.page_arr, []) or []),
            }
        )

    return {
        "id": str(_safe(lambda: album.album_id)),
        "title": _safe(lambda: album.name),
        "oname": _safe(lambda: album.oname),
        "author": _safe(lambda: album.author),
        "authors": list(_safe(lambda: album.authors, []) or []),
        "description": _safe(lambda: album.description),
        "tags": list(_safe(lambda: album.tags, []) or []),
        "page_count": _safe(lambda: album.page_count),
        "pub_date": _safe(lambda: album.pub_date),
        "update_date": _safe(lambda: album.update_date),
        "likes": _safe(lambda: album.likes),
        "views": _safe(lambda: album.views),
        "comment_count": _safe(lambda: album.comment_count),
        "episode_count": len(episodes),
        "episodes": episodes,
    }


def comments(
    option,
    album_id: str,
    *,
    page: int = 1,
    limit: int = 50,
    impl: str | None = None,
    domain_list: Sequence[str] | None = None,
) -> dict[str, Any]:
    """本子评论（含回评树）。"""
    client = make_client(option, impl=impl, domain_list=domain_list)
    result = client.album_pagination(str(album_id), page)

    def _one(comment) -> dict[str, Any]:
        return {
            "nickname": _safe(lambda: comment.nickname),
            "content": _safe(lambda: comment.content),
            "likes": _safe(lambda: comment.likes),
            "is_spoiler": _safe(lambda: comment.is_spoiler),
            "replies": [_one(reply) for reply in (_safe(lambda: comment.replies, []) or [])],
        }

    items: list[dict[str, Any]] = []
    for comment in result:
        items.append(_one(comment))
        if len(items) >= limit:
            break

    return {
        "album_id": str(album_id),
        "page": page,
        "total": _safe(lambda: result.total),
        "comment_count": _safe(lambda: result.comment_count),
        "count": len(items),
        "items": items,
    }


# ── 下载 ──────────────────────────────────────────────────────────────────

def download(
    option,
    ids: Sequence[str],
    *,
    dest: Path,
    export_dir: Path | None = None,
    library_dir: Path | None = None,
    photo: bool = False,
    dry_run: bool = False,
    login: bool = True,
    impl: str | None = None,
    domain_list: Sequence[str] | None = None,
) -> dict[str, Any]:
    """下载本子（或章节）。

    * ``dry_run``：只解析章节数与图片数，不落盘；
    * 落点由 ``dest`` 决定，红线目录一律拒绝。
    """
    dest = ensure_safe_dest(dest, export_dir=export_dir, library_dir=library_dir)
    ids = [str(i) for i in ids]
    if not ids:
        raise MangaError("没有要下载的 ID", code="NO_IDS", exit_code=EXIT_ERROR)

    if dry_run:
        client = make_client(option, impl=impl, domain_list=domain_list)
        plan = []
        for album_id in ids:
            album = client.get_album_detail(album_id)
            episodes = _safe(lambda: album.episode_list, []) or []
            plan.append(
                {
                    "id": album_id,
                    "title": _safe(lambda: album.name),
                    "episodes": len(episodes),
                    "images": _safe(lambda: album.page_count),
                }
            )
        return {"dry_run": True, "dest": str(dest), "kind": "photo" if photo else "album", "plan": plan}

    jm = _jmcomic()
    downloader = jm.download_photo if photo else jm.download_album

    try:
        result = downloader(ids, option, check_exception=False, login=login)
    except Exception as exc:
        raise _map_exception(exc, what="下载") from exc

    return _collect_download(result, dest=dest, kind="photo" if photo else "album")


def _collect_download(result, *, dest: Path, kind: str) -> dict[str, Any]:
    """把 jmcomic 的下载结果整理成稳定的 dict。"""
    failed = dict(_safe(lambda: getattr(result, "failed", None), {}) or {})
    succeeded: list[dict[str, Any]] = []
    files: list[str] = []

    for item in result:
        try:
            album, dler = item
        except (TypeError, ValueError):  # pragma: no cover - 上游返回形态变化
            succeeded.append({"id": str(item)})
            continue
        manifest = _safe(lambda: getattr(dler, "manifest", None))
        paths = list(_safe(lambda: manifest.image_filepath_list, []) or [])
        files.extend(paths)
        succeeded.append(
            {
                "id": str(_safe(lambda: album.album_id)),
                "title": _safe(lambda: album.name),
                "images": len(paths),
            }
        )

    return {
        "dry_run": False,
        "kind": kind,
        "dest": str(dest),
        "requested": len(failed) + len(succeeded),
        "succeeded": succeeded,
        "failed": {str(k): str(v) for k, v in failed.items()},
        "file_count": len(files),
        "files": files[:200],
    }


# ── 导出（jmcomic Feature） ──────────────────────────────────────────────

def export(
    option,
    album_id: str,
    *,
    fmt: str = "zip",
    dest: Path,
    export_dir: Path | None = None,
    library_dir: Path | None = None,
    filename_rule: str = "Atitle",
    login: bool = True,
    impl: str | None = None,
    domain_list: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把已下载的本子导出为 ZIP / PDF / 长图。

    走 jmcomic 的 ``Feature`` 机制（命中缓存即可导出，不必重新下载图片）。
    """
    if fmt not in EXPORT_FORMATS:
        raise MangaError(
            f"未知导出格式：{fmt}",
            code="BAD_FORMAT",
            exit_code=EXIT_ERROR,
            hint="可选：" + "、".join(EXPORT_FORMATS),
        )

    dest = ensure_safe_dest(dest, export_dir=export_dir, library_dir=library_dir)
    jm = _jmcomic()

    feature = getattr(jm.Feature, EXPORT_FORMATS[fmt])
    dir_kwarg = EXPORT_DIR_KWARGS.get(fmt, "")
    kwargs: dict[str, Any] = {"filename_rule": filename_rule}
    if dir_kwarg:
        kwargs[dir_kwarg] = str(dest)
    feature = feature(**kwargs)

    option.dir_rule.base_dir = str(dest)

    try:
        result = jm.download_album(str(album_id), option, extra=feature, login=login)
    except Exception as exc:
        raise _map_exception(exc, what="导出") from exc

    album, dler = result
    manifest = _safe(lambda: getattr(dler, "manifest", None))
    outputs: list[str] = []
    for suffix in ("zip", "pdf", "png"):
        try:
            outputs.extend(manifest.get_export_filepath_list(suffix))
        except Exception:  # pragma: no cover - 该格式没产出
            continue

    return {
        "id": str(_safe(lambda: album.album_id)),
        "title": _safe(lambda: album.name),
        "format": fmt,
        "dest": str(dest),
        "outputs": outputs,
        "output_count": len(outputs),
    }


# ── 域名 ──────────────────────────────────────────────────────────────────

def domains(*, include_html: bool = True) -> dict[str, Any]:
    """当前域名池（API 域名为静态池，网页域名需抓发布页）。"""
    jm = _jmcomic()
    api_list = list(_safe(lambda: jm.JmModuleConfig.DOMAIN_API_LIST, []) or [])
    html_list = list(_safe(lambda: jm.JmModuleConfig.DOMAIN_HTML_LIST, []) or [])
    html_error = None

    if include_html and not html_list:
        try:
            html_list = list(jm.JmModuleConfig.get_html_domain_all())
        except Exception as exc:
            html_error = str(exc)

    return {
        "api": api_list,
        "api_count": len(api_list),
        "html": html_list,
        "html_count": len(html_list),
        "html_error": html_error,
        "default_impl": _safe(lambda: jm.JmModuleConfig.DEFAULT_CLIENT_IMPL),
    }


def _probe(domain: str, timeout: float) -> dict[str, Any]:
    """探测一个域名：能不能连通、多久。"""
    started = time.monotonic()
    record: dict[str, Any] = {"domain": domain, "ok": False, "ms": None, "status": None, "error": None}
    try:
        import requests

        response = requests.get(f"https://{domain}/", timeout=timeout)
        record["status"] = response.status_code
        record["ok"] = response.status_code < 500
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["ms"] = round((time.monotonic() - started) * 1000, 1)
    return record


def pick_domain(
    option=None,
    *,
    timeout: float = 5.0,
    workers: int = 8,
    limit: int = 5,
) -> dict[str, Any]:
    """探测 API 域名池，选出最快可用的几个。

    站点域名天天变 —— 下载报「重试耗尽」时先跑这个。
    """
    jm = _jmcomic()
    candidates: list[str] = []

    if option is not None:
        for domain in _safe(lambda: option.client.domain, []) or []:
            if domain not in candidates:
                candidates.append(str(domain))
    for domain in _safe(lambda: jm.JmModuleConfig.DOMAIN_API_LIST, []) or []:
        if domain not in candidates:
            candidates.append(str(domain))

    if not candidates:
        return {"probed": 0, "best": [], "results": []}

    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(candidates)))) as pool:
        futures = {pool.submit(_probe, domain, timeout): domain for domain in candidates}
        for future in as_completed(futures):
            try:
                records.append(future.result())
            except Exception as exc:  # pragma: no cover - 探测线程自身异常
                records.append(
                    {"domain": futures[future], "ok": False, "ms": None, "status": None, "error": str(exc)}
                )

    records.sort(key=lambda r: (not r.get("ok"), r.get("ms") if r.get("ms") is not None else 1e9))
    return {
        "probed": len(records),
        "best": [r["domain"] for r in records if r.get("ok")][:limit],
        "results": records,
    }


# ── 异常映射 ──────────────────────────────────────────────────────────────

def _map_exception(exc: Exception, *, what: str) -> MangaError:
    """把上游异常映射成带退出码的领域异常。"""
    name = type(exc).__name__
    message = str(exc)
    low = message.lower()

    if name == "MissingAlbumPhotoException":
        return MangaError(
            f"{what}目标不存在：{getattr(exc, 'error_jmid', '')}",
            code="NOT_FOUND",
            exit_code=EXIT_NOTFOUND,
        )

    if name == "RequestRetryAllFailException":
        return MangaError(
            f"{what}重试耗尽（域名可能已失效）：{message}",
            code="DOMAIN_EXHAUSTED",
            exit_code=EXIT_ERROR,
            hint="先跑 manga domains / manga pick-domain，再重试",
        )

    if "login" in low or "登录" in message or "cookie" in low or "auth" in low:
        return MangaError(
            f"{what}需要登录：{message}",
            code="AUTH_REQUIRED",
            exit_code=EXIT_AUTH,
            hint="在 option 的 auth 段配置账号密码，或设 JM_USERNAME / JM_PASSWORD 环境变量",
        )

    return MangaError(
        f"{what}失败：{message}",
        code=(name or "ERROR").upper(),
        exit_code=EXIT_ERROR,
    )
