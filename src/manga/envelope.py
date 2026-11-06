"""统一输出信封与退出码。

设计约束（来自 docs/manga-skill-plan.md P0）：

* agent 解析的**只有一种形状**，成功与失败都是同一个 JSON 对象；
* 退出码语义固定，agent 不必解析 stdout 判断成败；
* 与既有 PowerShell 脚本的退出码语义对齐：
  PS ``0`` → CLI ``0``；PS ``1``（告警/冲突）→ CLI ``0`` 但 ``warnings`` 非空；PS ``2``（错误）→ CLI ``2``。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

# ── 退出码 ────────────────────────────────────────────────────────────────
EXIT_OK = 0            # 成功（可能带 warnings）
EXIT_USAGE = 1         # 参数 / 用法错误
EXIT_ERROR = 2         # 执行错误（与 PS 脚本退出码 2 对齐）
EXIT_AUTH = 3          # 需要登录 / 凭据
EXIT_NOTFOUND = 4      # 资源不存在
EXIT_CANCELLED = 5     # 被取消
EXIT_REFUSED = 6       # 红线拒绝（不提供该能力）


@dataclass(slots=True)
class Envelope:
    """一次命令调用的结果信封。"""

    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    exit: int = EXIT_OK

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "data": self.data,
                "error": self.error,
                "warnings": self.warnings,
                "exit": self.exit,
            },
            ensure_ascii=False,
            indent=2,
        )


class MangaError(Exception):
    """带错误码的领域异常。``code`` 与 ``exit`` 一起决定 agent 的后续动作。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "ERROR",
        exit_code: int = EXIT_ERROR,
        hint: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.hint = hint
        self.data = data or {}

    def to_envelope(self) -> Envelope:
        return Envelope(
            ok=False,
            data=self.data,
            error={"code": self.code, "message": self.message, "hint": self.hint},
            warnings=[],
            exit=self.exit_code,
        )


class RedLineViolation(MangaError):
    """红线被触发：灰色来源不得进入 Syncthing 共享区。"""

    def __init__(self, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="RED_LINE", exit_code=EXIT_REFUSED, data=data)


def emit(env: Envelope) -> int:
    """把信封写到 stdout 并返回退出码。"""
    sys.stdout.write(env.to_json() + "\n")
    sys.stdout.flush()
    return env.exit
