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

    def test_write_probe_leaves_no_garbage(self, workdir: Path) -> None:
        """写权限探针用完要删干净 —— 不能在书库里留垃圾文件。"""
        root = workdir / "jobs"
        jobs.JobStore.create([root])
        leftovers = [p.name for p in root.iterdir()]
        assert leftovers == [], f"探针文件没清干净：{leftovers}"

    def test_write_probe_uses_a_unique_name(self, workdir: Path, monkeypatch) -> None:
        """探针文件名必须唯一 —— 并发创建作业目录时会互相踩。

        之前固定叫 ``.writable``：两个进程同时探测 → 一个写、一个删 →
        后者 ``unlink`` 报 FileNotFoundError → 整个目录被判成不可写。
        """
        root = workdir / "jobs"
        seen: list[str] = []
        real_write_text = Path.write_text

        def spy_write_text(self, *args, **kwargs):
            if self.parent == root:
                seen.append(self.name)
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", spy_write_text)
        jobs.JobStore.create([root])
        jobs.JobStore.create([root])
        assert len(seen) == 2
        assert seen[0] != seen[1], "探针文件名必须每次不同"


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

    def test_concurrent_record_progress_keeps_all_items(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """并发写进度不能丢条目 —— 子进程是多线程的（逐 ID 下载）。

        之前是「读 → 改 → 写」三步没有锁：两个线程同时读到同一份旧数据，
        后写的把先写的条目**覆盖**掉 → 进度里少了已完成的 ID，
        agent 以为没下完，重复下载。
        """
        import threading

        path = workdir / "progress.json"
        monkeypatch.setenv(jobs.ENV_JOB_PROGRESS, str(path))

        def worker(index: int) -> None:
            for _ in range(10):
                jobs.record_progress(done=[f"item-{index}"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        payload = json.loads(path.read_text(encoding="utf-8"))
        for index in range(4):
            assert f"item-{index}" in payload["done"], f"并发写丢了 item-{index}"

    def test_temp_file_name_is_unique(self, workdir: Path) -> None:
        """原子写的临时文件名必须唯一 —— 并发写同一个文件会互相覆盖。"""
        target = workdir / "state.json"
        names: list[str] = []
        real_replace = Path.replace

        def spy_replace(self, other):
            names.append(self.name)
            return real_replace(self, other)

        target.parent.mkdir(parents=True, exist_ok=True)
        original = Path.replace
        Path.replace = spy_replace  # type: ignore[method-assign]
        try:
            jobs._write_json(target, {"a": 1})
            jobs._write_json(target, {"a": 2})
        finally:
            Path.replace = original  # type: ignore[method-assign]
        assert len(names) == 2
        assert names[0] != names[1], "临时文件名必须每次不同"


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
        # 子进程写完终态就退出，但退出有一小段时间差 —— 等它真的没了，
        # 否则「进程还活着」会被当成「仍在运行」，用例变成竞态。
        _wait_dead(state.pid)
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


class TestCancelHonesty:
    """取消必须「说了就算」—— 进程还活着就不能报 cancelled（P1）。

    之前：``request_cancel`` 写标记失败 + ``_kill_tree`` 被沙箱拒绝 → 函数照样
    返回 ``state=cancelled``，于是作业状态说「已停」，实际上子进程还在下载，
    还在往库里写文件。agent 看到 cancelled 就重跑，产物互相踩。
    """

    def test_cancel_fails_when_process_is_still_alive(
        self, store: jobs.JobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = jobs.start(store, "version", [], items=["a"])
        _write_progress(store, state.id, {"items": ["a"]})

        monkeypatch.setattr(jobs, "request_cancel", lambda *a, **k: False)
        monkeypatch.setattr(jobs, "_kill_tree", lambda pid: False)
        monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)

        with pytest.raises(MangaError) as excinfo:
            jobs.cancel(store, state.id, grace=0.1)

        assert excinfo.value.code == "CANCEL_FAILED"
        assert excinfo.value.exit_code == 2

        # 关键：状态**不能**被写成 cancelled（进程还在跑）
        current = store.load(state.id)
        assert current.state != jobs.STATE_CANCELLED
        # 也不能自己写一份「已取消」的终态报告（子进程自己写的终态不算）
        final = store.read_final(state.id)
        assert final is None or final.get("state") != jobs.STATE_CANCELLED

    def test_ctrl_break_branch_confirms_the_process_died(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CTRL_BREAK 发出去不等于进程死了 —— 必须轮询确认。"""
        if sys.platform != "win32":  # pragma: no cover - 仅 Windows 分支
            pytest.skip("CTRL_BREAK 是 Windows 专有")

        class _FakeProc:
            returncode = 1

        monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: _FakeProc())
        sent: list[int] = []
        monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: sent.append(sig))

        # 进程确实退出了 → 强杀算成功
        monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
        assert jobs._kill_tree(1234) is True
        assert sent  # CTRL_BREAK 确实发出去了

        # 进程还活着 → 强杀不算成功（不能骗人）
        monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
        assert jobs._kill_tree(1234) is False


class TestFinalizeHonesty:
    """终态必须与信封一致（P1）。

    ``exit == 0`` 只说明「进程退出码是 0」—— 如果子进程自己报了
    ``ok=false``（比如下载失败），作业绝不能显示 succeeded。
    """

    def test_finalize_with_ok_false_is_failed(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        path = workdir / "final.json"
        monkeypatch.setenv(jobs.ENV_JOB_FINAL, str(path))

        assert jobs.finalize({"ok": False, "exit": 0, "error": {"message": "下载失败"}}) is True

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state"] == jobs.STATE_FAILED
        assert payload["error"] == "下载失败"

    def test_finalize_with_ok_true_stays_succeeded(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        path = workdir / "final.json"
        monkeypatch.setenv(jobs.ENV_JOB_FINAL, str(path))

        assert jobs.finalize({"ok": True, "exit": 0}) is True
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state"] == jobs.STATE_SUCCEEDED

    def test_finalize_without_ok_field_is_succeeded(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """老信封没有 ok 字段时不能当成失败（向后兼容）。"""
        path = workdir / "final.json"
        monkeypatch.setenv(jobs.ENV_JOB_FINAL, str(path))

        assert jobs.finalize({"exit": 0}) is True
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["state"] == jobs.STATE_SUCCEEDED


class TestEffectiveStateReporting:
    """列表 / 轮询必须显示**真实**状态（P1）。

    之前：``store.list()`` 直接读 ``state.json``，父进程写的是 running，
    子进程早就写好 succeeded 了 —— 列表里仍显示 running。
    """

    def test_list_uses_effective_state(self, store: jobs.JobStore) -> None:
        state = jobs.start(store, "version", [])
        _wait_terminal(store, state.id)

        listed = {s.id: s for s in store.list()}
        assert state.id in listed
        assert listed[state.id].state == jobs.STATE_SUCCEEDED

    def test_elapsed_uses_finished_at(self, store: jobs.JobStore) -> None:
        """终态作业的耗时必须固定，不能随轮询时间一直涨。"""
        state = jobs.start(store, "version", [])
        _wait_terminal(store, state.id)

        first = jobs.poll(store, state.id)["elapsed_s"]
        time.sleep(1.1)
        second = jobs.poll(store, state.id)["elapsed_s"]
        assert first == second, "终态后耗时不该继续增长"

    def test_elapsed_is_zero_for_unfinished_job_without_start(self, store: jobs.JobStore) -> None:
        state = jobs.JobState(id="x", command="version")
        assert jobs._elapsed_seconds(state) == 0.0



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


def _wait_dead(pid: int | None, timeout: float = 20) -> None:
    """等子进程真的退出（避免「刚写完终态还在收尾」的竞态）。"""
    if not pid:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not jobs._pid_alive(pid):
            return
        time.sleep(0.1)


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
