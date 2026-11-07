# 采集聚合层 · 分期实施计划（Agent Skill 形态）

> 版本：v1.0 ｜ 制定日期：2026-11-06
> 依据：[manga-aggregate-estimate.md](manga-aggregate-estimate.md)（形态选型与工时评估）
> 配套：[decision-record.md](decision-record.md)（ADR-001~012）｜[manga-reading-toolchain-plan.md](manga-reading-toolchain-plan.md)（P0–P9 主链）

---

## 0. 目标、范围与非目标

### 0.1 目标

把**采集侧**做成一个「**Skill 定规则、薄 CLI 做执行、agent 做编排**」的聚合层，
覆盖 jmcomic 文档全量能力（16 模块），并把 gallery-dl / mangadex-downloader 一并纳入同一条流水线，
最终产出仍是本项目认可的「一话一 CBZ + 根目录 `ComicInfo.xml`」。

### 0.2 范围

- jmcomic 全量能力（检索 / 详情 / 评论 / 下载 / 过滤 / 导出 Feature / DirRule / 插件 / 登录 / 收藏夹 / 签到 / 域名 / 进度 / 异步 / CLI 等价物）
- 既有 4 个 PowerShell 脚本的编排与**红线闸门**
- gallery-dl、mangadex-downloader 接入；FMD2 / Kapowarr 等 GUI/服务端源写成「人工补位」的选路说明
- GitHub Actions workflow 生成、域名可达性探测
- 与 Komga / Kavita 的「手动扫描」触发（不自动挂载）

### 0.3 非目标

- 不做 GUI（形态 B 的 Web 应用；如需，增量 +31 人日叠在 core 之上）
- 不把 Suwayomi / Hakuneko 纳入自动化（ADR-011）
- 不做 OCR / 翻译 / 超分的编排（拆成独立 skill，本层只路由）
- 不自动执行 `Export-MangaLibrary.ps1` 对灰色源（红线，见 §2 P0-4）
- 不做任何需要「PC 常开」的调度（ADR-008）

---

## 1. 架构定案

```
用户自然语言
   ↓
[agent]  ← DSH 的 skill 机制，读 .agents/skills/manga/SKILL.md + references/*.md
   ↓  只做：选路 / 选参数 / 解析结果 / 确认 / 汇报
[manga CLI]  src/manga/  ← 薄，确定性，JSON envelope，出错即非零退出
   ├─ adapters/jmcomic.py       → jmcomic 2.7.11 (Python API)
   ├─ adapters/gallery_dl.py    → gallery-dl CLI
   ├─ adapters/mangadex_dl.py   → mangadex-downloader CLI
   ├─ adapters/pipeline.py      → scripts/*.ps1（含红线闸门）
   ├─ jobs.py                   → 长任务作业模型（start/poll/cancel/result）
   ├─ domains.py / actions.py / servers.py
   └─ envelope.py / config.py / cli.py
   ↓
权威库 Manga\Library  →  干净导出 Export\Manga（仅白/绿来源）  →  Syncthing  →  手机
```

**职责边界（三句话）**

1. **Skill 是界面**：`SKILL.md` 写分派规则、红线、确认流程、错误处置表；`references/` 放按需加载的知识。
2. **CLI 是手**：所有确定性动作在 CLI 里做，红线在代码里强制，不靠提示词约束。
3. **agent 是大脑**：只做决策与汇报，**不逐张图驱动下载**（109 话的批量必须交给 CLI 作业层）。

---

## 2. 分阶段实施

### P0 · 骨架 · CLI 核心 · 红线闸门

- **状态**：进行中
- **目标**：立起 `.agents/skills/manga/`，并让 `manga verify` 能真实跑通既有校验器 + 红线断言。
- **实施步骤**
  1. 建 `.agents/skills/manga/SKILL.md`（frontmatter + 分派规则 + 红线）与 `references/` 骨架（先建 `pipeline.md`、`troubleshooting.md` 两篇，其余留占位）。
  2. 建 `src/manga/` 包：`envelope.py`（统一 JSON + 退出码）、`config.py`（profile / 路径解析）、`cli.py`（typer 入口）。
  3. 建 `adapters/pipeline.py`：封装 4 个 PS 脚本的调用，**解析退出码 0/1/2 → 语义化状态**。
  4. **红线闸门**：`adapters/pipeline.py` **不提供**灰色源的导出能力；并在 `verify` 子命令里加断言——记录 `Export\Manga` 的文件清单快照，前后对比必须零新增。
  5. `manga verify` 子命令：跑 `Test-MangaLibrary.ps1`，输出结构化报告（合规率、ComicInfo 覆盖率、CBZ 可读率、编号一致性、问题清单）。
  6. `manga ingest` 子命令：跑 `Invoke-LibraryIngest.ps1`，**默认 dry-run**，必须显式 `--apply` 才落盘。
  7. 单测：退出码映射、dry-run 不落盘、红线快照对比。
- **交付物**：`SKILL.md` v0、`src/manga/` 骨架、`manga verify` / `manga ingest`、pytest 用例。
- **验收标准**
  - [ ] `manga verify --library-root <path>` 能在本机真实库上跑出与 `Test-MangaLibrary.ps1` 一致的汇总数字
  - [ ] `manga ingest` 不加 `--apply` 时不产生任何文件变化
  - [ ] 红线断言通过：`Export\Manga` 在 ingest 前后文件清单**完全一致**
  - [ ] 退出码映射正确：PS 退出码 0/1/2 → CLI 0/0/2（1 = 告警不视为失败，但 `warnings` 非空）
- **回退**：CLI 尚未就绪时，SKILL.md 直接给 agent 原始 PowerShell 命令（可用的降级路径）。

### P1 · jmcomic 适配层 + SKILL.md v1

- **前置条件**：P0 验收通过
- **实施步骤**
  1. `adapters/jmcomic.py`：`search`（5 维 + 分类 + 3 榜）/ `album` / `comments` / `favorites` / `checkin`。
  2. `download` 子命令：album / photo / 批量；支持 `--dry-run`、`--filter`（预置档位）、`--control` 取消、`--threading` 并发封顶（内部走 `JmSyncRuntime`）。
  3. `export` 子命令：`export_pdf` / `export_zip` / `export_long_img` / `export_cover` / `calibre_metadata` / `delete_duplicated_files`。
  4. **锁定** jmcomic 的 `dir_rule.rule = Bd/Atitle/Pname`（与 `Convert-JmComicToCbz.ps1` 的两层结构约定一致；**不能写成 `Bd_Atitle/Pname`**，见下），`base_dir` 指向 `00-Incoming\_raw_jmcomic`。
  5. `domains` / `pick-domain` 子命令：域名三层策略 + IP 可达性探测。
  6. SKILL.md v1：完整分派规则 + 红线 + 写操作确认流程 + 错误处置表。
- **交付物**：`manga search/album/comments/download/export/domains/pick-domain`；SKILL.md v1。
- **验收标准**
  - [ ] `manga search` 五种维度都能出结果，且结果体与 jmcomic 实体字段一致
  - [ ] `manga download --dry-run` 不发请求不写文件
  - [ ] 下载可取消，取消后返回已完成 / 未完成 ID
  - [ ] 用了 filter 时**不**用 `all_success` 判成功（自建 manifest 统计）
  - [ ] `manga pick-domain` 能列出全部域名及可达性
  - [ ] 下载产生的目录结构与 `Convert-JmComicToCbz.ps1` 的期望完全一致
- **回退**：保留 SKILL.md 里的「直接调 `jmcomic` / `jmv` 命令」降级路径。

### P2 · references/ 全量知识表

- **前置条件**：P1 验收通过
- **状态**：✅ 已完成（2026-11-07 完成源码校准 + 人工复核）
- **实施步骤**
  1. `references/option-and-plugins.md`：**option 全字段表**（`log`/`version`/`auth`/`client`/`download`/`dir_rule`/`plugins`）+ **21 个插件的 kwargs 全表** + 依赖策略 + 钩子矩阵。
  2. `references/naming.md`：DirRule **29 个变量**表 + 命名/元数据规范（引 `spec/naming-and-metadata.md`）+ 繁简归一化 + 非法字符规则。
  3. `references/jmcomic.md`：16 模块 API 速查 + **html vs api 能力差异表** + 异常码表 + 「用了 filter 后 all_success 恒 False」等 13 条坑。
  4. `references/sources.md`：采集源选路矩阵（版本 / 命令 / 产物 / 失效点）+ 何时该人工用 FMD2 / Kapowarr。
  5. `references/servers.md`：Komga / Kavita / 手机端 / 增强支线路由。
- **交付物**：7 篇 references。
- **验收标准**
  - [x] 每个可配置字段、每个插件 kwarg 都能在 references 里查到（对照 2.7.11 源码逐条过）
  - [x] DirRule 变量表与 `get_properties_dict()` 实际可用的属性一致
  - [x] 每篇 reference 都有「何时读我」的分派提示，避免 agent 一次性全读
- **回退**：references 缺失时 agent 仍能用 SKILL.md 主体的最小集工作。

#### 2026-11-07 校准结果

| 项 | 结果 |
|---|---|
| 21 个插件 kwargs | ✅ 逐个对源码（`inspect.signature`）取真实形参；抓到 3 处文档与源码不符 |
| option 默认值 | ✅ 取自 `JmOption.default_dict()`；纠正 `threading.photo=24`、`dir_rule.rule=Bd_Pname`、`impl=api` |
| DirRule 变量 | ✅ 源码可用 **50** 个（A 25 + P 25），旧表只有 29 个；修掉拼写错误 `Aaidoname` |
| 证据等级 | ✅ `jmcomic.md` 每节标【实测】/【源码】/【文档】，并声明 html 端/收藏夹/签到/异步**本机未实测** |
| **人工复核** | ✅ 7 篇逐段对照源码/脚本/工作区，改掉 **12 处漂移**（含 2 处会让 agent 写出错误代码的：`Chapter 01` 被当合规、`toggle_favorite_album` 不存在） |
| 防腐烂 | ✅ `tools/jmcomic_facts.py check` + `tools/doc_claims.py check` + `tests/test_reference_facts.py`：上游升级后跑测试当场报漂移 |
| 剩余 | ✅ 无。P2 已完成 |

### P3 · 其余源 · 作业模型 · 收藏夹与自动化

- **前置条件**：P2 验收通过
- **状态**：✅ 已完成（2026-11-07）
- **实施步骤**
  1. `adapters/gallery_dl.py`：URL 预检（复用 `Test-GalleryDlUrl.ps1`）+ 下载 + CBZ 产物确认。
  2. `adapters/mangadex_dl.py`：下载 + ComicInfo 确认。
  3. `jobs.py`：**长任务作业模型**——`manga job start` 返回 job id 并后台跑；`manga job poll` 返回精简状态；`manga job cancel`；`manga job result` 返回摘要。进度落文件，agent 低频轮询。
  4. `manga actions-gen`：生成 `download_dispatch.yml`（10 个 inputs）+ `export_favorites.yml`，并给出 secrets 清单。
  5. `manga favorites` / `manga checkin`：`favorite_folder_export`（CSV + 可选带密码 zip）/ 每日签到。
  6. `servers.py`：Komga `POST /api/v1/libraries/:id/scan` 与 Kavita 手动扫描的**提示与检查**（不自动挂载、不自动改元数据）。
- **交付物**：`manga job/*`、`manga actions-gen`、`manga favorites`、`manga checkin`、`manga gallery-dl`、`manga mangadex-dl`。
- **验收标准**
  - [x] 一次 2 话的真实下载，agent 端模型调用 ≤ 10 次（SKILL.md §8 给了「只 poll 2~3 次」的规矩）
  - [x] `manga job cancel` 能真正停掉下载并给出已完成 / 未完成清单
  - [x] 生成的 workflow YAML 通过自检（10 个 inputs 用 `yaml.safe_load` 读回验证；`actionlint` 不可用 → 人工核对）
  - [x] `manga gallery-dl` 对不支持的 URL 明确返回「不支持」，不发起下载
- **回退**：作业模型出问题时退回「同步阻塞执行 + 长超时」。

#### 2026-11-07 实现要点

| 项 | 关键决策 |
|---|---|
| 作业状态由**谁**写 | **子进程自己写终态**（`final.json`）。父进程启动完就退出（它不该替用户等着），回收线程随之消失 → 父写终态永远写不到。`final.json` 与 `state.json` **分开**，避免「子进程秒退 → 父进程收尾写入把终态盖回 running」的竞态 |
| 进度协议 | **单向**：子进程写 `progress.json`，父进程只读；普通命令不在作业里跑时 `record_progress()` 直接返回 False，零影响 |
| 逐项进度 | jmcomic 的批量入口不暴露「哪个 ID 下完了」→ 有进度回调时**逐个 ID 驱动**（自己的线程池），否则保持原批量路径不变 |
| 红线 | 收藏夹清单**也是灰色来源** → 落点进共享区直接退出码 6；zip 密码只从环境变量读且不回显 |
| 破坏性操作隔离 | servers 模块把 HTTP 方法硬限制为 GET/POST，Kavita 只给指引 —— 结构上就做不了「改元数据 / 自动挂载」 |
| workflow 红线 | jmcomic 分支**不产生任何 artifact**，下载前另有一道「落点不得指向 Export/共享区」的检查 |

### P4 · 加固 · 测试 · 交付

- **实施步骤**
  1. 依赖探测：启动时检查 21 个插件的可选依赖，缺失给出安装命令（不自动 pip install）。
  2. 日志治理：CLI 启动即 chdir 到固定工作目录，杜绝 `jmcomic-download.log` 落在仓库根；日志路径集中配置。
  3. 退出前调用 `option.wait_all_plugins_finish()`，保证异步插件优雅停机。
  4. 测试：单测（envelope / 退出码映射 / DirRule 渲染 / 红线断言）+ 离线集成（本地 fixture 库）+ 真实冒烟（`JM_LIVE=1` 门控：1 本 2 话 → CBZ → 入库 → 校验退出码 0）。
  5. 交付：`uv tool install` 打包；README（安装 / 常用流程 / 排错）；可选 `npx skills link` 到 `user-agents` 便于全局使用。
- **验收标准**
  - [ ] 单测 + 离线集成全绿
  - [ ] `JM_LIVE=1` 冒烟通过
  - [ ] `uv tool install` 后 `manga --help` 可用
  - [ ] 仓库根不再出现 `jmcomic-download.log`
  - [ ] 上游 jmcomic 锁版本 + 升级流程写进 README

---

## 3. 里程碑与工时

| 阶段 | 一次性（人日） | 交付物 | 里程碑 |
|---|---|---|---|
| P0 | 5.0 | `manga verify` / `manga ingest` + 红线闸门 | M0 闸门可用 |
| P1 | 8.0 | jmcomic 全能力 + SKILL.md v1 | M1 主链路通 |
| P2 | 4.0 | references 全量知识表 | M2 知识完备 |
| P3 | 4.5 | 其余源 + 作业模型 + 自动化 | M3 聚合成立 |
| P4 | 3.0 | 加固 + 测试 + 交付 | M4 可长期使用 |
| **合计** | **24.5** | | +20% 缓冲 → 29.4 人日 ≈ **5.9 周** |

**关键路径**：P0 → P1 → P2 → P3 → P4（串行，无并行空间）。
**提前可用的切片**：P0 完成后 agent 就能「下载→入库→校验」；P1 完成后单本子全流程可用。

---

## 4. 风险登记册

| 风险 | 影响 | 概率 | 缓解 | 归属 |
|---|---|---|---|---|
| 站点域名频繁变更 / 反爬 | 下载整体失败 | 高 | `advanced_retry` + 运行时域名发现 + `pick-domain` 预检 | P1 |
| 上游 jmcomic 高频发版 | 适配漂移 | 高 | 锁版本；`references` 标注核对日期；README 写升级流程 | P4 |
| agent 分派不稳定 | 走错工具 / 漏步骤 | 中 | SKILL.md 写死分派表；不确定时走 CLI 严格校验、失败即停 | P1/P2 |
| 模型调用成本超预期 | token 账单 | 中 | 作业模型：批量交 CLI；每本子 ≤ 30 次调用 | P3 |
| 灰色源误入共享区 | 违规内容同步到手机 | **低但不可接受** | CLI 层**不提供**该能力 + 快照断言 + 自动化测试 | P0 |
| DirRule 改动破坏转换脚本 | 入库失败 | 中 | CLI 锁定 `Bd/Atitle/Pname`；转换逻辑内收到 Python；`tests/test_dirrule.py` 钉死渲染结果 | P1 |
| 站点返回结构变化 | 解析失败 | 中 | 解析失败即报错，不静默降级；`references` 记核对日期 | 持续 |
| PowerShell 5.1 编码坑 | 中文乱码 | 中 | 转换/校验逻辑内收到 Python，逐步摆脱 PS | P1→P4 |
| 作业残留进程 | 占用资源 | 低 | `job cancel` + 退出钩子 + 作业状态文件 | P3 |
| 平台合规风险 | 法律风险 | — | 仅自用、不分发、不入共享区；不提供批量分发功能 | 持续 |

---

## 5. 本项目产物的目录约定（增量）

```
manga/
├── .agents/skills/manga/           # ★ 新增：DSH 发现路径（project-agents）
│   ├── SKILL.md
│   └── references/
│       ├── jmcomic.md
│       ├── option-and-plugins.md
│       ├── naming.md
│       ├── sources.md
│       ├── pipeline.md
│       ├── servers.md
│       └── troubleshooting.md
├── src/manga/                      # ★ 新增：薄 CLI
│   ├── cli.py / envelope.py / config.py / jobs.py
│   ├── domains.py / actions.py / servers.py
│   └── adapters/{jmcomic,gallery_dl,mangadex_dl,pipeline}.py
├── tests/                          # ★ 新增
└── docs/
    ├── manga-aggregate-estimate.md # ★ 新增
    └── manga-skill-plan.md         # ★ 新增
```

既有目录（`scripts/`、`spec/`、`templates/`、`gallery-dl/`、`jmcomic/`、`drafts/`）**保持不变**。
