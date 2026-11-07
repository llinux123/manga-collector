"""作业模型（manga job）的测试。

这些用例**不起真下载** —— 起的是 `python -m manga version` 这种秒退的子进程，
验证的是「后台跑 / 轮询 / 取消 / 结果」这条链路的形状与语义。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from manga import jobs
from manga.envelope import EXIT_CANCELLED, MangaError


@pytest.fixture()
def store(workdir: Path) -> jobs.JobStore:
    return jobs.JobStore(workdir / "jobs")


class TestJobStore:
    def test_create_falls_back_to_second_candidate(self, workdir: Path) -> None:
        """第一个目录建不出来时，退到第二个，而不是整体失败。"""
        blocker = workdir / "blocked"
        blocker.write_text("x", encoding="utf-8")  # 用文件占住目录名 → mkdir 会失败
        chosen = jobs.JobStore.create([blocker / "jobs", workdir / "fallback"])
        assert chosen.root == workdir / "fallback"

    def test_missing_job_raises_not_found(self, store: jobs.JobStore) -> None:
        with pytest.raises(MangaError) as excinfo:
            store.load("no-such-job")
        assert excinfo.value.exit_code == 4
        assert excinfo.value.code == "JOB_NOT_FOUND"


class TestProgressProtocol:
    def test_record_progress_is_noop_outside_a_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(jobs.ENV_JOB_PROGRESS, raising=False)
        assert jobs.record_progress(done=["a"]) is False

    def test_record_progress_merges_instead_of_overwriting(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        path = workdir / "progress.json"
        monkeypatch.setenv(jobs.ENV_JOB_PROGRESS, str(path))

        assert jobs.record_progress(items=["a", "b"]) is True
        assert jobs.record_progress(done=["a"]) is True
        assert jobs.record_progress(failed=["b"], message="b 挂了") is True

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["items"] == ["a", "b"]
        assert payload["done"] == ["a"]
        assert payload["failed"] == ["b"]
        assert payload["message"] == "b 挂了"

    def test_record_progress_survives_corrupt_file(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """progress.json 写坏了也不能把子进程带崩 —— 进度只是给人看的。"""
        path = workdir / "progress.json"
        path.write_text("{ 坏掉的 json", encoding="utf-8")
        monkeypatch.setenv(jobs.ENV_JOB_PROGRESS, str(path))
        assert jobs.record_progress(done=["a"]) is True
        assert json.loads(path.read_text(encoding="utf-8"))["done"] == ["a"]


class TestLifecycle:
    def test_start_returns_immediately_and_job_succeeds(self, store: jobs.JobStore) -> None:
        state = jobs.start(store, "version", [])
        assert state.id
        assert state.state in (jobs.STATE_RUNNING, jobs.STATE_SUCCEEDED)

        # 等子进程写终态（终态由子进程自己写 —— 父进程启动完就退出了）
        _wait_terminal(store, state.id)

        assert jobs.current_state(store, state.id) == jobs.STATE_SUCCEEDED

        # 子进程 stdout 恰好是一个 JSON 信封 → 能被 result 解析
        envelope = store.read_envelope(state.id)
        assert envelope is not None
        assert envelope["ok"] is True
        assert "version" in envelope["data"]

    def test_final_state_is_written_by_the_child(self, store: jobs.JobStore) -> None:
        """终态由子进程写进 final.json —— 父进程写不了（它已经退出了）。"""
        state = jobs.start(store, "version", [])
        _wait_terminal(store, state.id)
        final = store.read_final(state.id)
        assert final is not None
        assert final["state"] == jobs.STATE_SUCCEEDED
        assert final["exit_code"] == 0

    def test_poll_reports_progress_shape(self, store: jobs.JobStore) -> None:
        state = jobs.start(store, "version", [], items=["x", "y"])
        data = jobs.poll(store, state.id)
        assert data["job_id"] == state.id
        assert data["items_total"] == 2
        assert data["items_done"] == 0
        assert data["finished"] is False or data["state"] in jobs.TERMINAL_STATES

    def test_result_contains_envelope_data(self, store: jobs.JobStore) -> None:
        state = jobs.start(store, "version", [])
        _wait_terminal(store, state.id)
        data = jobs.result(store, state.id)
        assert data["state"] == jobs.STATE_SUCCEEDED
        assert data["ok"] is True
        assert "version" in data["data"]

    def test_failed_job_keeps_error_message(self, store: jobs.JobStore) -> None:
        """子进程自己失败（这里用「不存在的选项」制造一个确定性的用法错误）。"""
        state = jobs.start(store, "version", ["--no-such-flag"])
        _wait_terminal(store, state.id)
        assert jobs.current_state(store, state.id) == jobs.STATE_FAILED

    def test_orphan_job_is_detected(self, store: jobs.JobStore) -> None:
        """子进程被强杀（没写终态）→ 状态判为 failed，并说明原因。"""
        state = jobs.start(store, "version", [])
        _wait_terminal(store, state.id)
        # 抹掉终态报告，模拟「子进程没来得及写」
        store.final_path(state.id).unlink(missing_ok=True)
        assert jobs.current_state(store, state.id) == jobs.STATE_FAILED
        data = jobs.poll(store, state.id)
        assert data["finished"] is True
        assert "终态" in (data["error"] or "")

    def test_cancel_of_finished_job_is_refused(self, store: jobs.JobStore) -> None:
        state = jobs.start(store, "version", [])
        _wait_terminal(store, state.id)
        with pytest.raises(MangaError) as excinfo:
            jobs.cancel(store, state.id)
        assert excinfo.value.code == "JOB_ALREADY_FINISHED"


class TestCancel:
    def test_cancel_flag_round_trip(self, workdir: Path) -> None:
        """取消标记：写进去就能读到。"""
        job_dir = workdir / "job"
        assert jobs.cancel_requested(job_dir) is False
        assert jobs.request_cancel(job_dir, reason="用户取消") is True
        assert jobs.cancel_requested(job_dir) is True

    def test_watchdog_exits_process_on_cancel_flag(self, workdir: Path) -> None:
        """**协作式取消**：子进程盯着标记，看到就自己退出（退出码 5）。

        这是取消能用的关键 —— 受限环境（沙箱 / 容器 / CI）里，
        外部去杀「别人创建的进程」常被拒，作业模型必须能自己停下来。
        """
        import subprocess

        job_dir = workdir / "job"
        job_dir.mkdir(parents=True, exist_ok=True)

        script = (
            "import sys, time;"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'src')!r});"
            "from manga import jobs;"
            "from pathlib import Path;"
            f"jobs.start_cancel_watchdog(Path({str(job_dir)!r}));"
            "print('watching', flush=True);"
            "time.sleep(60)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "watching"  # 看门狗已就位
            jobs.request_cancel(job_dir)
            code = proc.wait(timeout=20)
            assert code == jobs.EXIT_CANCELLED
        finally:
            if proc.poll() is None:  # pragma: no cover - 兜底
                proc.kill()
                proc.wait(timeout=10)

    def test_cancel_kills_process_and_lists_unfinished(self, store: jobs.JobStore) -> None:
        """起一个会睡很久的子进程，取消它 → 进程真被杀掉，未完成清单非空。"""
        state = jobs.start(store, "version", [], items=["a", "b", "c"])
        # 手工写一份进度：模拟「已完成 1 个」
        jobs.record_progress  # noqa: B018 - 只是引用，避免未使用告警
        _write_progress(store, state.id, {"items": ["a", "b", "c"], "done": ["a"]})

        data = jobs.cancel(store, state.id)
        assert data["state"] == jobs.STATE_CANCELLED
        assert data["completed"] == ["a"]
        assert "b" in data["unfinished"]
        assert data["unfinished_count"] == 2

        current = store.load(state.id)
        assert current.state == jobs.STATE_CANCELLED
        assert current.exit_code == EXIT_CANCELLED


class TestJobList:
    def test_list_returns_recent_jobs(self, store: jobs.JobStore) -> None:
        jobs.start(store, "version", [])
        states = store.list()
        assert len(states) >= 1
        assert states[0].command == "version"

    def test_cleanup_keeps_recent(self, store: jobs.JobStore) -> None:
        ids = [jobs.start(store, "version", []).id for _ in range(3)]
        removed = jobs.cleanup(store, keep=1)
        assert set(removed).issubset(set(ids))
        assert len(store.list(limit=10)) == 1


def _write_progress(store: jobs.JobStore, job_id: str, payload: dict) -> None:
    path = store.progress_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _wait_terminal(store: jobs.JobStore, job_id: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if jobs.current_state(store, job_id) in jobs.TERMINAL_STATES:
                return
        except MangaError:
            return
        time.sleep(0.1)
    raise AssertionError(f"作业 {job_id} 在 {timeout}s 内没进入终态")
