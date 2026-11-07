"""manga —— 漫画采集聚合层的薄 CLI。

定位：**Skill 是界面，CLI 是手，agent 是大脑。**
本包只做确定性的执行与守门，把决策留给 agent（见 ``.agents/skills/manga/SKILL.md``）。
"""

from __future__ import annotations

__version__ = "0.2.0"

from .envelope import EXIT_CANCELLED, EXIT_ERROR, EXIT_NOTFOUND, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Envelope

__all__ = [
    "Envelope",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_ERROR",
    "EXIT_NOTFOUND",
    "EXIT_CANCELLED",
    "EXIT_REFUSED",
    "__version__",
]
