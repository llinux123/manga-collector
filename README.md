# manga-collector · 漫画采集聚合层

> **Skill 是界面，薄 CLI 是手，agent 是大脑。**
> 把 jmcomic 文档提到的**全部能力**（16 个模块）汇聚成一个可被 Agent 调用的采集层，
> 产物落到 manga 工作区的权威库，一话一个 CBZ + 根目录 `ComicInfo.xml`。

| 文件 | 作用 |
|------|------|
| [docs/manga-aggregate-estimate.md](docs/manga-aggregate-estimate.md) | **评估**：形态选型（Agent Skill / CLI / Web）与工时、jmcomic 文档 25 页全量能力清单、14 条已核实的坑 |
| [docs/manga-skill-plan.md](docs/manga-skill-plan.md) | **实施计划**：P0–P4 分期、里程碑、风险登记册 |
| [docs/workspace-dependency.md](docs/workspace-dependency.md) | 与 manga 工作区的依赖契约（路径、环境变量、脚本约定） |
| [.agents/skills/manga/SKILL.md](.agents/skills/manga/SKILL.md) | **Skill 主体**：分派表、命令清单、输出契约、错误处置、红线、写操作确认 |
| [.agents/skills/manga/references/](.agents/skills/manga/references/) | 按需加载的知识：`pipeline` `troubleshooting` `jmcomic` `option-and-plugins` `naming` `sources` `servers` |
| [src/manga/](src/manga/) | **薄 CLI**：`paths` `doctor` `verify` `ingest` `convert-cbz` `guard` |
| [config/jmcomic-option.yml](config/jmcomic-option.yml) | jmcomic 本机配置模板（`dir_rule.rule` 已修正，见下） |
| [tests/](tests/) | pytest 用例（信封契约、退出码映射、红线守卫、dry-run 不落盘、DirRule 渲染回归） |

## 与 manga 工作区的关系

本项目只做**采集侧**；书库侧的流水线脚本属于 **manga 工作区**，本项目**引用而不复制**：

```
manga-collector（本仓库）                  manga 工作区（另一个仓库）
├── .agents/skills/manga/    ← Skill        ├── scripts/*.ps1          ← 4 个流水线脚本（被调用）
├── src/manga/               ← 薄 CLI        ├── jmcomic/Convert-JmComicToCbz.ps1
├── config/jmcomic-option.yml                ├── spec/naming-and-metadata.md  ← 命名与元数据规范
└── tests/                                   └── docs/decision-record.md
```

调用时按需解析 manga 工作区：环境变量 `MANGA_WORKSPACE` → 同级 `../manga` → `~/Downloads/dsh/manga`。
详见 [docs/workspace-dependency.md](docs/workspace-dependency.md)。

## 快速开始

```powershell
# 0) 装环境
uv venv; uv pip install -e ".[dev]"

# 1) 体检（PowerShell / 脚本 / jmcomic 导入 + 版本下限 / 登录态 / 可选依赖 / 代理端口 / 目录 / manga 工作区）
manga doctor

# 2) 看落点
manga paths

# 3) 入库三步（默认 dry-run；确认后再加 --apply）
manga convert-cbz            # 散图 → CBZ
manga ingest                 # 干跑入库
manga ingest --apply --generate-comicinfo --zero-pad
manga verify                 # 校验闸门：退出码 0 才算入库成功

# 4) 红线守卫：共享区新增文件 → 退出码 6
manga guard --snapshot .export-snapshot.json
manga guard --compare .export-snapshot.json
```

## 输出契约

stdout 永远只有一个 JSON 对象：

```json
{"ok": true, "data": {}, "error": null, "warnings": [], "exit": 0}
```

| exit | 含义 |
|---|---|
| 0 | 成功（**可能带 warnings**） |
| 1 | 参数 / 用法错 |
| 2 | 执行错误（与 PS 脚本退出码 2 对齐） |
| 3 | 需要登录 / 凭据 |
| 4 | 资源不存在 |
| 6 | **红线拒绝**（灰色来源不得进入 `Export\Manga`） |

PS 退出码 `1`（告警/冲突）映射为 CLI `0` + `warnings` 非空 —— **不是错误**。

## 红线

1. **灰色来源（禁漫等未授权成人漫画扫描件）的产物不得进入 `Export\Manga`** —— 那是 Syncthing 共享区，
   会同步到手机。CLI 不提供对灰色来源的导出能力；`manga guard` 检测到共享区新增/丢失文件即以退出码 6 拒绝。
2. 不替用户分发、不做批量分发。
3. 不把 `Manga\Library` 设为 Syncthing 共享目录（只共享 `Export\Manga`）。
4. 触发红线时说明越界对象与原因，然后**停下**，不换命令再试一次。

## 已修正的一处上游陷阱

`config/jmcomic-option.yml` 的 `dir_rule.rule` 原为 `Bd_Atitle/Pname` —— **分隔符与写法混用**，
会渲染出字面量目录 `Bd_Atitle`，导致所有章节被误归到同名系列下（静默污染库）。
已改为 `Bd/Atitle/Pname`，并由 `tests/test_dirrule.py` 钉死渲染结果。
详见 [references/naming.md](.agents/skills/manga/references/naming.md)。

## 开发

```powershell
uv pip install -e ".[dev]"
uv run pytest
uv run manga doctor
```

Skill 的发现路径由 DSH 决定：`<项目根>\.agents\skills`（本仓库）或 `<用户目录>\.agents\skills`。
本仓库自带 `.agents/skills/manga`，**尚未**安装到用户级；需要全局可用时：

```powershell
Copy-Item -Recurse .agents\skills\manga "$env:USERPROFILE\.agents\skills\manga"
```

## 版本管理

按**阶段**推进，**每个阶段至少一次提交**。完整约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | Skill 骨架 + 薄 CLI（paths/doctor/verify/ingest/convert-cbz/guard）+ 红线守卫 + 45 项测试 | ✅ 已提交 |
| **P1** | jmcomic 适配层：search / album / comments / download / export / domains / pick-domain | ✅ 已提交（含一轮修复） |
| **P2** | 参考表补齐与校准（21 个插件 kwargs / DirRule 变量 / 证据等级标注） | ✅ 已完成 |
| **P3** | gallery-dl / mangadex-downloader 适配器 + 长任务模型 + GitHub Actions 生成器 | ✅ 已完成 |
| **P4** | 加固（日志治理、依赖探测、优雅停机）、测试、装配与发布 | 🟡 进行中 |

### P2 当前进度

| 条目 | 状态 | 说明 |
|---|---|---|
| 21 个插件 kwargs 逐个对源码 | ✅ | 按 `inspect.signature` 逐个取真实形参，不再照文档抄；抓到 3 个文档与源码对不上的点（`zip` 依赖是动态的 / `long_img` 的 Pillow 是隐式依赖 / `jm_server` 的依赖源码没声明） |
| option 默认值校准 | ✅ | 取自 `JmOption.default_dict()`，补上「上游默认值」表；纠正 `threading.photo=24`（不是 CPU 核数）、`dir_rule.rule` 默认 `Bd_Pname`、`impl` 默认 `api` |
| DirRule 变量表 | ✅ | 源码可用 **50** 个（A 25 + P 25），旧表只有 29 个 —— 补齐漏掉的一半，并修掉历史拼写错误 `Aaidoname` → `Aidoname` |
| 证据等级标注 | ✅ | `jmcomic.md` 每节标【实测】/【源码】/【文档】，并明确 html 端、收藏夹、签到、异步这几块**本机未实测** |
| **人工复核** | ✅ | 7 篇 references 逐段对照源码/脚本/工作区，**改掉 12 处漂移**（见下表） |
| **防腐烂工具** | ✅ | `tools/jmcomic_facts.py`（`plugins`/`dirrule`/`check`）+ `tools/doc_claims.py`（声明核对）+ `tests/test_reference_facts.py`：上游升级后跑测试就当场报漂移 |

**人工复核改掉的 12 处漂移**

| # | 文件 | 问题 | 依据 |
|---|---|---|---|
| 1 | pipeline.md | 乱码「# ① 打���」 | 文件被截断过 |
| 2 | pipeline.md / naming.md | **`Chapter 01` 被列为「合规」** | 正则要求编号以数字开头 → 实为**违规** |
| 3 | pipeline.md | LanguageISO 推断口径不全（漏 `cn`→zh-CN、`jp`→ja；实际扫**所有**路径段） | `Invoke-LibraryIngest.ps1` 的 `LanguageMap` |
| 4 | naming.md | ComicInfo 表让人以为 Volume/Manga/PageCount/LanguageISO 都会被校验 | 校验器**只有 Number 是逐字段必填** |
| 5 | troubleshooting.md | **`Bd_Atitle/Pname` 坏写法残留**（第 3 处） | 混用分隔符 → 字面量目录 |
| 6 | troubleshooting.md | 「pytest 已配 `--basetemp`」 | 实际是 `conftest.py` 的 `workdir` fixture（避开 `\\?\` 前缀 + 0o700） |
| 7 | troubleshooting.md / jmcomic.md | 「主程序退出前会调 `wait_all_plugins_finish()`」 | 上游**只定义、零调用点** → 得自己调 |
| 8 | jmcomic.md | **`client.toggle_favorite_album()` 不存在** | 2.7.11 只有 `add_favorite_album` / `delete_favorite_album` |
| 9 | jmcomic.md / troubleshooting.md | 并发模型过时（「默认可达 320 线程」） | 2.7.11 是**每层独立临时池** → 24×20=**480** |
| 10 | sources.md | `mangadex-dl` 标成可用 | 本机**未安装**（P3 做适配器前要先装） |
| 11 | sources.md | 漏了 gallery-dl 在受限环境的 PyInstaller 报错 | 实测 `[PYI-xxxxx:ERROR] Failed to create parent directory structure` |
| 12 | servers.md | 版本号标着「实抓」但本轮没联网 | 诚实降级为「2026-11-05 快照，未复核」 |

```powershell
uv run python tools/jmcomic_facts.py plugins   # 21 个插件的源码事实表
uv run python tools/jmcomic_facts.py dirrule   # DirRule 可用变量 + 与文档的差异
uv run python tools/jmcomic_facts.py check     # 漂移检查（测试里也在跑）
uv run python tools/doc_claims.py check        # 声明核对：API/常量/异常/脚本/正则/CLI 命令
```

### P3 当前进度

| 条目 | 状态 | 说明 |
|---|---|---|
| **作业模型**（`manga job/*`） | ✅ | `start` 立刻返回 job id 并后台跑；`poll` 精简秒回；`cancel` 杀进程树并给**已完成 / 未完成**清单；`result` 拿子进程的信封。状态**由子进程自己写终态**（父进程启动完就退出，收不了尾）；`final.json` 与 `state.json` 分开以免互相覆盖 |
| 作业取消 | ✅ | **协作式**：`job cancel` 只写取消标记，**子进程自己**看到后退出 —— 受限环境（沙箱/容器/CI）里外部杀「别的进程创建的进程」会被拒。实测：真实下载作业取消后子进程自行退出，`exited_by_itself=true` |
| gallery-dl 适配层 | ✅ | `precheck()` 只模拟不落盘（`-s`），不支持的 URL 明确返回「不支持」；`download()` 递归找新增 CBZ，**0 产物报 NO_ARTIFACT**（防假成功）；识别 PyInstaller 的环境问题并与「URL 不支持」区分开（根因是 **TEMP 不可写**，不是 cwd） |
| mangadex-downloader 适配层 | ✅ | `--save-as cbz` + 逐包检查 ComicInfo.xml；未安装时 TOOL_MISSING（退出码 4） |
| 收藏夹 / 签到 | ✅ | 走 jmcomic 的 `favorite_folder_export` 插件；zip 密码**只从环境变量**读且不回显；收藏夹清单也是灰色来源 → 落点进共享区直接**红线拒绝（退出码 6）**；无登录态 → 退出码 3 |
| GitHub Actions 生成器 | ✅ | `download_dispatch.yml`（**恰好 10 个 inputs**）+ `export_favorites.yml`（cron + 手动）；生成后自检 YAML；**jmcomic 分支不产生任何 artifact**（红线内建）；secrets 清单 12 项，只有名字与说明 |
| 服务端扫描助手 | ✅ | Komga：探活 / 列库 / 触发扫描（需 ADMIN）；Kavita：**只给指引**，本项目不自动做；HTTP 方法硬限制为 GET/POST，结构上无法改元数据 |
| 验收 | ✅ | 一次 2 话的真实下载，agent 端模型调用 ≤ 10 次（SKILL.md §8 给了轮询规矩）；`job cancel` 真能停；生成的 YAML 通过自检（10 inputs 用 `yaml.safe_load` 读回验证）；不支持的 URL 明确返回「不支持」且不下载 |

```powershell
manga job start download 404005 390442 --apply   # 立刻返回 job_id
manga job poll <job_id>                            # 几秒就回
manga job result <job_id>                          # 终态摘要
manga job cancel <job_id>                          # 真能停
manga gallery-dl "<URL>"                           # 预检：支不支持
manga actions-gen                                  # 生成 workflow（dry-run）
```

### P4 当前进度

| 条目 | 状态 | 说明 |
|---|---|---|
| 日志治理 | ✅ | `download` / `export` 调 jmcomic 前切到 `<书库根>\Manga\Metadata\logs`；目录不可写时退到系统临时目录并给 warning，不再让命令崩掉 |
| 依赖探测 | ✅ | `doctor` 报 `img2pdf` / `pillow` / `pyzipper` / `py7zr` / `pikepdf` / `gallery-dl` / `mangadex-dl` 缺哪些 —— **只给安装命令，绝不自动装** |
| 版本下限校验 | ✅ | `doctor` 报 jmcomic 版本并与下限 `2.7.11` 比对（低版本会静默忽略 option 新字段） |
| 登录态检查 | ✅ | 口径与 jmcomic 一致：`cookies` 非空 **且** 用户标识非空（api 看 `user_id`、html 看 `username`） |
| 导出缺依赖的友好报错 | ✅ | 缺 `img2pdf` 等 → 退出码 **4**（缺东西，与缺外部工具 `TOOL_MISSING` 一致）+ `uv pip install …` 提示，不再抛原始异常 |
| 干跑降级 | ✅ | 取不到元数据时干跑降级为最小计划并给 `warnings`，不再整条命令失败 |
| stdout 纯净性 | ✅ | jmcomic 的日志原本打在 stdout（会毁掉 JSON 信封），已改道 stderr；命令体里的意外 `print` 也被兜底改道 |
| 增量校验 | ✅ | `manga verify --series <名>` 只校验指定系列 —— 存量库有历史问题时，全库闸门永远不是 0。选中的系列会复制进临时影子库根再验（原库零改动，验完即删）；支持嵌套系列（`18comic/繼母與繼姐`）与来源层自动摊平；`verify` 输出带 `series_available` |
| 入库路径显式化 | ✅ | `ingest` 显式传 `-BackupRoot` / `-LogPath`，不再依赖 CWD（默认 `./backup` 会写进仓库根） |
| 脚本输出透出 | ✅ | 脚本退出码 1 时把输出尾部放进 `warnings` —— 不再只丢一句「有冲突或告警」 |
| `option.wait_all_plugins_finish()` | ⏳ | 退出前优雅停机（自定义异步插件用） |
| `uv tool install` 打包 + 用户级 skill 安装 | ⏳ | `pyproject` 已有 console script，未做安装实测 |

提交信息格式：`<类型>(P<阶段号>): <祈使句>`，例如 `feat(P1): jmcomic 适配层 —— 搜索/详情/下载/导出`。
阶段分支 `p/<阶段号>-<短名>`，squash 合入 `main`，保证 `main` 历史里 **一个阶段 = 一个提交**。

```powershell
git switch -c p/1-jmcomic-adapter
# …开发…
uv run pytest && uv run manga doctor
git add -A && git commit -m "feat(P1): jmcomic 适配层 —— 搜索/详情/下载/导出"
```

## 边界

- 不做 GUI（Web 层如需，另行叠加在 core 之上，估算见评估文档）。
- 不装第三方扩展；不替用户索要密码或 Cookie。
- 不做 OCR / 翻译 / 超分（建议拆成独立 skill）。
- 站点内容为未授权扫描件：仅自用、不分发、不入共享区。
