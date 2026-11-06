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

# 1) 体检（PowerShell / 脚本 / jmcomic / 代理端口 / 目录 / manga 工作区）
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

## 边界

- 不做 GUI（Web 层如需，另行叠加在 core 之上，估算见评估文档）。
- 不装第三方扩展；不替用户索要密码或 Cookie。
- 不做 OCR / 翻译 / 超分（建议拆成独立 skill）。
- 站点内容为未授权扫描件：仅自用、不分发、不入共享区。
