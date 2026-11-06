"""DirRule 回归测试：锁死目录规则的真实渲染结果。

**为什么需要这个测试**：2026-11-06 实测发现 `jmcomic/option.yml` 里的
``rule: Bd_Atitle/Pname`` 会渲染出**字面量目录** ``Bd_Atitle``——
因为分隔符是 ``/``（按 ``/`` 切段），首段 ``Bd_Atitle`` 既不等于 ``Bd``
又不以 ``A``/``P`` 开头，于是落到 f-string 解析器被原样输出。

后果是 ``Convert-JmComicToCbz.ps1`` 会把所有章节归到名为 ``Bd_Atitle`` 的系列下 → 静默污染库。

这类错误**不会报错**，只会安静地产出错误的目录结构，所以必须用测试钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jmcomic import JmAlbumDetail, JmPhotoDetail
from jmcomic.jm_option import DirRule

REPO_ROOT = Path(__file__).resolve().parents[1]
OPTION_FILE = REPO_ROOT / "config" / "jmcomic-option.yml"

#: 规则写法 → 期望的完整渲染路径（相对 base_dir）
ALBUM_TITLE = "社团学姐 [汉化组]"
PHOTO_NAME = "第01話 上"
RULE_CASES = [
    ("Bd/Atitle/Pname", f"D:/base/{ALBUM_TITLE}/{PHOTO_NAME}"),
    ("Bd_Atitle_Pname", f"D:/base/{ALBUM_TITLE}/{PHOTO_NAME}"),
    ("Bd / {Atitle} / {Pname}", f"D:/base/{ALBUM_TITLE}/{PHOTO_NAME}"),
    ("Bd/Atitle/Pindextitle", f"D:/base/{ALBUM_TITLE}/第1話 {PHOTO_NAME}"),
]


def _entities() -> tuple[JmAlbumDetail, JmPhotoDetail]:
    album = JmAlbumDetail(
        album_id="248965",
        scramble_id="220980",
        name="社团学姐 [汉化组]",
        episode_list=[],
        page_count=42,
        pub_date="2023-01-15",
        update_date="2023-06-20",
        likes="1K",
        views="40K",
        comment_count=128,
        works=[],
        actors=[],
        authors=["BLVEFO9"],
        tags=[],
    )
    photo = JmPhotoDetail(
        photo_id="212214",
        name="第01話 上",
        series_id="248965",
        sort=1,
        page_arr=["00001.webp"],
        data_original_domain="cdn-msp.jmapiproxy1.cc",
        from_album=album,
    )
    return album, photo


class TestDirRuleRendering:
    @pytest.mark.parametrize(("rule", "expected"), RULE_CASES)
    def test_rule_renders_expected_levels(self, rule: str, expected: str) -> None:
        album, photo = _entities()
        path = DirRule(rule=rule, base_dir="D:/base").apply_rule_to_path(album, photo)
        assert path == expected

    def test_mixed_separator_creates_literal_directory(self) -> None:
        """回归靶点：`Bd_` 前缀 + `/` 分隔 = 字面量目录（这正是被修掉的 bug）。"""
        album, photo = _entities()
        path = DirRule(rule="Bd_Atitle/Pname", base_dir="D:/base").apply_rule_to_path(album, photo)
        assert "Bd_Atitle" in path  # 字面量目录名，不是本子标题
        assert ALBUM_TITLE not in path

    def test_separator_and_segment_style_must_agree(self) -> None:
        """`/` 分隔 → 首段不能带 `Bd_`；`_` 分隔 → 段内不能含 `/`。"""
        album, photo = _entities()
        # 「_」分隔的写法是对的
        assert DirRule(rule="Bd_Atitle_Pname", base_dir="D:/base").apply_rule_to_path(album, photo) == (
            f"D:/base/{ALBUM_TITLE}/{PHOTO_NAME}"
        )
        # 混用就退化成字面量目录
        assert "Bd_Atitle" in DirRule(rule="Bd_Atitle/Pname", base_dir="D:/base").apply_rule_to_path(album, photo)


class TestProjectOption:
    """本机 option.yml 的规则必须渲染成「两层：本子 / 章节」。"""

    @pytest.fixture()
    def rule(self) -> str:
        data = yaml.safe_load(OPTION_FILE.read_text(encoding="utf-8-sig"))
        return data["dir_rule"]["rule"]

    def test_option_rule_has_no_literal_bd_prefix(self, rule: str) -> None:
        assert "Bd_" not in rule, f"option.yml 的 dir_rule.rule 含字面量 Bd_ 前缀：{rule}"

    def test_option_rule_renders_two_levels(self, rule: str) -> None:
        album, photo = _entities()
        path = DirRule(rule=rule, base_dir="D:/base").apply_rule_to_path(album, photo)
        relative = path[len("D:/base/") :]
        assert relative.count("/") == 1, f"应为两层目录（本子/章节），实际：{relative}"
        assert not relative.startswith("Bd"), f"首段被当字面量目录名：{relative}"

    def test_option_base_dir_points_into_the_inbox(self) -> None:
        data = yaml.safe_load(OPTION_FILE.read_text(encoding="utf-8-sig"))
        base = data["dir_rule"]["base_dir"]
        assert base.replace("\\", "/").endswith("00-Incoming/_raw_jmcomic"), base
        assert "Export" not in base, "下载落点绝不能指向 Syncthing 共享区"
