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
| [src/manga/](src/manga/) | **薄 CLI**（20 个命令）：`paths` `doctor` `verify` `ingest` `convert-cbz` `guard` `search` `album` `comments` `download` `export` `domains` `pick-domain` `job *` `favorites` `checkin` `gallery-dl` `mangadex-dl` `actions-gen` `server *` `version` |
| [config/jmcomic-option.yml](config/jmcomic-option.yml) | jmcomic 本机配置模板（`dir_rule.rule` 已修正，见下） |
| [tests/](tests/) | pytest 用例（信封契约、退出码映射、红线守卫、dry-run 不落盘、DirRule 渲染回归、作业模型、references 同步性守护、gallery-dl 合规命名与部分失败） |
| [tools/](tools/) | 防腐烂工具：`jmcomic_facts.py`（源码事实提取）、`doc_claims.py`（文档声明核对）、`gallery_dl_facts.py`（gallery-dl 站点关键词 + 合规命名配置） |

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

### 安装

```powershell
# A) 开发装（推荐：改代码立刻生效）
uv venv; uv pip install -e ".[dev]"

# B) 工具装（装成全局命令 `manga`）
uv tool install --from . manga-collector
#   受限环境（沙箱 / 无写权限的 APPDATA）用工作区内的目录：
#   $env:UV_TOOL_DIR = "$PWD\.uv-tools"; $env:UV_TOOL_BIN_DIR = "$PWD\.uv-bin"
#   uv tool install --from . manga-collector
```

装完验证：`manga --help` / `manga version` / `manga doctor`。

### 把 Skill 装到用户级（全局可用）

Skill 主体在 `.agents/skills/manga/`。要让它在**所有会话**里可用，做一个**目录联接**
到用户级 skill 目录（DSH 读 `~/.agents/skills/`，Claude Code 读 `~/.claude/skills/`）：

```powershell
New-Item -ItemType Junction -Path "$env:USERPROFILE\.agents\skills\manga" `
         -Target "$PWD\.agents\skills\manga"
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\manga" `
         -Target "$PWD\.agents\skills\manga"
```

联接的好处：改仓库里的 SKILL.md / references **立即生效**，不用反复拷贝。
（`Remove-Item <链接路径>` 即可解链，**不会**删掉目标目录。）

### 常用流程

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

**下载走作业模型**（别用一条同步命令死等）：

```powershell
manga job start download 404005 390442 --apply   # 立刻返回 job_id
manga job poll <job_id>                            # 几秒就回（间隔 ≥ 15s，别高频轮询）
manga job result <job_id>                          # 终态摘要
manga job cancel <job_id>                          # 真能停（协作式：写取消标记，子进程自己退出）
```

### 排错

| 症状 | 先看 |
|---|---|
| 命令报错 / 结果不对 | `manga doctor` → 再看 [references/troubleshooting.md](.agents/skills/manga/references/troubleshooting.md) |
| 退出码含义 | [references/pipeline.md](.agents/skills/manga/references/pipeline.md)（PS ↔ CLI 映射） |
| 校验器报了什么 | `manga verify --report-path 报告.csv`（完整清单；控制台只显示前 N 条） |
| 存量库全库校验不为 0 | 那是历史问题，不是你刚入库那本的错 → 用 `manga verify --series <名>` 做增量验收 |

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
| **P4** | 加固（日志治理、依赖探测、优雅停机）、测试、装配与发布 | ✅ 已完成 |

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
| gallery-dl 适配层 | ✅ | `precheck()` 只模拟不落盘（`-s`），不支持的 URL 明确返回「不支持」；`download()` 递归找新增 CBZ，**0 产物报 NO_ARTIFACT**（防假成功）；识别 PyInstaller 的环境问题并与「URL 不支持」区分开（根因是 **TEMP 不可写**，不是 cwd）；**退出码位 4（部分文件失败）报 PARTIAL_DOWNLOAD 而非成功** |
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

### gallery-dl 的「入库合规命名」（2026-11-09 实测补的坑）

端到端跑通（下载 → 入库 → 验收）时发现两个**实测**出来的缺口，都已修：

| 坑 | 现象 | 修法 |
|---|---|---|
| **原生命名过不了入库闸门** | gallery-dl 的原生目录模板产出 `v01 c001.cbz` → 命名不合规 + `ComicInfo.Number` 缺失 + 库内多一层来源目录 | 自带按站配置 `src/manga/data/gallery-dl-option.json`，`manga gallery-dl` **默认套用** → 产出 `<系列>/<话号> - <标题>.cbz`；`--config` 是**替换**默认 |
| **退出码位 4 被当成成功** | gallery-dl 退出码是**位掩码**；位 4 = 部分文件下载失败 —— 产物 CBZ **已写出**但少了几页（实测 37 页只下了 36） | 适配层识别为 `PARTIAL_DOWNLOAD`（退出码 2）而不是 ok —— 否则缺页的 CBZ 会进库，而入库闸门查不出来（页数 ≥ 5 就「达标」） |

配置是**生成物**（各站关键词不一样，配错会渲染成 `None`）：

```powershell
uv run python tools/gallery_dl_facts.py sites   # 各站可用关键词（Markdown 表）
uv run python tools/gallery_dl_facts.py config  # 重新生成配置
uv run python tools/gallery_dl_facts.py check   # 校验配置是否仍与本机 gallery-dl 一致（测试里也在跑）
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
| `option.wait_all_plugins_finish()` | ✅ | **上游只定义、从不自动调用**（全仓零调用点）→ 我们在命令退出前显式等异步插件收工；`runtime.finish_plugins()` 做了「老版本没这方法 / 插件炸了都不影响命令结果」的兜底 |
| `uv tool install` 打包 | ✅ | `uv tool install --from . manga-collector` 实测通过，装完 `manga --help` / `version` / `doctor` 都正常；受限环境用 `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` 指到工作区 |
| 用户级 Skill 安装 | ✅ | 文档写清**目录联接**做法（`~/.agents/skills/manga` ← 仓库 `.agents/skills/manga`）；改仓库即时生效，解链不删源 |
| 锁版本 + 升级流程 | ✅ | README 新增「版本锁定与升级流程」：升 jmcomic 必须走 `jmcomic_facts check` → `doc_claims check` → pytest → JM_LIVE 冒烟，否则 references 会悄悄过期 |
| 真机冒烟（JM_LIVE=1） | ✅ 测试已就绪 | `tests/test_live_smoke.py`：2 章下载 → CBZ → 入库 → `verify --series` 退出码 0 → 共享区零新增。默认跳过，`$env:JM_LIVE="1"` 才跑（需联网 + 代理） |

## 版本锁定与升级流程

### 锁版本

| 依赖 | 锁定方式 | 理由 |
|---|---|---|
| `jmcomic` | `jmcomic>=2.7.11`（下限硬校验在 `runtime.MIN_JMCOMIC_VERSION`） | 低于 2.7.11 的 option 新字段会被**静默忽略**（表现为「配了不生效」），`doctor` 会报出来 |
| `typer` / `rich` / `pyyaml` | `>=` 下限 + `uv.lock` 锁死具体版本 | 复现性；升级走下面的流程 |
| Python | `>=3.14` | 用了 3.12+ 的语法与 `tempfile` 行为差异 |

`uv.lock` 是**权威版本记录** —— 别人 clone 下来 `uv sync` 得到的版本和你一样。

### 升级 jmcomic 的流程（**别直接改版本号就跑**）

上游一升级，**references 里的事实就会过期**（插件 kwargs、DirRule 变量、默认值都可能变）。
所以升级必须走这条流水线：

```powershell
# 1) 改 pyproject 的 jmcomic 版本，重新锁
#    pyproject.toml: jmcomic>=2.7.12
uv lock

# 2) 装上新的，看它到底变了什么
uv sync
uv run python tools/jmcomic_facts.py plugins    # 21 个插件的真实形参/依赖
uv run python tools/jmcomic_facts.py dirrule    # DirRule 可用变量 + 与文档的差异
uv run python tools/jmcomic_facts.py check      # ← references 是否已过期

# 3) 跑声明核对（API / 常量 / 异常 / 脚本 / 正则 / CLI 命令）
uv run python tools/doc_claims.py check

# 4) 跑测试（221 项，含「references 与源码同步」的守护）
uv run pytest

# 5) 按 check 的报告更新 references（改完 check 要变绿）
#    并在 references 里更新「校对日期」

# 6) 真机冒烟（要联网 + 代理）
$env:JM_LIVE = "1"; uv run pytest tests/test_live_smoke.py -v
```

**第 2 步的 `check` 变红就说明文档过期了** —— 这两个工具就是为此存在的：
`jmcomic_facts.py` 管「源码里的事实」，`doc_claims.py` 管「文档里的声明」。

### 真机冒烟

```powershell
$env:JM_LIVE = "1"                                  # 不设就跳过
$env:JM_LIVE_PHOTOS = "404005 390442"               # 可选：换章节
uv run pytest tests/test_live_smoke.py -v
```

跑的是**全链路**：2 章下载 → CBZ → 入库 → `verify --series` 退出码 0 → 共享区零新增。
落点全在**工作区内的临时书库**，不碰真实书库。
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
