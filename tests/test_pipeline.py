"""报告解析、退出码映射与 run_script 生命周期的单元测试。

解析器测试是纯字符串（快）；``TestRunScriptLifecycle`` 会起真实的 pwsh
（找不到 PowerShell 时整类跳过）。
"""

from __future__ import annotations

import inspect
import shutil
import threading
import time

import pytest

from manga.adapters import pipeline
from manga.adapters.pipeline import (
    DEFAULT_SCRIPT_TIMEOUT,
    REPORT_DETAIL_CAP,
    ScriptResult,
    _coerce,
    parse_cbz_report,
    parse_ingest_report,
    parse_verify_report,
    run_script,
)
from manga.envelope import MangaError

# 取自 scripts/README.md「输出示例」的真实格式
SAMPLE = """==================== 漫画库校验报告 ====================
库根目录   : D:\\Manga\\library
系列目录数 : 3
扫描文件数 : 11（CBZ 11，CBR/RAR 0）
命名合规率 : 90.9%（10/11）
ComicInfo 覆盖率: 90.0%（9/10 可读 CBZ）
CBZ 可读率: 90.9%（10/11）
编号一致性: 85.7%（6/7 文件名编号 == ComicInfo.Number）
页数达标率 : 100.0%（10/10，阈值 5 页）
空卷/残卷  : 1（零字节 / 无图片页 / 页数不足 / 损坏）
重复入库   : 1 组（Number+Volume 组合键）
分卷提示   : 1 组（同话号不同卷号，不算重复）
宽度不一致 : 1 个系列
同名文件   : 0 组（提示项）
问题统计   : 错误 6 个 / 告警 4 个

---- 问题列表（前 10 条，共 10 条）----
[Error  ] NumberMismatch Mismatch Series\\002 - X.cbz
          文件名编号 002 与 ComicInfo.Number=1 不一致（会导致排序错乱并被误判成重复）
[Warning] Split          Split Series\\01 - Part A.cbz
          同一话号 Number=1 对应多个卷（Volume: 1, 2），按一话分卷（split）处理，不是重复入库
"""


class TestParseVerifyReport:
    def test_summary_fields_are_extracted(self) -> None:
        report = parse_verify_report(SAMPLE)
        assert report["library_root"] == "D:\\Manga\\library"
        assert report["series_count"] == 3
        assert report["naming_compliance"] == {"percent": 90.9, "detail": "10/11"}
        assert report["comicinfo_coverage"]["percent"] == 90.0
        assert report["cbz_readable"]["percent"] == 90.9
        assert report["number_consistency"]["percent"] == 85.7
        assert report["page_count_ok"]["percent"] == 100.0

    def test_count_fields(self) -> None:
        report = parse_verify_report(SAMPLE)
        assert report["empty_or_broken"] == {"count": 1, "detail": "零字节 / 无图片页 / 页数不足 / 损坏"}
        assert report["duplicates"] == {"count": 1, "detail": "Number+Volume 组合键"}
        assert report["splits"]["count"] == 1
        assert report["duplicate_names"]["count"] == 0

    def test_issues_are_parsed_with_severity(self) -> None:
        report = parse_verify_report(SAMPLE)
        issues = report["issues"]
        assert len(issues) == 2
        assert issues[0]["severity"] == "error"
        assert issues[0]["category"] == "NumberMismatch"
        assert issues[1]["severity"] == "warning"
        assert issues[1]["category"] == "Split"

    def test_continuation_line_is_merged(self) -> None:
        report = parse_verify_report(SAMPLE)
        assert "不一致" in report["issues"][0]["message"]

    def test_empty_output(self) -> None:
        assert parse_verify_report("") == {"issues": []}


class TestCoerce:
    def test_percent_with_detail(self) -> None:
        assert _coerce("97.4%（754/774）") == {"percent": 97.4, "detail": "754/774"}

    def test_na_form(self) -> None:
        assert _coerce("n/a（0/0 文件名编号 == ComicInfo.Number）") == {
            "percent": None,
            "detail": "0/0 文件名编号 == ComicInfo.Number",
        }

    def test_count_with_detail(self) -> None:
        assert _coerce("18（零字节 / 损坏）") == {"count": 18, "detail": "零字节 / 损坏"}

    def test_count_with_unit(self) -> None:
        assert _coerce("0 组（Number+Volume 组合键）") == {"count": 0, "detail": "Number+Volume 组合键"}

    def test_plain_int(self) -> None:
        assert _coerce("3") == 3
        assert _coerce("1 个系列") == 1

    def test_plain_string(self) -> None:
        assert _coerce("错误 6 个 / 告警 4 个") == "错误 6 个 / 告警 4 个"


class TestScriptResultSemantics:
    """PS 退出码 0/1/2 → 语义。1 是告警，**不是错误**。"""

    def _result(self, code: int) -> ScriptResult:
        return ScriptResult(script="X.ps1", exit_code=code, stdout="", stderr="")

    def test_zero_is_ok(self) -> None:
        r = self._result(0)
        assert r.ok and not r.warned and not r.failed

    def test_one_is_warning_not_error(self) -> None:
        r = self._result(1)
        assert not r.ok and r.warned and not r.failed

    def test_two_is_error(self) -> None:
        r = self._result(2)
        assert not r.ok and not r.warned and r.failed

    @pytest.mark.parametrize("code", [3, 127, 255, -1])
    def test_unexpected_exit_codes_are_errors(self, code: int) -> None:
        """P1 回归：0/1/2 之外的退出码（异常终止/被杀/命令不存在）必须判失败。

        此前只把 2 当失败 → exit 3/127/-1 全被包装成 ok=true（假成功）。
        """
        r = self._result(code)
        assert r.failed
        assert not r.ok and not r.warned


class TestScriptEnvelopeMapping:
    """_script_result_to_envelope：异常退出码不得产出成功信封。"""

    def _env(self, code: int, stdout: str = "", stderr: str = ""):
        from manga.cli import _script_result_to_envelope

        return _script_result_to_envelope(ScriptResult(script="X.ps1", exit_code=code, stdout=stdout, stderr=stderr))

    @pytest.mark.parametrize("code", [3, 127, -1])
    def test_unexpected_codes_fail_envelope(self, code: int) -> None:
        env = self._env(code, stderr="boom")
        assert env.ok is False
        assert env.exit == 2
        assert env.error["code"] == "SCRIPT_ERROR"

    def test_exit_one_with_valid_report_stays_warning(self) -> None:
        env = self._env(1, stdout=SAMPLE)
        assert env.ok is True
        assert env.exit == 0
        assert env.warnings

    def test_exit_one_without_report_but_stderr_is_error(self) -> None:
        """PS 未处理异常常以 1 退出：没有任何已知汇总块 + stderr 非空 → 不是告警。"""
        env = self._env(1, stdout="", stderr="RuntimeException: boom")
        assert env.ok is False
        assert env.exit == 2
        assert env.error["code"] == "SCRIPT_ERROR"

    def test_exit_one_empty_output_stays_warning(self) -> None:
        """既无报告也无 stderr：维持既有告警语义（不臆断为异常）。"""
        env = self._env(1)
        assert env.ok is True
        assert env.warnings


# ── Convert-JmComicToCbz.ps1 输出解析 ─────────────────────────────────────
# 行格式取自工作区脚本源码（Convert-JmComicToCbz.ps1:249/240/281/291/180）

CBZ_SAMPLE = """【dry-run】未加 -Apply，只打印计划，不写任何文件。
[车号] 404005 → Some Album
[打包] Some Album\\001 - 第一话.cbz   （12 张图，源：第01話）
[打包] Some Album\\002.cbz   （8 张图，源：第02話）
[跳过] 已存在同名包，不覆盖： 003 - 重复.cbz
[错误] 004 - 残卷：副題.cbz：源文件读取失败
汇总：计划/成功 3，跳过 1，错误 1。目标目录：D:\\Library\\00-Incoming\\Manga
"""


class TestParseCbzReport:
    def test_counts_and_target(self) -> None:
        report = parse_cbz_report(CBZ_SAMPLE)
        assert report["planned_or_succeeded"] == 3
        assert report["skipped"] == 1
        assert report["errors"] == 1
        assert report["target_root"] == "D:\\Library\\00-Incoming\\Manga"

    def test_packed_entries(self) -> None:
        report = parse_cbz_report(CBZ_SAMPLE)
        assert report["packed"] == [
            {"series": "Some Album", "file": "001 - 第一话.cbz", "images": 12, "source": "第01話"},
            {"series": "Some Album", "file": "002.cbz", "images": 8, "source": "第02話"},
        ]

    def test_skip_and_error_lines(self) -> None:
        report = parse_cbz_report(CBZ_SAMPLE)
        assert report["skipped_files"] == ["003 - 重复.cbz"]
        # 文件名里含「：」不拆字段 —— 整行保留（拆错会把文件名截断）
        assert report["error_lines"] == ["004 - 残卷：副題.cbz：源文件读取失败"]

    def test_nothing_to_do_is_flagged(self) -> None:
        out = "源目录下没有找到图片目录（期望 <base>\\<本子>\\<章节>\\*.jpg 或 <base>\\<章节>\\*.jpg）。"
        report = parse_cbz_report(out)
        assert report["nothing_to_do"] is True
        assert report["parsed"] is True
        assert "planned_or_succeeded" not in report

    def test_unrecognized_output_marks_parsed_false(self) -> None:
        report = parse_cbz_report("随便什么旧版脚本的输出")
        assert report["parsed"] is False
        assert report["nothing_to_do"] is False

    def test_empty_output(self) -> None:
        assert parse_cbz_report("")["parsed"] is False

    def test_detail_is_capped_and_flagged(self) -> None:
        lines = [f"[打包] S\\{i:03d}.cbz   （{i} 张图，源：c{i}）" for i in range(1, 26)]
        out = "\n".join(lines) + "\n汇总：计划/成功 25，跳过 0，错误 0。目标目录：T\n"
        report = parse_cbz_report(out)
        assert len(report["packed"]) == REPORT_DETAIL_CAP
        assert report["truncated"] is True
        assert report["planned_or_succeeded"] == 25

    def test_small_report_is_not_truncated(self) -> None:
        assert parse_cbz_report(CBZ_SAMPLE)["truncated"] is False


# ── Invoke-LibraryIngest.ps1 输出解析 ─────────────────────────────────────

INGEST_DRYRUN_SAMPLE = """==================== 漫画入库整理 ====================
模式       : DRY-RUN（只打印计划，不落盘）
Inbox      : D:\\Library\\00-Incoming\\Manga
Library    : D:\\Library\\Manga\\Library

---- 处理计划（dry-run，不落盘）----
[  1] 入库 禁漫 (JMComic)/Some Album/001 - X.cbz
      系列   : Some Album
      目标   : D:\\Library\\Manga\\Library\\Some Album\\001 - X.cbz
      重命名 : 001 - X.cbz  ->  001 - X.cbz
[  2] 冲突 禁漫 (JMComic)/Some Album/002 - Y.cbz
      系列   : Some Album
      元数据 : 冲突跳过，不写入元数据
      备注   : 目标已存在同名文件，跳过（不覆盖）
[  3] 跳过 禁漫 (JMComic)/Other/broken.cbz
      原因: CBZ 无法打开（疑似损坏）: zip 头错误

---- 汇总 ----
扫描文件   : 3
待入库     : 1
冲突跳过   : 1
跳过(损坏) : 1
提示: 以上为 dry-run 计划，未做任何改动；加 -Apply 才会真正执行。
======================================================
"""

INGEST_APPLY_SUMMARY = """
---- 汇总 ----
扫描文件   : 2
待入库     : 2
冲突跳过   : 0
跳过(损坏) : 0
实际入库   : 2
写入元数据 : 1
错误       : 0
日志       : D:\\Library\\Manga\\Metadata\\logs\\ingest-1.log
"""


class TestParseIngestReport:
    def test_summary_counts(self) -> None:
        report = parse_ingest_report(INGEST_DRYRUN_SAMPLE)
        assert report["scanned"] == 3
        assert report["planned_moves"] == 1
        assert report["conflicts"] == 1
        assert report["skipped_corrupt"] == 1
        # dry-run 没有 apply-only 字段
        assert "moved" not in report
        assert "comicinfo_written" not in report
        assert "errors" not in report
        assert "log_path" not in report

    def test_plan_entries_with_notes(self) -> None:
        report = parse_ingest_report(INGEST_DRYRUN_SAMPLE)
        plans = report["plans"]
        assert [p["tag"] for p in plans] == ["入库", "冲突", "跳过"]
        assert plans[0]["source"] == "禁漫 (JMComic)/Some Album/001 - X.cbz"
        assert plans[1]["note"] == "目标已存在同名文件，跳过（不覆盖）"
        assert plans[2]["note"] == "CBZ 无法打开（疑似损坏）: zip 头错误"

    def test_apply_summary_fields(self) -> None:
        report = parse_ingest_report(INGEST_APPLY_SUMMARY)
        assert report["moved"] == 2
        assert report["comicinfo_written"] == 1
        assert report["errors"] == 0
        assert report["log_path"] == "D:\\Library\\Manga\\Metadata\\logs\\ingest-1.log"

    def test_nothing_to_do(self) -> None:
        report = parse_ingest_report("[提示] inbox 中没有找到可处理的 CBZ 文件。")
        assert report["nothing_to_do"] is True
        assert report["parsed"] is True

    def test_unrecognized_output(self) -> None:
        assert parse_ingest_report("gibberish")["parsed"] is False


# ── run_script 生命周期：超时与取消击杀 ───────────────────────────────────

def _pwsh_available() -> bool:
    return bool(shutil.which("pwsh") or shutil.which("powershell"))


@pytest.mark.skipif(not _pwsh_available(), reason="需要 pwsh / powershell")
class TestRunScriptLifecycle:
    """脚本子进程的句柄必须能被取消线程杀掉 —— 否则 pwsh 变孤儿（继续写包/删源图）。"""

    def test_default_timeout_is_registered(self) -> None:
        default = inspect.signature(run_script).parameters["timeout"].default
        assert default == DEFAULT_SCRIPT_TIMEOUT

    def test_timeout_kills_process_and_unregisters(self, workdir) -> None:
        # 用 conftest 的 workdir（工作区内普通路径）而不是 tmp_path：
        # 沙箱里 pytest 的 basetemp 在 %TEMP%，受限进程读不了（见 conftest 说明）。
        script = workdir / "sleep.ps1"
        script.write_text("Start-Sleep -Seconds 60\n", encoding="utf-8")
        with pytest.raises(MangaError) as excinfo:
            run_script(script, timeout=1)
        assert excinfo.value.code == "TIMEOUT"
        # 进程已被杀且已注销：活动表为空
        assert pipeline.kill_active() == 0

    def test_kill_active_kills_running_script(self, workdir) -> None:
        script = workdir / "sleep.ps1"
        script.write_text("Start-Sleep -Seconds 60\n", encoding="utf-8")
        results: list[ScriptResult] = []
        errors: list[Exception] = []

        def runner() -> None:
            try:
                results.append(run_script(script, timeout=60))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=runner)
        thread.start()
        killed = 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            killed = pipeline.kill_active()
            if killed:
                break
            time.sleep(0.1)
        assert killed == 1
        thread.join(timeout=30)
        assert not errors
        # 被终止的脚本进程退出码是终止码（Windows 终止码 1），不再是 0/1/2 语义
        assert results and results[0].exit_code not in (0, 2)
