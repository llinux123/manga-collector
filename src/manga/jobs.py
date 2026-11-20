"""长任务作业模型（job model）。

**为什么需要它**：一次真实下载可能跑几分钟到几十分钟。agent 如果一直阻塞等，
就会浪费模型调用、超时、拿不到中间状态。作业模型把长任务拆成四步：

====================== ==========================================================
命令                    作用
====================== ==========================================================
``manga job start``    立刻返回 job id，后台跑，**不等**
``manga job poll``     低频轮询：精简状态（几秒就返回）
``manga job cancel``   真正停掉（杀进程树），给出已完成 / 未完成清单
``manga job result``   最终摘要：子进程的信封 + 逐项结果
====================== ==========================================================

**状态落文件**，所以 ``poll`` / ``result`` 可以是**另一个进程**来读 —— agent
每次轮询都是一次独立的短命令，不需要保持连接。

目录布局::

    <jobs_root>/<job_id>/
    ├── state.json      # 作业状态（父进程写）
    ├── progress.json   # 逐项进度（子进程写）
    ├── envelope.json   # 子进程的 stdout —— 恰好一个 JSON 信封
    └── error.log       # 子进程的 stderr

**进度协议**：子进程通过环境变量 ``MANGA_JOB_PROGRESS`` 拿到 progress.json 的路径，
随时调用 :func:`record_progress` 写进去。父进程只读不写 —— 单向，���可能互相踩坏。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .envelope import EXIT_CANCELLED, EXIT_ERROR, EXIT_NOTFOUND, MangaError

# ── 状态 ──────────────────────────────────────────────────────────────────

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED})

#: 子进程用来回报进度的环境变量
ENV_JOB_ID = "MANGA_JOB_ID"
ENV_JOB_PROGRESS = "MANGA_JOB_PROGRESS"
#: 子进程写终态报告的路径
ENV_JOB_FINAL = "MANGA_JOB_FINAL"
#: 作业目录（子进程靠它找到「取消标记」与终态文件）
ENV_JOB_DIR = "MANGA_JOB_DIR"

#: 取消标记文件：``job cancel`` 写它，子进程盯着它
CANCEL_FLAG = "cancel.request"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_job_id() -> str:
    """``20261107-173355-a1b2c3``：时间前缀方便按时间排序。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


# ── 状态对象 ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class JobState:
    """一个作业的状态（父进程写，子进程只读 progress）。"""

    id: str
    command: str
    argv: list[str] = field(default_factory=list)
    state: str = STATE_PENDING
    pid: int | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    items: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    message: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "argv": self.argv,
            "state": self.state,
            "pid": self.pid,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "items": self.items,
            "done": self.done,
            "failed": self.failed,
            "message": self.message,
            "error": self.error,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JobState:
        return cls(
            id=str(raw["id"]),
            command=str(raw.get("command", "")),
            argv=list(raw.get("argv", [])),
            state=str(raw.get("state", STATE_PENDING)),
            pid=raw.get("pid"),
            created_at=str(raw.get("created_at", "")),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            exit_code=raw.get("exit_code"),
            items=list(raw.get("items", [])),
            done=list(raw.get("done", [])),
            failed=list(raw.get("failed", [])),
            message=raw.get("message"),
            error=raw.get("error"),
            warnings=list(raw.get("warnings", [])),
        )


# ── 存储 ──────────────────────────────────────────────────────────────────


class JobStore:
    """作业目录的读写。

    根目录优先用书库的 ``Manga\\Metadata\\jobs``（跟其它工具产物在一起），
    建不出来就退到系统临时目录 —— 作业状态是**临时**的，不该因为书库不可写就整个失败。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(cls, candidates: Sequence[Path]) -> JobStore:
        """按顺序挑第一个**能写**的目录。"""
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".writable"
                probe.write_text("x", encoding="utf-8")
                probe.unlink()
                return cls(candidate)
            except OSError:
                continue
        raise MangaError(
            "找不到可写的作业目录",
            code="JOBS_DIR_FAILED",
            exit_code=EXIT_ERROR,
            hint="检查书库目录权限，或设 TEMP 指向可写目录后重试",
        )

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def state_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "state.json"

    def progress_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "progress.json"

    def final_path(self, job_id: str) -> Path:
        """子进程写的终态报告。

        **与 ``state.json`` 分开**是刻意的：父进程启动完就退出了（它不该等着），
        父写的 state.json 与子写的终态如果挤在一个文件里就会互相覆盖 ——
        子进程极快结束时，父进程的收尾写入会把终态**盖回** running。
        分成两个文件就没有这个竞态。
        """
        return self.job_dir(job_id) / "final.json"

    def envelope_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "envelope.json"

    def error_log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "error.log"

    def exists(self, job_id: str) -> bool:
        return self.state_path(job_id).is_file()

    def save(self, state: JobState) -> None:
        path = self.state_path(state.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, state.as_dict())

    def load(self, job_id: str) -> JobState:
        path = self.state_path(job_id)
        if not path.is_file():
            raise MangaError(
                f"找不到作业：{job_id}",
                code="JOB_NOT_FOUND",
                exit_code=EXIT_NOTFOUND,
                hint="用 `manga job list` 看现有作业",
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return JobState.from_dict(raw)

    def list(self, limit: int = 20) -> list[JobState]:
        """列出最近的作业。

        ⚠ 状态取的是**真实**状态（:func:`_effective_state`）—— 父进程写的
        ``state.json`` 永远是「启动时的样子」（running），子进程真正的终态在
        ``final.json`` 里。之前这里直接读 state.json → 列表里所有跑完的作业
        仍显示 running。
        """
        if not self.root.is_dir():
            return []
        states: list[JobState] = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if not (directory / "state.json").is_file():
                continue
            try:
                state = self.load(directory.name)
            except (OSError, ValueError):
                continue
            effective, final = _effective_state(self, state)
            if effective != state.state:
                state.state = effective
                if final:
                    state.exit_code = final.get("exit_code", state.exit_code)
                    state.finished_at = final.get("finished_at", state.finished_at)
            states.append(state)
            if len(states) >= limit:
                break
        return states

    def read_progress(self, job_id: str) -> dict[str, Any]:
        path = self.progress_path(job_id)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def read_final(self, job_id: str) -> dict[str, Any] | None:
        """子进程写的终态报告；没有就返回 ``None``（还在跑）。"""
        path = self.final_path(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def read_envelope(self, job_id: str) -> dict[str, Any] | None:
        """子进程 stdout —— 恰好一个 JSON 信封；还没写完就返回 ``None``。"""
        path = self.envelope_path(job_id)
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None  # 还在写

    def tail_error(self, job_id: str, limit: int = 800) -> str | None:
        path = self.error_log_path(job_id)
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not text:
            return None
        return text[-limit:]


def _write_json(path: Path, payload: Any) -> None:
    """原子写：先写临时文件再 replace，避免读到写了一半的 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── 进度协议（子进程侧） ──────────────────────────────────────────────────


def record_progress(
    *,
    items: Iterable[str] | None = None,
    done: Iterable[str] | None = None,
    failed: Iterable[str] | None = None,
    message: str | None = None,
) -> bool:
    """子进程写进度。**没在作业里跑就什么都不做**（普通命令不受影响）。

    单向协议：子进程只写 progress.json，父进程只读 —— 不会互相踩坏。
    """
    path_text = os.environ.get(ENV_JOB_PROGRESS)
    if not path_text:
        return False
    path = Path(path_text)
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}

    def merge(key: str, values: Iterable[str] | None) -> None:
        if values is None:
            return
        merged = list(current.get(key, []))
        for value in values:
            if value not in merged:
                merged.append(value)
        current[key] = merged

    merge("items", items)
    merge("done", done)
    merge("failed", failed)
    if message is not None:
        current["message"] = message
    current["updated_at"] = _now()
    try:
        _write_json(path, current)
    except OSError:
        return False
    return True


# ── 启动 ──────────────────────────────────────────────────────────────────


def start(
    store: JobStore,
    command: str,
    args: Sequence[str] = (),
    *,
    items: Sequence[str] = (),
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    timeout: float | None = None,
) -> JobState:
    """后台启动一个作业，**立刻返回**（不等待）。

    子进程 = ``python -m manga <command> <args...>`` —— 走的是和 agent 直接
    调用完全一样的入口，所以行为一致、退出码一致。

    ``env_extra`` 用来给子进程补环境变量（比如把 ``--library-root`` 换成
    ``MANGA_LIBRARY_ROOT``）—— **不要**把路径拼成命令行参数：不是每个命令
    都认 ``--library-root``（``version`` 就不认），拼上去会让子进程直接用法错。
    """
    job_id = _new_job_id()
    job_dir = store.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    argv = [sys.executable, "-m", "manga", command, *[str(a) for a in args]]
    state = JobState(
        id=job_id,
        command=command,
        argv=argv[1:],
        state=STATE_RUNNING,
        items=[str(i) for i in items],
    )
    state.started_at = _now()
    # 先落盘再启动：子进程可能秒退，终态报告要能写在 state.json 之后
    store.save(state)

    env = os.environ.copy()
    env[ENV_JOB_ID] = job_id
    env[ENV_JOB_PROGRESS] = str(store.progress_path(job_id))
    env[ENV_JOB_FINAL] = str(store.final_path(job_id))
    env[ENV_JOB_DIR] = str(job_dir)
    env["PYTHONUNBUFFERED"] = "1"
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})

    creationflags = 0
    if sys.platform == "win32":
        # 独立进程组：取消时要能连同子进程一起杀
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    # stdout / stderr 分开：stdout 必须是**纯 JSON 信封**，才能被 result 解析
    with store.envelope_path(job_id).open("wb") as out, store.error_log_path(job_id).open("wb") as err:
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv 是列表，不走 shell
                argv,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                cwd=str(cwd) if cwd else str(job_dir),
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise MangaError(
                f"作业启动失败：{exc}",
                code="JOB_START_FAILED",
                exit_code=EXIT_ERROR,
                hint=f"确认可以执行：{' '.join(argv)}",
            ) from exc

    state.pid = proc.pid
    store.save(state)
    return state


# ── 取消：协作式优先 ──────────────────────────────────────────────────────


def request_cancel(job_dir: Path, reason: str = "用户取消") -> bool:
    """写取消标记 —— **不需要任何特权**。

    为什么不让「取消方」去杀进程：很多受限环境（DSH 沙箱、容器、CI）
    只允许**杀自己创建的子进程**。``manga job start`` 和 ``manga job cancel``
    是两个独立的 CLI 调用（兄弟进程），外部杀法会被拒。

    所以改成：取消方只**写一个标记文件**，子进程自己盯着它、优雅退出。
    外部杀法退化成兜底（标记没被响应时才用）。
    """
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / CANCEL_FLAG).write_text(reason, encoding="utf-8")
        return True
    except OSError:
        return False


def cancel_requested(job_dir: Path) -> bool:
    """取消标记在不在？"""
    try:
        return (job_dir / CANCEL_FLAG).is_file()
    except OSError:
        return False


def start_cancel_watchdog(
    job_dir: Path,
    *,
    interval: float = 0.4,
    on_cancel: Any = None,
    grace: float = 5.0,
) -> None:
    """子进程侧：盯着取消标记，看到就自己退出。

    **两级取消**：

    1. ``on_cancel`` 给了就先调它（比如 jmcomic 的 ``DownloadControl.cancel()``），
       给最多 ``grace`` 秒让工作**优雅**收尾（断点续传已下的部分、写进度）；
    2. 超时还没退出 → ``os._exit(EXIT_CANCELLED)``（立刻，不等栈展开）。

    没有 ``on_cancel`` 时行为和原来一样（看到标记直接退）。
    """
    import threading

    def _watch() -> None:
        while True:
            try:
                if cancel_requested(job_dir):
                    if on_cancel is not None:
                        try:
                            on_cancel()
                        except Exception:  # noqa: BLE001 - 取消回调炸了也得能退
                            pass
                        # 给优雅收尾最多 grace 秒
                        deadline = time.time() + grace
                        while time.time() < deadline:
                            # 如果主线程在收尾时自己结束了，这个守护线程会随进程退出
                            time.sleep(0.3)
                    _write_final(
                        job_dir,
                        {"state": STATE_CANCELLED, "exit_code": EXIT_CANCELLED, "error": "已被取消"},
                    )
                    os._exit(EXIT_CANCELLED)
            except Exception:  # noqa: BLE001 - 看门狗不该自己崩
                pass
            time.sleep(interval)

    threading.Thread(target=_watch, name="job-cancel-watchdog", daemon=True).start()


def _write_final(job_dir: Path, payload: dict[str, Any]) -> None:
    try:
        _write_json(job_dir / "final.json", {**payload, "finished_at": _now()})
    except OSError:
        pass


# ── 终态上报（子进程侧） ──────────────────────────────────────────────────


def finalize(envelope: dict[str, Any]) -> bool:
    """子进程在退出前写终态报告。

    **为什么不让父进程写**：父进程启动完作业就退出了（它不该替用户等着），
    等它回收子进程的那条线程也随之消失 → 状态会永远停在 running。
    所以终态由**子进程自己**写（它知道自己几时退出、退出码是多少）。

    不在作业里跑就什么都不做。

    ⚠ **退出码 0 不等于成功**：子进程可能自己报了 ``ok=false``（下载失败、
    红线拒绝…）却仍以 0 退出。这种情况必须判为 failed —— 否则作业显示
    succeeded，agent 以为拿到结果了。
    """
    path_text = os.environ.get(ENV_JOB_FINAL)
    if not path_text:
        return False
    exit_code = int(envelope.get("exit", 0) or 0)
    ok_field = envelope.get("ok")
    # ok 字段缺省（老信封）时按退出码判断；显式为 false 一律算失败。
    succeeded = exit_code == 0 and ok_field is not False
    if exit_code == EXIT_CANCELLED:
        state = STATE_CANCELLED
    elif succeeded:
        state = STATE_SUCCEEDED
    else:
        state = STATE_FAILED
    error = envelope.get("error")
    payload = {
        "state": state,
        "exit_code": exit_code,
        "ok": bool(ok_field) if ok_field is not None else succeeded,
        "warnings": list(envelope.get("warnings") or []),
        "error": error.get("message") if isinstance(error, dict) else error,
        "data": envelope.get("data"),
        "finished_at": _now(),
    }
    try:
        _write_json(Path(path_text), payload)
    except OSError:
        return False
    return True


def _pid_alive(pid: int | None) -> bool:
    """进程还在吗？（用来发现「子进程没了却没写终态」的孤儿作业）

    ⚠ Windows：``OpenProcess`` 成功**不代表进程还活着** —— 只要句柄还被
    内核对象引用着（例如 PID 被复用、或退出后对象尚未释放），已退出的进程
    也能打开。必须再查退出码：``STILL_ACTIVE (259)`` 才是真的在跑。
    """
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True  # 查不了退出码 → 保守当它还在
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - 体检性问题，不该影响主流程
            return True  # 判断不了就当它还在
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _effective_state(store: JobStore, state: JobState) -> tuple[str, dict[str, Any] | None]:
    """作业当前状态：优先用子进程写的终态，其次父进程的记录。

    子进程被杀（没机会写终态）时 → 状态判为 failed，附一句说明。
    """
    final = store.read_final(state.id)
    if final is not None:
        return str(final.get("state", state.state)), final
    if state.state == STATE_RUNNING and not _pid_alive(state.pid):
        return STATE_FAILED, {
            "state": STATE_FAILED,
            "exit_code": None,
            "error": "子进程已退出但没写终态（被强杀或崩溃）",
            "warnings": [],
        }
    return state.state, None


# ── 轮询 / 取消 / 结果 ────────────────────────────────────────────────────


def _elapsed_seconds(state: JobState, *, final: dict[str, Any] | None = None) -> float:
    """作业耗时（秒）。

    终态作业的耗时必须**固定**：子进程的 ``final.json`` 里有 ``finished_at``，
    用它算；没有才回退到「现在」—— 否则每轮 poll 的耗时都在涨，agent
    无法用它判断作业跑了多久。
    """
    if not state.started_at:
        return 0.0
    end = state.finished_at or (final or {}).get("finished_at") or _now()
    try:
        start_dt = datetime.fromisoformat(state.started_at)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return 0.0
    return max(0.0, (end_dt - start_dt).total_seconds())


def current_state(store: JobStore, job_id: str) -> str:
    """作业当前状态（终态优先）—— 给外部轮询用。"""
    state = store.load(job_id)
    return _effective_state(store, state)[0]


def poll(store: JobStore, job_id: str) -> dict[str, Any]:
    """精简状态 —— 给 agent 低频轮询用。**不含**大块输出。"""
    state = store.load(job_id)
    progress = store.read_progress(job_id)
    done = list(progress.get("done", state.done))
    failed = list(progress.get("failed", state.failed))
    items = list(progress.get("items", state.items))
    current, final = _effective_state(store, state)

    return {
        "job_id": state.id,
        "state": current,
        "command": state.command,
        "elapsed_s": round(_elapsed_seconds(state, final=final), 1),
        "items_total": len(items),
        "items_done": len(done),
        "items_failed": len(failed),
        "remaining": max(0, len(items) - len(done) - len(failed)),
        "message": progress.get("message") or state.message,
        "exit_code": (final or {}).get("exit_code", state.exit_code),
        "error": (final or {}).get("error") or state.error,
        "done": done,
        "failed": failed,
        "finished": current in TERMINAL_STATES,
    }


def cancel(store: JobStore, job_id: str, *, grace: float = 6.0) -> dict[str, Any]:
    """取消作业，并给出**已完成 / 未完成**清单。

    **先协作、后强杀**：

    1. 写取消标记 → 子进程自己看到并优雅退出（**不需要任何特权**）；
    2. 等它退出（最多 ``grace`` 秒）；
    3. 还活着才用强杀兜底（taskkill / CTRL_BREAK / SIGTERM 逐个试）。

    ⚠ **「说了就算」**：如果既写不了取消标记、强杀也没杀掉进程，
    就**不能**报 cancelled —— 子进程还在跑、还在往库里写文件，状态却说
    「已停」，agent 就会重跑并把产物踩坏。这种情况抛 ``CANCEL_FAILED``
    （退出码 2），状态保持原样。
    """
    state = store.load(job_id)
    current, _ = _effective_state(store, state)
    if current in TERMINAL_STATES:
        raise MangaError(
            f"作业已结束（{current}），无法取消",
            code="JOB_ALREADY_FINISHED",
            exit_code=EXIT_ERROR,
            hint="用 `manga job result` 看最终结果",
        )

    job_dir = store.job_dir(job_id)
    asked = request_cancel(job_dir, reason="用户取消")

    # 等子进程自己退出
    exited = False
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(state.pid):
            exited = True
            break
        time.sleep(0.2)

    killed = False
    if not exited:
        killed = _kill_tree(state.pid)
        # 强杀只是「发出去了」—— 要确认进程真的死了才算数
        if killed:
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not _pid_alive(state.pid):
                    break
                time.sleep(0.2)
            exited = not _pid_alive(state.pid)

    progress = store.read_progress(job_id)
    done = list(progress.get("done", state.done))
    failed = list(progress.get("failed", state.failed))
    items = list(progress.get("items", state.items))
    unfinished = [i for i in items if i not in done and i not in failed]

    # 进程还活着 → 取消**没有生效**。绝不能写 cancelled（会骗 agent）。
    if not exited:
        raise MangaError(
            f"取消失败：作业 {job_id} 的进程仍在运行（PID {state.pid}）",
            code="CANCEL_FAILED",
            exit_code=EXIT_ERROR,
            hint=(
                "取消标记"
                + ("已写入但子进程没响应" if asked else "写不进去")
                + "；强杀也被环境拒绝。请在宿主上直接结束该进程，"
                "或改用协作式取消（子进程自己盯着 cancel.request）"
            ),
            data={
                "job_id": job_id,
                "pid": state.pid,
                "cancel_requested": asked,
                "killed": killed,
                "completed": done,
                "unfinished": unfinished,
            },
        )

    state.state = STATE_CANCELLED
    state.finished_at = _now()
    state.exit_code = EXIT_CANCELLED
    state.done = done
    state.failed = failed
    state.message = state.message or "已被取消"
    store.save(state)
    _write_final(
        job_dir,
        {
            "state": STATE_CANCELLED,
            "exit_code": EXIT_CANCELLED,
            "error": "已被取消",
            "warnings": [],
        },
    )

    return {
        "job_id": job_id,
        "state": STATE_CANCELLED,
        "cancel_requested": asked,
        "exited_by_itself": exited,
        "killed": killed,
        "items_total": len(items),
        "completed": done,
        "failed": failed,
        "unfinished": unfinished,
        "completed_count": len(done),
        "unfinished_count": len(unfinished),
    }


def _kill_tree(pid: int | None) -> bool:
    """强杀进程树 —— **兜底**，逐个尝试，成功即返回。

    受限环境里这些手段可能全被拒（沙箱只允许杀自己的子进程），
    所以真正管用的是上面的**协作式取消**。
    """
    if not pid:
        return False

    if sys.platform == "win32":
        # ① taskkill（带 /T 杀整棵树）
        try:
            proc = subprocess.run(  # noqa: S603 - argv 列表
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0 and _wait_dead(pid):
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        # ② CTRL_BREAK：给整个进程组发中断（不需要特权）
        #
        # ⚠ 「信号发出去了」≠「进程死了」—— CTRL_BREAK 可能被忽略或延迟处理。
        # 必须轮询确认真的退出了才算强杀成功，否则调用方会以为它停了，
        # 实际子进程还在下载、还在往库里写文件。
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except (OSError, AttributeError, ValueError):
            return False
        return _wait_dead(pid)

    # POSIX：先杀进程组，再退回单进程
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        if _wait_dead(pid):
            return True
    except (OSError, ProcessLookupError):
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return _wait_dead(pid)


def _wait_dead(pid: int | None, *, timeout: float = 3.0) -> bool:
    """等进程真的退出（最多 ``timeout`` 秒）。

    信号只是「请求」—— 进程可能正在收尾、也可能完全无视。确认它不在了
    才算强杀成功。
    """
    if not pid:
        return False
    deadline = time.time() + timeout
    while True:
        if not _pid_alive(pid):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.1)


def result(store: JobStore, job_id: str) -> dict[str, Any]:
    """最终摘要：子进程的信封 + 逐项结果。"""
    state = store.load(job_id)
    envelope = store.read_envelope(job_id)
    progress = store.read_progress(job_id)
    current, final = _effective_state(store, state)

    payload: dict[str, Any] = {
        "job_id": state.id,
        "state": current,
        "command": state.command,
        "elapsed_s": round(_elapsed_seconds(state, final=final), 1),
        "exit_code": (final or {}).get("exit_code", state.exit_code),
        "items": list(progress.get("items", state.items)),
        "done": list(progress.get("done", state.done)),
        "failed": list(progress.get("failed", state.failed)),
        "warnings": list((final or {}).get("warnings") or state.warnings),
        "error": (final or {}).get("error") or state.error,
    }
    if envelope:
        payload["ok"] = bool(envelope.get("ok"))
        payload["data"] = envelope.get("data")
        payload["warnings"] = list(envelope.get("warnings") or payload["warnings"])
        if isinstance(envelope.get("error"), dict):
            payload["error"] = envelope["error"].get("message")
    elif final and final.get("data") is not None:
        payload["ok"] = current == STATE_SUCCEEDED
        payload["data"] = final.get("data")
    tail = store.tail_error(job_id)
    if tail:
        payload["stderr_tail"] = tail
    return payload


def cleanup(store: JobStore, keep: int = 20) -> list[str]:
    """删掉最老的作业目录，返回被删的 id。"""
    states = store.list(limit=10_000)
    removed: list[str] = []
    for state in states[keep:]:
        shutil.rmtree(store.job_dir(state.id), ignore_errors=True)
        removed.append(state.id)
    return removed
