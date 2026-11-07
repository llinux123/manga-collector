"""GitHub Actions workflow 生成器（P3 · 自动化）。

**只生成文本，不写盘**：``generate_workflows()`` 返回 ``文件名 → YAML 文本``，
由调用方决定要不要落盘（``write_workflows()`` 默认也是 dry-run，``apply=True`` 才写）。

两个 workflow：

* ``download_dispatch.yml`` —— ``workflow_dispatch`` 手动触发，**恰好 10 个 inputs**
  （验收标准之一），结构是「装工具 → 跑 manga CLI → 上传产物」；
* ``export_favorites.yml`` —— ``schedule``（cron）+ ``workflow_dispatch`` 触发，
  导出收藏夹清单，产物走 artifact 上传。

⚠ **红线（ADR-004）**：灰色来源（jmcomic / 禁漫）的产物**绝不能**进入
① 共享区（``Export\\``，Syncthing 同步到手机）② 发布区（GitHub Release 等对外分发）。
生成的 workflow 里，jmcomic 分支**不产生任何 artifact / release**，
并且落点在进入下载前会被一道红线检查拦下（见步骤「红线检查」）。

**依赖**：只用标准库 + ``pyyaml``（已在 pyproject 里），不新增任何依赖。

**契约**（与本项目其他模块一致）：

* 成功返回 ``dict``（直接进 JSON 信封的 data 字段）；
* 失败抛 :class:`~manga.envelope.MangaError`（带 code 与退出码）；
* **绝不 print 到 stdout** —— stdout 只能有一个 JSON 信封。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import yaml

from .envelope import EXIT_ERROR, MangaError

#: ``download_dispatch.yml`` 的 10 个 inputs（**验收标准：恰好 10 个**）
REQUIRED_DISPATCH_INPUTS: tuple[str, ...] = (
    "source",
    "ids",
    "mode",
    "destination",
    "format",
    "language",
    "dry_run",
    "include_comicinfo",
    "min_pages",
    "timeout_minutes",
)

#: 生成的两个 workflow 的文件名
WORKFLOW_FILENAMES: tuple[str, ...] = ("download_dispatch.yml", "export_favorites.yml")

#: workflow 在仓库里的落点（相对仓库根）
WORKFLOW_SUBDIR = ".github/workflows"

#: 生成内容的文件头（提醒：不要手工改生成物）
_HEADER = """\
# ============================================================================
# 由 `manga actions-gen` 生成 —— 请勿手工编辑
# 改动请改 src/manga/actions_gen.py 后重新生成。
"""


# ── download_dispatch.yml ─────────────────────────────────────────────────


_DOWNLOAD_WORKFLOW = _HEADER + """#
# ⚠ 红线（ADR-004）：灰色来源（jmcomic / 禁漫）的产物**绝不能**进入
#    ① 共享区（Export/，Syncthing 同步到手机）
#    ② 发布区（GitHub Release 等对外分发）
#    → 本 workflow 的 jmcomic 分支**不产生任何 artifact / release**，
#      且落点会在下载前被「红线检查」拦下。
# ============================================================================
name: manga download (dispatch)

on:
  workflow_dispatch:
    inputs:
      source:
        description: "采集源：gallery-dl / mangadex / jmcomic（灰色来源，产物不上传）"
        required: true
        type: choice
        default: gallery-dl
        options:
          - gallery-dl
          - mangadex
          - jmcomic
      ids:
        description: "要下的 ID 列表（逗号分隔；gallery-dl / mangadex 也接受 URL）"
        required: true
        type: string
        default: ""
      mode:
        description: "album=整本下载 / photo=按章节下载"
        required: true
        type: choice
        default: album
        options:
          - album
          - photo
      destination:
        description: "落点目录（必须是收件箱；禁止指向 Export/ 共享区或 Manga/Library 权威库）"
        required: true
        type: string
        default: "00-Incoming/Manga"
      format:
        description: "产物格式"
        required: true
        type: choice
        default: cbz
        options:
          - cbz
          - zip
          - pdf
          - longimg
      language:
        description: "语言过滤（zh / ja / en；留空 = 不限）"
        required: false
        type: string
        default: ""
      dry_run:
        description: "只干跑不落盘（true = 不真正下载）"
        required: false
        type: boolean
        default: true
      include_comicinfo:
        description: "是否生成 ComicInfo.xml"
        required: false
        type: boolean
        default: true
      min_pages:
        description: "每话最少页数（低于此值判为残卷）"
        required: false
        type: number
        default: 5
      timeout_minutes:
        description: "单次运行超时（分钟）"
        required: false
        type: number
        default: 30

permissions:
  contents: read

concurrency:
  group: manga-download-${{ github.ref }}
  cancel-in-progress: false

jobs:
  download:
    name: "下载 ${{ inputs.source }} → ${{ inputs.destination }}"
    runs-on: ubuntu-latest
    timeout-minutes: ${{ inputs.timeout_minutes }}
    env:
      MANGA_LIBRARY_ROOT: "${{ secrets.MANGA_LIBRARY_ROOT }}"
      MANGA_OPTION_FILE: "${{ secrets.MANGA_OPTION_FILE }}"
      MANGA_SOURCE: "${{ inputs.source }}"
      MANGA_MODE: "${{ inputs.mode }}"
      MANGA_FORMAT: "${{ inputs.format }}"
      MANGA_LANGUAGE: "${{ inputs.language }}"
      MANGA_MIN_PAGES: "${{ inputs.min_pages }}"
      MANGA_DRY_RUN: "${{ inputs.dry_run }}"
      MANGA_COMICINFO: "${{ inputs.include_comicinfo }}"
    steps:
      - name: 检出仓库
        uses: actions/checkout@v4

      - name: 准备 Python 3.14
        uses: actions/setup-python@v5
        with:
          python-version: "3.14"

      - name: 安装 uv
        uses: astral-sh/setup-uv@v5

      - name: 安装本项目与依赖
        run: uv pip install --system -e .

      - name: 参数检查
        shell: bash
        run: |
          set -euo pipefail
          ids="${{ inputs.ids }}"
          if [ -z "${ids//[[:space:],]/}" ]; then
            echo "::error::ids 为空：至少给一个 ID"
            exit 1
          fi
          echo "源：${{ inputs.source }}｜模式：${{ inputs.mode }}｜干跑：${{ inputs.dry_run }}"

      - name: 准备落点
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p "${{ inputs.destination }}"
          echo "落点：${{ inputs.destination }}"

      - name: "🛡 红线检查：灰色来源不得指向共享区 / 发布区"
        if: inputs.source == 'jmcomic'
        shell: bash
        run: |
          set -euo pipefail
          dest="${{ inputs.destination }}"
          case "$dest" in
            *[Ee]xport*|*[Ss]hared*|*[Rr]elease*|*[Pp]ublish*)
              echo "::error::红线：jmcomic（灰色来源）的落点不得是共享区或发布区：$dest"
              exit 1
              ;;
          esac
          echo "红线检查通过：$dest 不是共享区 / 发布区"

      - name: 下载（jmcomic · 灰色来源 · 产物不外传）
        if: inputs.source == 'jmcomic'
        shell: bash
        env:
          JM_USERNAME: "${{ secrets.JM_USERNAME }}"
          JM_PASSWORD: "${{ secrets.JM_PASSWORD }}"
        run: |
          set -euo pipefail
          # ⚠ 灰色来源：产物只落在本次运行的临时目录，**本 workflow 不上传它**
          dest=".work/raw-jmcomic"
          mkdir -p "$dest"
          ids=$(echo "${{ inputs.ids }}" | tr ',' ' ')
          apply_flag=""
          if [ "${{ inputs.dry_run }}" != "true" ]; then apply_flag="--apply"; fi
          photo_flag=""
          if [ "${{ inputs.mode }}" = "photo" ]; then photo_flag="--photo"; fi
          manga download $ids --dest "$dest" $photo_flag $apply_flag \\
            --library-root "${MANGA_LIBRARY_ROOT:-$PWD/Library}"

      - name: 下载（gallery-dl）
        if: inputs.source == 'gallery-dl'
        shell: bash
        run: |
          set -euo pipefail
          ids=$(echo "${{ inputs.ids }}" | tr ',' ' ')
          apply_flag=""
          if [ "${{ inputs.dry_run }}" != "true" ]; then apply_flag="--apply"; fi
          # format 目前只有 cbz 真的实现了（gallery-dl --cbz）；其余值经环境变量 MANGA_FORMAT 预留
          manga gallery-dl $ids --dest "${{ inputs.destination }}" $apply_flag

      - name: 下载（mangadex）
        if: inputs.source == 'mangadex'
        shell: bash
        run: |
          set -euo pipefail
          ids=$(echo "${{ inputs.ids }}" | tr ',' ' ')
          apply_flag=""
          if [ "${{ inputs.dry_run }}" != "true" ]; then apply_flag="--apply"; fi
          lang_flag=""
          if [ -n "${{ inputs.language }}" ]; then lang_flag="--language ${{ inputs.language }}"; fi
          manga mangadex-dl $ids --dest "${{ inputs.destination }}" $apply_flag $lang_flag

      - name: 入库 + 校验（闸门）
        if: inputs.dry_run == false
        shell: bash
        run: |
          set -euo pipefail
          comicinfo_flag=""
          if [ "${{ inputs.include_comicinfo }}" = "true" ]; then comicinfo_flag="--generate-comicinfo"; fi
          manga ingest --apply $comicinfo_flag --library-root "${MANGA_LIBRARY_ROOT:-$PWD/Library}"
          manga verify --min-pages "${{ inputs.min_pages }}" --library-root "${MANGA_LIBRARY_ROOT:-$PWD/Library}"

      - name: 上传产物（非灰色来源）
        if: inputs.source != 'jmcomic'
        uses: actions/upload-artifact@v4
        with:
          name: "manga-${{ inputs.source }}-${{ github.run_id }}"
          path: "${{ inputs.destination }}"
          if-no-files-found: error
          retention-days: 7

      - name: 灰色来源产物不外传（显式说明）
        if: inputs.source == 'jmcomic'
        shell: bash
        run: |
          echo "::notice::jmcomic 属于灰色来源：产物只留在本次运行的临时目录，"
          echo "::notice::不产生 artifact、不发布到 release，也不得同步到共享区 Export/。"
"""


# ── export_favorites.yml ──────────────────────────────────────────────────


_EXPORT_FAVORITES_WORKFLOW = _HEADER + """#
# 每天定时导出收藏夹清单（CSV + 可选密码 zip），产物以 artifact 形式留存。
#
# ⚠ 红线：收藏夹里可能混有灰色来源（jmcomic）的条目 →
#    artifact 是**仓库私有产物**，不得发布到 release、不得同步到共享区 Export/。
# ============================================================================
name: manga export favorites

on:
  schedule:
    # 每天 03:17 —— 避开整点（整点提交会被 GitHub 排队丢弃）
    - cron: "17 3 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  export:
    name: 导出收藏夹清单
    runs-on: ubuntu-latest
    timeout-minutes: 30
    env:
      MANGA_LIBRARY_ROOT: "${{ secrets.MANGA_LIBRARY_ROOT }}"
      MANGA_OPTION_FILE: "${{ secrets.MANGA_OPTION_FILE }}"
      JM_USERNAME: "${{ secrets.JM_USERNAME }}"
      JM_PASSWORD: "${{ secrets.JM_PASSWORD }}"
    steps:
      - name: 检出仓库
        uses: actions/checkout@v4

      - name: 准备 Python 3.14
        uses: actions/setup-python@v5
        with:
          python-version: "3.14"

      - name: 安装 uv
        uses: astral-sh/setup-uv@v5

      - name: 安装本项目与依赖
        run: uv pip install --system -e .

      - name: 导出收藏夹
        shell: bash
        env:
          MANGA_ZIP_PASSWORD: "${{ secrets.MANGA_ZIP_PASSWORD }}"
        run: |
          set -euo pipefail
          mkdir -p "$PWD/.work/favorites"
          # 收藏夹是**灰色来源的书单** → 落点绝不能是共享区（CLI 自己也会拦）
          if [ -n "${MANGA_ZIP_PASSWORD:-}" ]; then
            manga favorites --dest "$PWD/.work/favorites" --zip
          else
            manga favorites --dest "$PWD/.work/favorites"
          fi

      - name: 上传清单（artifact · 私有产物）
        uses: actions/upload-artifact@v4
        with:
          name: "manga-favorites-${{ github.run_id }}"
          path: ".work/favorites"
          if-no-files-found: warn
          retention-days: 14

      - name: 红线提示
        shell: bash
        run: |
          echo "::notice::收藏夹清单可能含灰色来源条目：只作为 artifact 私有留存，"
          echo "::notice::不发布到 release、不进共享区 Export/。"
"""


# ── secrets 清单 ──────────────────────────────────────────────────────────


def secrets_manifest() -> list[dict[str, Any]]:
    """workflow 需要的 secrets 清单：**只有名字与说明，不含任何真实凭据**。

    每项::

        {"name": "JM_USERNAME", "description": "禁漫账号（留空则多数内容仍可下）",
         "required": False, "used_by": ["download_dispatch.yml"]}

    ``required=True`` 表示「不配就跑不起来」；``False`` 表示「按来源/场景才需要」。
    """
    return [
        {
            "name": "MANGA_LIBRARY_ROOT",
            "description": "书库根目录（CI 里通常是工作区内的临时目录；不要指向共享区 Export/）",
            "required": True,
            "used_by": ["download_dispatch.yml", "export_favorites.yml"],
        },
        {
            "name": "MANGA_OPTION_FILE",
            "description": "jmcomic option 文件路径（该文件里放 auth 等配置，不要把凭据写进 workflow）",
            "required": False,
            "used_by": ["download_dispatch.yml", "export_favorites.yml"],
        },
        {
            "name": "JM_USERNAME",
            "description": "禁漫账号（留空则多数内容仍可下载；收藏夹 / 签到 / 敏感题材才需要）",
            "required": False,
            "used_by": ["download_dispatch.yml", "export_favorites.yml"],
        },
        {
            "name": "JM_PASSWORD",
            "description": "禁漫账号密码（与 JM_USERNAME 配套；只在 workflow 里引用 secret 本身，不要写进 YAML）",
            "required": False,
            "used_by": ["download_dispatch.yml", "export_favorites.yml"],
        },
        {
            "name": "GALLERY_DL_CONFIG",
            "description": "可选：gallery-dl 的配置文件路径（站点凭据放文件里，别放 workflow 里）",
            "required": False,
            "used_by": ["download_dispatch.yml"],
        },
        {
            "name": "MANGA_ZIP_PASSWORD",
            "description": "可选：收藏夹导出 zip 的密码（只经环境变量传给 CLI，别写进命令行）",
            "required": False,
            "used_by": ["export_favorites.yml"],
        },
        {
            "name": "KOMGA_BASE_URL",
            "description": "Komga 服务地址（例：http://127.0.0.1:25600），用于扫描助手",
            "required": False,
            "used_by": [],
        },
        {
            "name": "KOMGA_API_KEY",
            "description": "Komga API Key（后台 → Settings → API Key 生成；触发扫描需 ADMIN 权限）",
            "required": False,
            "used_by": [],
        },
        {
            "name": "KOMGA_USERNAME",
            "description": "Komga 账号（未用 API Key 时走 Basic 认证）",
            "required": False,
            "used_by": [],
        },
        {
            "name": "KOMGA_PASSWORD",
            "description": "Komga 账号密码（与 KOMGA_USERNAME 配套）",
            "required": False,
            "used_by": [],
        },
        {
            "name": "KAVITA_BASE_URL",
            "description": "Kavita 服务地址（例：http://127.0.0.1:5000）；Kavita 只做提示，不自动扫描",
            "required": False,
            "used_by": [],
        },
        {
            "name": "KAVITA_API_KEY",
            "description": "Kavita API Key（可选，仅用于只读探测是否可达）",
            "required": False,
            "used_by": [],
        },
    ]


# ── 生成 ──────────────────────────────────────────────────────────────────


def generate_workflows() -> dict[str, str]:
    """生成两个 workflow，返回 ``{"文件名": "YAML 文本"}``。

    **不写盘**：落盘由调用方决定（见 :func:`write_workflows`）。
    生成后会自检 YAML 能否被 ``yaml.safe_load`` 解析 —— 不能则抛
    :class:`~manga.envelope.MangaError`（宁可报错，也不把坏 YAML 交出去）。
    """
    files: dict[str, str] = {
        "download_dispatch.yml": _DOWNLOAD_WORKFLOW,
        "export_favorites.yml": _EXPORT_FAVORITES_WORKFLOW,
    }
    for name, text in files.items():
        report = validate_workflow(text)
        if not report["ok"]:
            raise MangaError(
                f"生成的 {name} 自检不通过：{'；'.join(report['errors'])}",
                code="WORKFLOW_INVALID",
                exit_code=EXIT_ERROR,
                hint="修 src/manga/actions_gen.py 里的模板；本模块不会产出坏 YAML",
            )
    return files


def write_workflows(destination: str | Path, *, apply: bool = False) -> dict[str, Any]:
    """把两个 workflow 写进 ``<destination>/.github/workflows/``。

    **默认 dry-run**：只报告将要写哪些文件；``apply=True`` 才落盘
    （与本项目「写操作默认 dry-run」的约定一致）。

    返回::

        {"workflow_dir": "...", "planned": [{"name", "path", "bytes"}],
         "written": [...], "dry_run": bool}
    """
    root = Path(destination).expanduser()
    target_dir = root / WORKFLOW_SUBDIR
    files = generate_workflows()

    planned = [
        {
            "name": name,
            "path": str(target_dir / name),
            "bytes": len(text.encode("utf-8")),
        }
        for name, text in files.items()
    ]

    written: list[str] = []
    if apply:
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            path = target_dir / name
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(str(path))

    return {
        "workflow_dir": str(target_dir),
        "planned": planned,
        "written": written,
        "dry_run": not apply,
        "note": "把这些文件提交到仓库后，Actions 才会出现在 CI 列表里",
    }


# ── 校验 ──────────────────────────────────────────────────────────────────


def _trigger_section(doc: dict[str, Any]) -> dict[str, Any] | None:
    """取出 ``on:`` 段。

    ⚠ YAML 1.1（PyYAML 的 ``safe_load`` 用的就是它）把裸键 ``on`` 解析成布尔
    ``True`` —— ``yaml.safe_load("on:\\n  workflow_dispatch:")`` 的键是 ``True``，
    不是 ``"on"``。两种都得认。
    """
    for key in ("on", True):
        if key in doc:
            value = doc[key]
            if isinstance(value, dict):
                return value
            return None
    return None


def validate_workflow(
    yaml_text: str,
    *,
    expected_inputs: Sequence[str] | None = None,
    require_inputs: bool = False,
) -> dict[str, Any]:
    """校验一份 workflow YAML。

    检查项：

    1. YAML 能被 ``yaml.safe_load`` 解析；
    2. 顶层是映射；
    3. 有 ``on:`` 触发器，且**有** ``workflow_dispatch``；
    4. 若声明了 ``workflow_dispatch.inputs``，则**恰好**是那 10 个
       （``expected_inputs`` 可覆盖；``require_inputs=True`` 时强制必须声明）；
    5. 每个 input 都有 ``description`` / ``required`` / ``default`` / ``type``；
    6. ``jobs`` 存在，每个 job 有 ``runs-on`` 与非空 ``steps``，每个 step 有 ``uses`` 或 ``run``。

    返回::

        {"ok": bool, "errors": [...], "warnings": [...], "inputs": [...],
         "input_count": int, "triggers": [...], "name": str | None}

    **不抛异常**：坏 YAML 也返回 ``ok=False`` + 错误清单（校验器的职责是报告，不是炸）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    result: dict[str, Any] = {
        "ok": False,
        "errors": errors,
        "warnings": warnings,
        "inputs": [],
        "input_count": 0,
        "triggers": [],
        "name": None,
    }

    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        errors.append(f"YAML 解析失败：{exc}")
        return result

    if not isinstance(doc, dict):
        errors.append("顶层不是映射（workflow 必须是一个字典）")
        return result

    result["name"] = doc.get("name") if isinstance(doc.get("name"), str) else None

    triggers = _trigger_section(doc)
    if triggers is None:
        errors.append("缺少 on: 触发器")
        triggers = {}

    result["triggers"] = sorted(str(k) for k in triggers.keys())
    if "workflow_dispatch" not in triggers:
        errors.append("缺少 on.workflow_dispatch（手动触发）")

    dispatch = triggers.get("workflow_dispatch")
    inputs: dict[str, Any] = {}
    if isinstance(dispatch, dict) and isinstance(dispatch.get("inputs"), dict):
        inputs = dict(dispatch["inputs"])
    elif require_inputs:
        errors.append("on.workflow_dispatch 下缺少 inputs")

    required = tuple(expected_inputs) if expected_inputs is not None else REQUIRED_DISPATCH_INPUTS
    result["inputs"] = list(inputs.keys())
    result["input_count"] = len(inputs)

    if inputs:
        missing = [name for name in required if name not in inputs]
        extra = [name for name in inputs if name not in required]
        if missing:
            errors.append(f"缺少 inputs：{'、'.join(missing)}")
        if extra:
            errors.append(f"多余的 inputs：{'、'.join(extra)}")
        if len(inputs) != len(required):
            errors.append(f"inputs 数量应为 {len(required)}，实际 {len(inputs)}")
        for name, spec in inputs.items():
            if not isinstance(spec, dict):
                errors.append(f"input {name} 的定义不是映射")
                continue
            for key in ("description", "required", "default", "type"):
                if key not in spec:
                    errors.append(f"input {name} 缺少 {key}")
            if "type" in spec and spec["type"] not in ("boolean", "choice", "number", "string", "environment"):
                errors.append(f"input {name} 的 type 非法：{spec['type']}")
            if spec.get("type") == "choice" and not spec.get("options"):
                warnings.append(f"input {name} 是 choice 但没有 options")
    elif not require_inputs:
        warnings.append("该 workflow 没有 workflow_dispatch.inputs（不检查 10 个 inputs）")

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        errors.append("缺少 jobs")
    else:
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                errors.append(f"job {job_name} 不是映射")
                continue
            if "runs-on" not in job:
                errors.append(f"job {job_name} 缺少 runs-on")
            steps = job.get("steps")
            if not isinstance(steps, list) or not steps:
                errors.append(f"job {job_name} 缺少 steps")
                continue
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    errors.append(f"job {job_name} 的第 {index} 个 step 不是映射")
                    continue
                if "uses" not in step and "run" not in step:
                    errors.append(f"job {job_name} 的第 {index} 个 step 既没有 uses 也没有 run")

    result["ok"] = not errors
    return result


def validate_workflows() -> dict[str, Any]:
    """校验生成的两个 workflow，返回逐份报告 + 汇总。

    ``download_dispatch.yml`` 会被额外要求「恰好 10 个 inputs」。
    """
    files = generate_workflows()
    reports: dict[str, Any] = {}
    for name, text in files.items():
        require = name == "download_dispatch.yml"
        reports[name] = validate_workflow(text, require_inputs=require)
    ok = all(report["ok"] for report in reports.values())
    return {"ok": ok, "reports": reports, "required_inputs": list(REQUIRED_DISPATCH_INPUTS)}


__all__ = [
    "REQUIRED_DISPATCH_INPUTS",
    "WORKFLOW_FILENAMES",
    "WORKFLOW_SUBDIR",
    "generate_workflows",
    "secrets_manifest",
    "validate_workflow",
    "validate_workflows",
    "write_workflows",
]
