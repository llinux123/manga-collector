"""采集侧适配器。

每个适配器把一个外部工具的调用翻译成统一的 JSON 信封数据，
并把「工具特有的坑」挡在适配层，不外泄给 agent。

* ``jmcomic`` —— 禁漫（灰色来源）：搜索/详情/评论/下载/导出/域名
* ``gallery_dl`` —— gallery-dl（通用站）：URL 预检 + 下载
* ``mangadex_dl`` —— mangadex-downloader（MangaDex 专项）
* ``pipeline`` —— manga 工作区的 PowerShell 流水线脚本编排
"""

from __future__ import annotations

__all__ = ["gallery_dl", "jmcomic", "mangadex_dl", "pipeline"]
