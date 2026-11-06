"""报告解析与退出码映射的单元测试（不跑 PowerShell，快）。"""

from __future__ import annotations

from manga.adapters.pipeline import ScriptResult, _coerce, parse_verify_report

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
