"""透传参数的红线扫描。

透传（``--config`` / ``-o key=value`` / ``--`` 之后的原样参数）把底层工具的完整参数面
开放给了调用方 —— 这也包括**落点参数**。如果调用方透传一个指向共享区的路径
（比如 gallery-dl 的 ``-o base-directory=…``、mangadex 的 ``--path``），
就会绕过 :func:`~manga.adapters.jmcomic.ensure_safe_dest` 的守卫，把文件写进
``Export\\Manga``（Syncthing 共享区）。

本模块在透传**之前**扫一遍参数，命中红线目录就拒绝（退出码 6）。

**防线定位**：防的是**误操作**（agent/用户把灰产写进共享区），不是恶意对抗 ——
真要绕过有一百种方法，但那不是我们的威胁模型。我们只要求：
「明显指向红线目录的参数」必须被拦下并给出清楚的错误。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from .adapters.jmcomic import is_inside
from .envelope import RedLineViolation

#: 各下载器**已知的落点参数名**（长/短形式，不带前导横线）。
#: 出现在 ``--path <值>`` 或 ``--path=<值>`` 或 ``-o key=value`` 里都要查。
_DEST_KEYS = frozenset(
    {
        "path",
        "dest",
        "destination",
        "directory",
        "base-directory",  # gallery-dl
        "download-path",
        "output",
        "output-path",
        "save-path",
    }
)

#: ``key=value`` / ``--key=value`` / ``-o key=value`` 三种形态
_KV_RE = re.compile(r"^-{0,2}(?:o\s+)?(?P<key>[A-Za-z][\w.-]*)\s*=\s*(?P<value>.+)$")


def _looks_like_path(text: str) -> bool:
    """粗略判断一个参数值是不是路径（绝对路径、~ 开头、带盘符）。"""
    if not text:
        return False
    if text.startswith("~"):
        return True
    if len(text) >= 3 and text[1] == ":" and text[2] in ("\\", "/"):  # C:\... / C:/...
        return True
    return text.startswith(("/", "\\\\"))


def scan_passthrough_args(
    args: Sequence[str],
    *,
    export_dir: Path | None,
    library_dir: Path | None,
) -> list[str]:
    """扫描透传参数，返回命中的红线违规描述列表（空 = 安全）。

    检查三类：

    1. ``--dest <路径>`` / ``--path <路径>`` 形式：值是指向下一个参数的已知落点参数；
    2. ``--dest=<路径>`` / ``-o base-directory=<路径>`` 形式：key=value 里嵌的路径；
    3. 裸路径参数：值本身就是绝对路径。
    """
    violations: list[str] = []
    args = [str(a) for a in args]

    for i, arg in enumerate(args):
        # 形态 1：--key <值>（值在下一个位置）
        key_match = re.fullmatch(r"-{1,2}([A-Za-z][\w.-]*)", arg)
        if key_match and key_match.group(1).lower() in _DEST_KEYS and i + 1 < len(args):
            violations.extend(_check_value(key_match.group(1), args[i + 1], export_dir, library_dir))
            continue

        # 形态 2：key=value（含 -o key=value）
        kv = _KV_RE.match(arg)
        if kv and kv.group("key").lower() in _DEST_KEYS:
            violations.extend(_check_value(kv.group("key"), kv.group("value"), export_dir, library_dir))
            continue

        # 形态 3：裸路径
        if _looks_like_path(arg):
            violations.extend(_check_value("(裸路径)", arg, export_dir, library_dir))

    return violations


def _check_value(
    key: str,
    value: str,
    export_dir: Path | None,
    library_dir: Path | None,
) -> list[str]:
    """单个参数值的路径红线检查。"""
    if not _looks_like_path(value):
        return []
    try:
        target = Path(value).expanduser().resolve()
    except OSError:
        return []

    hits: list[str] = []
    for label, root in (("共享区 Export\\Manga", export_dir), ("权威库 Manga\\Library", library_dir)):
        if root is None:
            continue
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            continue
        if is_inside(target, root_resolved):
            hits.append(f"透传参数 `{key}` 指向{label}：{value}")
    return hits


def assert_passthrough_safe(
    args: Sequence[str],
    *,
    export_dir: Path | None,
    library_dir: Path | None,
) -> None:
    """透传前的守门：有违规就抛红线（退出码 6）。"""
    violations = scan_passthrough_args(args, export_dir=export_dir, library_dir=library_dir)
    if violations:
        raise RedLineViolation(
            "红线：透传参数指向红线目录（" + "；".join(violations) + "）",
            data={"violations": violations},
        )
