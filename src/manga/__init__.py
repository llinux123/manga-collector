"""manga —— 漫画采集聚合层的薄 CLI。

定位：**Skill 是界面，CLI 是手，agent 是大脑。**
本包只做确定性的执行与守门，把决策留给 agent（见 ``.agents/skills/manga/SKILL.md``）。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

#: 包名（与 pyproject.toml 的 ``[project] name`` 一致）
DIST_NAME = "manga-collector"

#: 版本号的**唯一来源**：装好的发行包元数据。
#:
#: 之前版本号写死在两处（pyproject.toml 与本文件），改一处忘另一处 →
#: ``manga version`` 报 0.2.0 而 ``importlib.metadata`` 报 0.1.0，
#: agent 拿到的版本号是错的。现在只认发行元数据。
#:
#: 从源码直接跑（没装成包）时元数据不存在 → 用这个兜底常量，
#: 它必须与 pyproject.toml 的 ``version`` 保持一致（由测试守住）。
_FALLBACK_VERSION = "0.3.0"

try:
    __version__ = _dist_version(DIST_NAME)
except PackageNotFoundError:  # pragma: no cover - 源码树里直接跑
    __version__ = _FALLBACK_VERSION

from .envelope import EXIT_CANCELLED, EXIT_ERROR, EXIT_NOTFOUND, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Envelope

__all__ = [
    "Envelope",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_ERROR",
    "EXIT_NOTFOUND",
    "EXIT_CANCELLED",
    "EXIT_REFUSED",
    "DIST_NAME",
    "__version__",
]
