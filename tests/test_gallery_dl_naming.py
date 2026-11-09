"""gallery-dl 「入库合规命名」配置 + 部分失败识别的测试。

覆盖三件之前**没有护栏**、实测才发现的事：

1. **默认 CBZ 名过不了入库闸门** —— gallery-dl 原生模板产出 ``v01 c001``，
   而命名规范要求 ``<话号> - <标题>.cbz``。本项目自带一份配置把模板改成合规形态，
   由 ``manga gallery-dl`` 默认套用。这份配置是**生成物**
   （``tools/gallery_dl_facts.py config``），与本机 gallery-dl 的关键词对得上才算数。
2. **配置必须覆盖全部「章节站」** —— 少一个站，那个站下载出来的名字就不合规。
   用 ``tools/gallery_dl_facts.py check`` 守住。
3. **退出码位 4（部分文件下载失败）不能报成功** —— 产物 CBZ 已经写出来了，
   只是少了几页；报 ok 会让缺页的 CBZ 进库，而入库闸门查不出来（页数 ≥ 5 就「达标」）。

⚠ 全部离线：不联网、不下载。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from manga.adapters import gallery_dl
from manga.config import default_gallery_dl_config
from manga.envelope import EXIT_ERROR, MangaError

REPO_ROOT = Path(__file__).resolve().parents[1]
FACTS_TOOL = REPO_ROOT / "tools" / "gallery_dl_facts.py"
CONFIG_PATH = REPO_ROOT / "src" / "manga" / "data" / "gallery-dl-option.json"

#: 入库闸门（``Test-MangaLibrary.ps1``）的命名正则
#:
#: ⚠ 上游用的是 .NET/PCRE 语法（``(?<number>…``），Python 的 ``re`` 不认
#: （那是 ``(?P<name>…)``）→ 这里用 Python 等价写法，语义一致。
NAMING_PATTERN = (
    r"^(?:(?:vol|volume|卷)\s*\.?\s*)?(?:第\s*)?"
    r"(?P<number>\d+(?:\.\d+)*(?:[-–—]\d+)?)\s*"
    r"(?:(?:[-–—]|:|：)\s*|\s+)?(?P<title>.+)?$"
)


# ── 配置本体 ──────────────────────────────────────────────────────────────


class TestBundledConfig:
    def test_config_file_exists_and_is_json(self) -> None:
        assert CONFIG_PATH.is_file(), f"缺少配置：{CONFIG_PATH}"
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        assert "extractor" in data
        assert data["extractor"], "配置里一个分类都没有"

    def test_default_config_resolves_to_bundled_file(self) -> None:
        """CLI 默认套用的就是这个文件（`default_gallery_dl_config`）。"""
        resolved = default_gallery_dl_config()
        assert resolved == CONFIG_PATH

    def test_every_entry_produces_a_compliant_name(self) -> None:
        """每个分类的目录模板渲染出来，必须落在命名闸门里。

        模板是 gallery-dl 的格式串；这里用**样本关键词**渲染，
        检查渲染结果符合 ``<话号> - <标题>.cbz``。
        """
        import re

        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        pattern = re.compile(NAMING_PATTERN, re.IGNORECASE)

        for category, cfg in sorted(data["extractor"].items()):
            chapter = cfg.get("chapter")
            assert chapter is not None, f"{category} 缺 chapter 子分类"
            directory = chapter["directory"]
            assert isinstance(directory, list) and directory, f"{category} 目录模板为空"

            # 末段是文件名（gallery-dl 把它当 CBZ 名）
            leaf = directory[-1]
            assert "{chapter" in leaf, f"{category} 文件名模板里没有话号：{leaf}"

            # 样本渲染：话号 1、标题 "Chapter Title"
            rendered = leaf.replace("{chapter:>03}", "001").replace("{title|manga}", "Chapter Title")
            assert pattern.match(rendered), f"{category} 渲染结果不合规：{rendered}"

    def test_directory_has_exactly_one_level_below_series(self) -> None:
        """目录必须是「系列/文件」两层 —— 再深一层入库会报「层级过深」。

        模板首段是系列名，末段是文件名，**不能**有中间层
        （gallery-dl 原生模板带 ``{category}`` 来源层 → 库内多一层）。
        """
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for category, cfg in sorted(data["extractor"].items()):
            directory = cfg["chapter"]["directory"]
            assert len(directory) == 2, f"{category} 目录层数不对：{directory}"


# ── 与本机 gallery-dl 的一致性 ────────────────────────────────────────────


def _gallery_dl_available() -> bool:
    try:
        import gallery_dl.extractor  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _gallery_dl_available(), reason="本机没装 gallery-dl")
class TestFactsTool:
    def test_check_passes_against_installed_gallery_dl(self) -> None:
        """配置与本机 gallery-dl 的关键词一致（上游升级后能当场报漂移）。"""
        proc = subprocess.run(
            [sys.executable, str(FACTS_TOOL), "check"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, f"配置漂移：\n{proc.stdout}\n{proc.stderr}"

    def test_sites_command_lists_chapter_sites(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(FACTS_TOOL), "sites"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        assert "mangadex" in proc.stdout


# ── 部分失败不能报成功 ────────────────────────────────────────────────────


class _FakePopen:
    """极简替身：只回退出码与输出。"""

    def __init__(self, recorder) -> None:  # noqa: ANN001
        self._recorder = recorder
        self.returncode = recorder.returncode

    def communicate(self, timeout=None):  # noqa: ANN001
        return self._recorder.stdout, self._recorder.stderr

    def poll(self):
        return self._recorder.returncode

    def kill(self) -> None:
        pass

    def wait(self, timeout=None):  # noqa: ANN001
        return self._recorder.returncode


class _Recorder:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.argv: list[str] = []

    def popen(self, argv, **kwargs):  # noqa: ANN001
        self.argv = [str(a) for a in argv]
        return _FakePopen(self)


def _install(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    monkeypatch.setattr(gallery_dl.shutil, "which", lambda cmd, *a, **k: r"C:\fake\gallery-dl.exe")
    monkeypatch.setattr(gallery_dl, "_POPEN", recorder.popen)
    monkeypatch.setattr(gallery_dl, "FALLBACK_PATHS", ())


def _make_cbz(path: Path) -> Path:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.jpg", b"x")
    return path


class TestPartialDownload:
    def test_partial_download_raises_instead_of_ok(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """退出码位 4（有产物但缺页）→ 必须报错，不能报 ok。

        实测：MangaDex 一次 37 页的下载，第 26 页 SSL 断连 →
        gallery-dl 退出码 4，CBZ 却已经写出来了（只有 36 页）。
        """
        dest = workdir / "inbox"
        recorder = _Recorder(
            stderr="[download][error] Failed to download Series_c001_026.png\n",
            returncode=4,
        )

        def _popen(argv, **kwargs):  # noqa: ANN001
            recorder.argv = [str(a) for a in argv]
            target = Path(argv[argv.index("-d") + 1])
            _make_cbz(target / "Series" / "001 - Chapter.cbz")
            return _FakePopen(recorder)

        recorder.popen = _popen  # type: ignore[method-assign]
        _install(monkeypatch, recorder)

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.download(["https://example.com/series/chapter-1"], dest)

        assert excinfo.value.code == "PARTIAL_DOWNLOAD"
        assert excinfo.value.exit_code == EXIT_ERROR
        assert "Series_c001_026.png" in (excinfo.value.hint or "")

    def test_auth_failure_reports_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """退出码位 16 → 认证失败（不是「下载失败」）。"""
        dest = workdir / "inbox"
        recorder = _Recorder(stderr="[pixiv][error] AuthenticationError\n", returncode=16)

        def _popen(argv, **kwargs):  # noqa: ANN001
            recorder.argv = [str(a) for a in argv]
            target = Path(argv[argv.index("-d") + 1])
            _make_cbz(target / "Series" / "001 - Chapter.cbz")
            return _FakePopen(recorder)

        recorder.popen = _popen  # type: ignore[method-assign]
        _install(monkeypatch, recorder)

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.download(["https://example.com/series/chapter-1"], dest)

        assert excinfo.value.code == "AUTH_FAILED"

    def test_clean_exit_zero_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """退出码 0 且有产物 → 正常成功（别把正常路径也拦了）。"""
        dest = workdir / "inbox"
        recorder = _Recorder(stdout="ok\n", returncode=0)

        def _popen(argv, **kwargs):  # noqa: ANN001
            recorder.argv = [str(a) for a in argv]
            target = Path(argv[argv.index("-d") + 1])
            _make_cbz(target / "Series" / "001 - Chapter.cbz")
            return _FakePopen(recorder)

        recorder.popen = _popen  # type: ignore[method-assign]
        _install(monkeypatch, recorder)

        result = gallery_dl.download(["https://example.com/series/chapter-1"], dest)

        assert result["count"] == 1
        assert result["exit_code"] == 0
