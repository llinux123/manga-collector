"""外部下载器适配层测试（P3）—— 全部离线，**不联网**。

不依赖本机是否装了 gallery-dl / mangadex-dl：
用 ``monkeypatch`` 把 ``shutil.which`` 与 ``subprocess.run`` 换成假的
（``subprocess.run`` 换掉后，适配层跑的是「假 gallery-dl」，
按 argv 造出真实的 CBZ 产物，用来验证产物确认逻辑）。

覆盖：

1. **工具缺失** → ``TOOL_MISSING``（退出码 4，hint 带安装命令）；
2. **预检判定不支持** → ``supported=False`` 且**没有调用下载命令**；
3. **下载成功** → 发现新增 CBZ（递归）；
4. **下载了但 0 产物** → ``NO_ARTIFACT``（假成功）；
5. **mangadex-dl 未安装** → ``TOOL_MISSING``（正常路径）；
6. **ComicInfo.xml 统计** → 有 / 没有两种都算得对。

⚠ 不用 pytest 的 ``tmp_path``：它在 Windows 上用 ``\\\\?\\`` 扩展路径前缀，
沙箱的文件过滤器不认这种写法。改用仓库现成的 ``workdir`` 夹具（见 ``conftest.py``）。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from manga.adapters import gallery_dl
from manga.adapters import mangadex_dl
from manga.envelope import EXIT_ERROR, EXIT_NOTFOUND, EXIT_REFUSED, MangaError, RedLineViolation

# ── 测试替身 ──────────────────────────────────────────────────────────────


@dataclass
class _Call:
    """一次被记录下来的外部命令调用。"""

    argv: list[str]
    cwd: str | None = None
    timeout: float | None = None
    env: dict[str, str] | None = None
    #: 调用发生时，子进程的 TEMP 目录是否**已经存在**（PyInstaller 要求它存在）
    temp_dir_exists: bool | None = None


class _FakePopen:
    """冒充 ``subprocess.Popen``：记录调用参数，``communicate`` 返回预置的 stdout/stderr。"""

    def __init__(self, recorder: _Recorder, argv, **kwargs) -> None:
        env = kwargs.get("env")
        temp_dir_exists: bool | None = None
        if env and "TEMP" in env:
            temp_dir_exists = Path(env["TEMP"]).is_dir()
        recorder.calls.append(
            _Call(
                argv=[str(a) for a in argv],
                cwd=kwargs.get("cwd"),
                timeout=kwargs.get("timeout"),
                env=dict(env) if env else None,
                temp_dir_exists=temp_dir_exists,
            )
        )
        if recorder.on_call is not None:
            recorder.on_call([str(a) for a in argv], kwargs)
        self._recorder = recorder
        self.returncode = recorder.returncode
        self._killed = False

    def communicate(self, timeout=None):  # noqa: ANN001
        return self._recorder.stdout, self._recorder.stderr

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self._killed = True

    def wait(self, timeout=None):  # noqa: ANN001
        return self.returncode


@dataclass
class _Recorder:
    """记录所有 ``_POPEN`` 调用，并按需替身执行。"""

    calls: list[_Call] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    on_call: object | None = None

    def popen(self, argv, **kwargs):  # noqa: ANN001 - 冒充 subprocess.Popen
        return _FakePopen(self, argv, **kwargs)

    @property
    def download_calls(self) -> list[_Call]:
        """所有「会落盘」的调用（带 ``--cbz`` 或 ``-d``）。"""
        return [c for c in self.calls if "--cbz" in c.argv or "-d" in c.argv]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    *,
    tools: dict[str, str] | None = None,
    temp_base: Path | None = None,
) -> None:
    """装上假外部命令：``which`` 返回假路径，``subprocess.run`` 返回假结果。

    ``temp_base``：把子进程的临时目录（TEMP）指到这个目录下 ——
    测试不依赖机器的系统临时目录（那里可能有别的进程留下的残骸）。
    """
    mapping = tools if tools is not None else {}
    monkeypatch.setattr(gallery_dl.shutil, "which", lambda cmd, *a, **k: mapping.get(cmd))
    monkeypatch.setattr(gallery_dl, "_POPEN", recorder.popen)
    # 兜底路径在测试机上可能真的存在（scoop）→ 关掉，保证行为可预期
    monkeypatch.setattr(gallery_dl, "FALLBACK_PATHS", ())

    if temp_base is not None:
        temp_base.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("TEMP", str(temp_base))
        monkeypatch.setenv("TMP", str(temp_base))
        # tempfile 会缓存 gettempdir() 的结果 → 缓存要一起清掉
        monkeypatch.setattr(tempfile, "tempdir", None)


def _make_cbz(path: Path, *, comicinfo: bool = False) -> Path:
    """造一个最小 CBZ（页图 + 可选 ComicInfo.xml）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("00001.jpg", b"\xff\xd8\xff\xd9")
        if comicinfo:
            zf.writestr("ComicInfo.xml", "<ComicInfo><Title>T</Title></ComicInfo>")
    return path


def _arg_value(argv: list[str], flag: str) -> str:
    """取 ``-d <path>`` / ``--path <path>`` 的值。"""
    index = argv.index(flag)
    return argv[index + 1]


# ── ① 工具缺失 ────────────────────────────────────────────────────────────


class TestToolMissing:
    def test_gallery_dl_not_installed_raises_tool_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gallery_dl.shutil, "which", lambda cmd, *a, **k: None)
        monkeypatch.setattr(gallery_dl, "FALLBACK_PATHS", ())

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.precheck("https://example.com/series/1")

        assert excinfo.value.code == "TOOL_MISSING"
        assert excinfo.value.exit_code == EXIT_NOTFOUND
        hint = excinfo.value.hint or ""
        assert "uv tool install gallery-dl" in hint
        assert "scoop install gallery-dl" in hint

    def test_download_also_reports_tool_missing(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        monkeypatch.setattr(gallery_dl.shutil, "which", lambda cmd, *a, **k: None)
        monkeypatch.setattr(gallery_dl, "FALLBACK_PATHS", ())

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.download(["https://example.com/series/1"], workdir / "inbox")

        assert excinfo.value.code == "TOOL_MISSING"
        assert excinfo.value.exit_code == EXIT_NOTFOUND

    def test_scoop_fallback_is_used_when_which_fails(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """``which`` 找不到时回退 scoop 布局（``~/scoop/apps/gallery-dl/current``）。"""
        fake = workdir / "gallery-dl.exe"
        fake.write_bytes(b"MZ")
        monkeypatch.setattr(gallery_dl.shutil, "which", lambda cmd, *a, **k: None)
        monkeypatch.setattr(gallery_dl, "FALLBACK_PATHS", (fake,))

        assert gallery_dl.find_executable() == fake


# ── ② 预检 ────────────────────────────────────────────────────────────────


class TestPrecheck:
    def test_unsupported_url_is_reported_and_nothing_is_downloaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _Recorder(
            stderr="gallery-dl: unsupported URL: https://example.com/nope\n",
            returncode=1,
        )
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        result = gallery_dl.precheck("https://example.com/nope")

        assert result["supported"] is False
        assert result["status"] == "unsupported"
        assert "unsupported URL" in result["reason"]

        # 预检路径里**绝不能**出现会落盘的命令
        assert len(recorder.calls) == 1
        argv = recorder.calls[0].argv
        assert "-s" in argv
        assert "--no-input" in argv
        assert "--cbz" not in argv
        assert "-d" not in argv
        assert recorder.download_calls == []

    def test_supported_url_reports_site_and_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _Recorder(
            stdout="https://example.com/series/chapter-1\n└─ Some Series/001 - Chapter.cbz\n",
        )
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        result = gallery_dl.precheck("https://example.com/series/chapter-1")

        assert result["supported"] is True
        assert result["status"] == "supported"
        assert result["site"] == "example.com"
        assert result["title"] == "Some Series"

    def test_precheck_runs_in_a_writable_directory(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """PyInstaller 打包版在不可写的位置会炸 → 子进程的 cwd/TEMP 必须可写。"""
        recorder = _Recorder(stdout="ok\n")
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        gallery_dl.precheck("https://example.com/x", cwd=workdir)

        call = recorder.calls[0]
        cwd = Path(call.cwd or "")
        # 调用发生时目录存在（跑完会被删掉，所以只能看调用瞬间的快照）
        assert call.temp_dir_exists is True
        assert cwd.parent.is_dir()
        assert os.access(cwd.parent, os.W_OK)

    def test_pyinstaller_stderr_is_environment_problem_not_download_failure(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        recorder = _Recorder(
            stderr="[PYI-4567:ERROR] Failed to create parent directory structure.\n",
            returncode=1,
        )
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.precheck("https://example.com/x", cwd=workdir)

        assert excinfo.value.code == "ENV_PYINSTALLER"
        assert excinfo.value.code != "DOWNLOAD_FAILED"
        assert excinfo.value.exit_code == EXIT_ERROR


# ── ③ / ④ 下载与产物确认 ──────────────────────────────────────────────────


class TestDownload:
    def test_download_finds_new_cbz_recursively(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        dest = workdir / "inbox"

        def on_call(argv: list[str], kwargs: dict) -> None:
            target = Path(_arg_value(argv, "-d"))
            # CBZ 落在子目录里（-d 只是 base dir）
            _make_cbz(target / "Some Series" / "001 - Chapter.cbz")

        recorder = _Recorder(on_call=on_call)
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        result = gallery_dl.download(["https://example.com/series/chapter-1"], dest)

        assert result["count"] == 1
        assert result["new_count"] == 1
        assert len(result["files"]) == 1
        assert result["files"][0].endswith("001 - Chapter.cbz")
        assert (dest / "Some Series" / "001 - Chapter.cbz").is_file()

        # 命令行形状：--cbz + -d + url
        argv = recorder.calls[0].argv
        assert "--cbz" in argv
        assert "-d" in argv
        assert argv[-1] == "https://example.com/series/chapter-1"

    def test_download_with_zero_artifacts_raises_no_artifact(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """退出码 0 但一个 CBZ 都没有 —— 假成功，必须报错。"""
        recorder = _Recorder(stdout="done\n")
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.download(["https://example.com/series/chapter-1"], workdir / "inbox")

        assert excinfo.value.code == "NO_ARTIFACT"
        assert excinfo.value.exit_code == EXIT_ERROR

    def test_failed_command_without_artifacts_reports_download_failed(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        recorder = _Recorder(stderr="gallery-dl: error: 404\n", returncode=1)
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.download(["https://example.com/x"], workdir / "inbox")

        assert excinfo.value.code == "DOWNLOAD_FAILED"
        assert excinfo.value.exit_code == EXIT_ERROR

    def test_pre_existing_cbz_is_not_counted_as_new(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """已有的 CBZ 没被改动 → 不算这次的产物（避免「假成功」）。"""
        dest = workdir / "inbox"
        _make_cbz(dest / "Old" / "001.cbz")

        recorder = _Recorder()
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.download(["https://example.com/x"], dest)

        assert excinfo.value.code == "NO_ARTIFACT"

    def test_redline_dest_is_refused(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """落点在共享区 → 红线拒绝（退出码 6），且**不执行**任何命令。"""
        recorder = _Recorder()
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        export = workdir / "Export" / "Manga"
        with pytest.raises(RedLineViolation) as excinfo:
            gallery_dl.download(
                ["https://example.com/x"],
                export,
                export_dir=export,
                library_dir=workdir / "Manga" / "Library",
            )

        assert excinfo.value.exit_code == EXIT_REFUSED
        assert recorder.calls == []


# ── 子进程临时目录（PyInstaller 的根因） ─────────────────────────────────


class TestChildTempDir:
    """PyInstaller（onefile）版 gallery-dl 的运行时解压位置由 **TEMP/TMP** 决定，
    与 cwd 无关；TEMP 还必须指向一个**已存在**的目录。"""

    def test_temp_is_pointed_at_an_existing_writable_dir(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        recorder = _Recorder(stdout="ok\n")
        _install(
            monkeypatch,
            recorder,
            tools={"gallery-dl": r"C:\fake\gallery-dl.exe"},
            temp_base=workdir / "tmp",
        )

        gallery_dl.precheck("https://example.com/x", cwd=workdir)

        call = recorder.calls[0]
        env = call.env or {}
        assert env.get("TEMP") == env.get("TMP") == env.get("TMPDIR")
        # 调用发生时 TEMP 目录必须已经存在（指向不存在的目录会报
        # "Could not create temporary directory!"）
        assert call.temp_dir_exists is True
        # cwd 与 TEMP 指向同一个目录：工具的相对路径产物不会落在落点/仓库里
        assert Path(call.cwd or "") == Path(env["TEMP"])
        assert Path(env["TEMP"]).name.startswith(gallery_dl.SCRATCH_DIRNAME)
        assert Path(env["TEMP"]).parent == workdir / "tmp"

    def test_temp_is_not_left_in_the_dest(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """TEMP 落点不许落在落点里（PyInstaller 的 _MEI 垃圾会留在那儿）。"""
        dest = workdir / "inbox"

        def on_call(argv: list[str], kwargs: dict) -> None:
            target = Path(_arg_value(argv, "-d"))
            _make_cbz(target / "S" / "001.cbz")

        recorder = _Recorder(on_call=on_call)
        _install(
            monkeypatch,
            recorder,
            tools={"gallery-dl": r"C:\fake\gallery-dl.exe"},
            temp_base=workdir / "tmp",
        )

        gallery_dl.download(["https://example.com/x"], dest)

        env = recorder.calls[0].env or {}
        assert dest not in Path(env["TEMP"]).parents
        assert not [p for p in dest.rglob("*") if p.name.startswith("_MEI")]

    def test_scratch_dir_is_cleaned_up_after_the_run(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        """跑完把临时目录删掉（PyInstaller 的 ``_MEIxxxxxx`` 不该留下）。"""
        dest = workdir / "inbox"

        def on_call(argv: list[str], kwargs: dict) -> None:
            target = Path(_arg_value(argv, "-d"))
            _make_cbz(target / "S" / "001.cbz")

        recorder = _Recorder(on_call=on_call)
        _install(
            monkeypatch,
            recorder,
            tools={"gallery-dl": r"C:\fake\gallery-dl.exe"},
            temp_base=workdir / "tmp",
        )

        gallery_dl.download(["https://example.com/x"], dest)

        env = recorder.calls[0].env or {}
        assert not Path(env["TEMP"]).exists()
        assert not list((workdir / "tmp").glob(f"{gallery_dl.SCRATCH_DIRNAME}*"))

    def test_mangadex_child_also_gets_a_writable_temp_dir(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        dest = workdir / "inbox"

        def on_call(argv: list[str], kwargs: dict) -> None:
            target = Path(_arg_value(argv, "--path"))
            _make_cbz(target / "S" / "001.cbz", comicinfo=True)

        recorder = _Recorder(on_call=on_call)
        _install(
            monkeypatch,
            recorder,
            tools={"mangadex-dl": r"C:\fake\mangadex-dl.exe"},
            temp_base=workdir / "tmp",
        )

        mangadex_dl.download("https://mangadex.org/title/x", dest)

        call = recorder.calls[0]
        assert call.temp_dir_exists is True
        assert Path(call.cwd or "") == Path((call.env or {})["TEMP"])
        assert not list((workdir / "tmp").glob(f"{gallery_dl.SCRATCH_DIRNAME}*"))

    def test_environment_problem_hint_points_at_the_temp_dir(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path
    ) -> None:
        recorder = _Recorder(
            stderr="[PYI-4567:ERROR] Failed to create parent directory structure.\n",
            returncode=1,
        )
        _install(monkeypatch, recorder, tools={"gallery-dl": r"C:\fake\gallery-dl.exe"})

        with pytest.raises(MangaError) as excinfo:
            gallery_dl.precheck("https://example.com/x", cwd=workdir)

        hint = excinfo.value.hint or ""
        assert "TEMP" in hint
        assert "临时目录" in hint
        # 措辞要说清「这是环境问题，不是下载失败」
        assert "环境问题" in hint


# ── ⑤ / ⑥ mangadex-dl ────────────────────────────────────────────────────


class TestMangaDex:
    def test_not_installed_raises_tool_missing(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """本机默认没装 mangadex-dl —— 这是**正常路径**，不是崩溃。"""
        monkeypatch.setattr(mangadex_dl.shutil, "which", lambda cmd, *a, **k: None)

        with pytest.raises(MangaError) as excinfo:
            mangadex_dl.download("https://mangadex.org/title/xxx", workdir / "inbox")

        assert excinfo.value.code == "TOOL_MISSING"
        assert excinfo.value.exit_code == EXIT_NOTFOUND
        assert "mangadex-dl" in (excinfo.value.hint or "")

    def test_comicinfo_counts_with_and_without(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        dest = workdir / "inbox"

        def on_call(argv: list[str], kwargs: dict) -> None:
            target = Path(_arg_value(argv, "--path"))
            _make_cbz(target / "With" / "001.cbz", comicinfo=True)
            _make_cbz(target / "Without" / "002.cbz", comicinfo=False)

        recorder = _Recorder(on_call=on_call)
        _install(monkeypatch, recorder, tools={"mangadex-dl": r"C:\fake\mangadex-dl.exe"})

        result = mangadex_dl.download("https://mangadex.org/title/xxx", dest)

        assert result["comicinfo"] == {"with": 1, "without": 1}
        assert result["count"] == 2

        # 命令行形状：--save-as cbz --path <dest> <url>
        argv = recorder.calls[0].argv
        assert "--save-as" in argv
        assert argv[argv.index("--save-as") + 1] == "cbz"
        assert argv[-1] == "https://mangadex.org/title/xxx"

    def test_language_option_is_passed(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        def on_call(argv: list[str], kwargs: dict) -> None:
            target = Path(_arg_value(argv, "--path"))
            _make_cbz(target / "S" / "001.cbz", comicinfo=True)

        recorder = _Recorder(on_call=on_call)
        _install(monkeypatch, recorder, tools={"mangadex-dl": r"C:\fake\mangadex-dl.exe"})

        result = mangadex_dl.download(
            "https://mangadex.org/title/xxx",
            workdir / "inbox",
            language="zh",
        )

        argv = recorder.calls[0].argv
        assert "--language" in argv
        assert argv[argv.index("--language") + 1] == "zh"
        assert result["language"] == "zh"

    def test_zero_artifacts_raises_no_artifact(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        recorder = _Recorder(stdout="done\n")
        _install(monkeypatch, recorder, tools={"mangadex-dl": r"C:\fake\mangadex-dl.exe"})

        with pytest.raises(MangaError) as excinfo:
            mangadex_dl.download("https://mangadex.org/title/xxx", workdir / "inbox")

        assert excinfo.value.code == "NO_ARTIFACT"
        assert excinfo.value.exit_code == EXIT_ERROR

    def test_redline_dest_is_refused(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        recorder = _Recorder()
        _install(monkeypatch, recorder, tools={"mangadex-dl": r"C:\fake\mangadex-dl.exe"})

        library = workdir / "Manga" / "Library"
        with pytest.raises(RedLineViolation) as excinfo:
            mangadex_dl.download(
                "https://mangadex.org/title/xxx",
                library,
                export_dir=workdir / "Export" / "Manga",
                library_dir=library,
            )

        assert excinfo.value.exit_code == EXIT_REFUSED
        assert recorder.calls == []

    def test_has_comicinfo_reads_only_the_zip_directory(self, workdir: Path) -> None:
        """只读目录表，不解压内容。"""
        good = _make_cbz(workdir / "good.cbz", comicinfo=True)
        bad = _make_cbz(workdir / "bad.cbz")
        broken = workdir / "broken.cbz"
        broken.write_bytes(b"not a zip")

        assert mangadex_dl.has_comicinfo(good) is True
        assert mangadex_dl.has_comicinfo(bad) is False
        assert mangadex_dl.has_comicinfo(broken) is False
