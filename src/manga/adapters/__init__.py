"""采集侧适配器。

每个适配器把一个外部工具的调用翻译成统一的 JSON 信封数据，
并把「工具特有的坑」挡在适配层，不外泄给 agent。
"""

from __future__ import annotations

__all__ = ["pipeline"]
