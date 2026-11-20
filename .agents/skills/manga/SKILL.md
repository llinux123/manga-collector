---
name: manga
description: >
  漫画采集与入库：搜索/详情/评论/下载/导出/收藏夹/签到（禁漫 jmcomic、gallery-dl、MangaDex），
  以及 CBZ 转换、入库整理、库校验、共享区红线守卫。
  Use when 用户提到：漫画/本子/禁漫/JM/jmcomic/CBZ/入库/校验/追更/收藏夹/签到；
  或要把下载件变成一话一包 + ComicInfo.xml 的收藏库；或要检查漫画库质量。
  不做：不装第三方扩展；不替用户索要密码或 Cookie；不把灰色来源导出到手机同步区；
  不做 OCR/翻译/超分（另有独立 skill）。
version: 0.4.0
metadata:
  project: manga
  repo: https://github.com/hect0x7/JMComic-Crawler-Python
---

# Manga 采集聚合层

**Skill 是界面，薄 CLI 是手，agent 是大脑。**

动手前先说一句「用 manga skill」。本 skill 被调用后全程有效，以下规则一直适用。

## 0. 一句话定位

用户说「下载 / 搜索 / 入库 / 校验」这类话时，**不要自己拼 PowerShell 命令**——用 `manga` CLI。
CLI 把所有确定性动作（跑脚本、解析退出码、守红线）做掉，你只负责：选命令、选参数、解析 JSON、汇报。

## 1. 选路表

| 用户说 | 你做什么 |
|---|---|
| 「下载 JM 404005」 | `manga doctor` 确认环境 → `manga download 404005 --dest .manga-tmp\raw`（**dry-run**，报章节数/图片数）→ 用户确认 → `manga job start download 404005 --dest .manga-tmp\raw --apply` → §7 的暂存入库流程 |
| 「搜个本子 / 找 XX 的图」 | `manga search "关键词" --mode site\|tag\|author\|work\|actor` |
| 「这本是啥 / 有几话」 | `manga album 404005` |
| 「这本在不在库 / 下过没」 | `manga lookup 404005`（按车号精确判，`--title` 兜底；只读本地库） |
| 「看看评论区」 | `manga comments 404005` |
| 「导出成 zip / pdf / 长图」 | `manga export 404005 --format zip\|pdf\|longimg`（落点受红线约束） |
| 「把这个本子入库」 | `manga convert-cbz --apply` → `manga ingest --apply` → `manga verify` |
| 「检查一下库」 | `manga verify`（**整库闸门**：存量库有历史问题时退出码就是 2；要验收刚入库的那本 → `manga verify --series <系列名>`，系列名见 `verify` 输出里的 `series_available`） |
| 「看看下载到哪了 / 收件箱里有什么」 | `manga paths` 看落点，再列目录 |
| 「导出到手机」 | **先问来源等级**：白/绿来源才导出到共享区；灰色来源**直接拒绝**（§5） |
| 「哪些域名能访问 / 下载报重试耗尽」 | `manga pick-domain`（挑最快的）→ `manga domains`（看池子） |
| 「导出收藏夹 / 签到」 | `manga favorites` / `manga checkin`（P3 起可用） |
| 「帮我配一下」 | 读 [references/option-and-plugins.md](references/option-and-plugins.md) 再改本仓库的 `config/jmcomic-option.yml` |
| 「收藏夹命名 / 文件夹怎么排」 | 读 [references/naming.md](references/naming.md) |

## 2. 命令清单

CLI 位置：本仓库的 `.venv/Scripts/manga.exe`（或 `python -m manga`）。
书库侧的流水线脚本在 **manga 工作区**，路径解析见 [docs/workspace-dependency.md](../../../docs/workspace-dependency.md)。

| 命令 | 作用 | 写操作 |
|---|---|---|
| `manga paths` | 打印所有解析后的路径（收件箱/权威库/共享区/脚本目录） | 否 |
| `manga doctor` | 环境体检：PowerShell、脚本、jmcomic（**导入 + 版本下限**）、**登录态**、**可选依赖**、代理端口、目录、工作区 | 否 |
| `manga search` | 禁漫搜索（site/tag/author/work/actor 五维）+ 分类浏览 + 排行榜 | 否 |
| `manga album` | 本子详情（含章节列表） | 否 |
| `manga lookup` | 判断一本是否**已在库中**：`manga lookup <车号>`（`--title` 兜底）。车号来自 ComicInfo 的 `Notes: jm:<id>` | 否 |
| `manga comments` | 本子评论（含回评树） | 否 |
| `manga domains` | 当前域名池（API 静态池 + 网页端域名） | 否 |
| `manga pick-domain` | 并发探测域名池，列出最快的可用域名 | 否 |
| `manga download` | 下载本子/章节到暂存区（`--option <file>` 换变体配置） | **默认 dry-run**，要 `--apply` |
| `manga export` | 导出 ZIP / PDF / 长图（走 jmcomic Feature） | 是 |
| `manga verify` | 只读校验权威库（命名/元数据/可读率/编号一致性/重复）；`--series <名>` 可只校验单个系列（增量验收） | 否 |
| `manga ingest` | 入库整理：重命名 + 写 ComicInfo.xml + 移入权威库 | **默认 dry-run**，要 `--apply` |
| `manga convert-cbz` | jmcomic 散图 → `<编号> - <标题>.cbz`（图片平铺包根） | **默认 dry-run**，要 `--apply` |
| `manga guard` | 红线守卫：查看/快照/比对共享区 `Export\Manga` | 只读（写快照文件除外） |
| `manga job start <命令> [参数…]` | **后台**跑一个长任务，立刻返回 job id | 视被调命令而定 |
| `manga job poll <job_id>` | 查作业状态（精简，秒回） | 否 |
| `manga job cancel <job_id>` | 取消作业（**协作式**：写取消标记，子进程自己停下），给出已完成 / 未完成清单 | 是（终止下载） |
| `manga job result <job_id>` | 作业最终摘要（子进程的信封 + 逐项结果） | 否 |
| `manga job list` | 列出最近的作业 | 否 |
| `manga favorites` | 导出收藏夹为 CSV（可选打 zip） | 是 |
| `manga checkin` | 每日签到 | 是（写站点状态） |
| `manga gallery-dl` | gallery-dl（通用站）：**默认预检**（判断 URL 支不支持），`--apply` 才下载 | **默认预检**，要 `--apply` |
| `manga mangadex-dl` | MangaDex 专项：**默认预检**，`--apply` 才下载（产物 CBZ 带 ComicInfo.xml） | **默认预检**，要 `--apply` |
| `manga actions-gen` | 生成 GitHub Actions workflow（download_dispatch + export_favorites）+ secrets 清单 | **默认 dry-run**，要 `--apply` |
| `manga server check` | 探测 Komga / Kavita 是否可达（**网络失败不报错**，返回原因） | 否 |
| `manga server libraries` | 列出 Komga 的书库（id / 名称 / 根路径） | 否 |
| `manga server scan <库id>` | 触发 Komga 扫描某个书库（需 ADMIN；**只触发扫描**，不改元数据） | 是（触发服务端扫描） |
| `manga server kavita-hint` | Kavita 的扫描**指引**（本项目不自动做） | 否 |

常用参数：`--library-root`（书库根，默认 `%USERPROFILE%\Documents\Library`）、
`--scripts-dir`（脚本目录）、`--report-path`（校验报告 CSV）、`--top-n`（问题条数）、
`--dest`（下载/导出落点，默认 `00-Incoming\_raw_jmcomic`）、`--impl api|html`。

**车号（album id）链路**：`manga download --apply` 成功后会在每本目录写 `.jm-album.json`；
`manga convert-cbz` 把它带进收件箱的系列目录；`manga ingest --generate-comicinfo` 再写进
CBZ 的 `ComicInfo.xml`（`<Notes>jm:<id></Notes>` + `<Web>https://18comic.vip/album/<id>/</Web>`）。
`manga lookup <id>` 与入库时的重复判重都以这个车号为准，标题匹配只作兜底。

**落点红线**：`manga download` / `manga export` 的落点若落在
`Export\Manga`（共享区）或 `Manga\Library`（权威库）→ **退出码 6**，这是代码强制的，改参数绕不过去。
透传参数（`--option` / `--config` / `-o` / `--`）里的路径也会被扫 —— 指向红线目录一样拒。

⚠ **`--option` 变体里的插件也要过红线**：jmcomic 的 `zip` / `img2pdf` / `long_img` /
`download_cover` 等插件**自带输出目录参数**（`zip_dir` / `pdf_dir` / `img_dir` …），
只覆盖 `dir_rule.base_dir` 挡不住它们。CLI 在**构造 option 阶段**就把这些目录逐个校验，
命中共享区/权威库 → 退出码 6；不在白名单里的插件一律拒绝（宁可误拒，不可漏放）。

**透传约定**（各源的高级能力不逐个映射成 CLI 参数）：

| 源 | 透传方式 |
|---|---|
| jmcomic | `--option <file>` 换一份变体配置（插件/threading/domain 全在里面） |
| gallery-dl | `--config <file>`（**替换**默认的合规命名配置）+ `-o key=value`（可多次）+ `--` 之后的原样参数 |
| mangadex-dl | `--` 之后的原样参数 |

预检（不加 `--apply`）**不透传** —— 预检只回答「支不支持」。

### gallery-dl 的两个默认行为（不用传参就知道）

1. **默认套用「入库合规命名」配置** —— gallery-dl 原生模板产出 `v01 c001` 这类名字，
   **过不了**入库闸门（要求 `<话号> - <标题>.cbz`）。本项目自带配置把它改成合规形态，
   `manga gallery-dl` 直接套用；要上游原生命名就显式 `--config <file>`。
2. **退出码位 4 = 部分文件下载失败** —— 产物 CBZ 已写出但少了几页 → 报
   `PARTIAL_DOWNLOAD`（退出码 2），**不是**成功。**重跑同一条命令即可续传**。

## 3. 输出契约（必须遵守）

**stdout 只有一个 JSON 对象**，形状恒定：

```json
{"ok": true, "data": {}, "error": null, "warnings": [], "exit": 0}
```

退出码：

| exit | 含义 | 你怎么办 |
|---|---|---|
| 0 | 成功（**可能带 warnings**） | 读 `data`，把 `warnings` 一并转述给用户 |
| 1 | 参数 / 用法错（`BAD_USAGE`） | 照 `error.message` 改参数，不重试。**用法错也是 JSON 信封**，stdout 不会出现帮助文本 |
| 2 | 执行错误 | 读 `error.hint`（含脚本输出尾部），定位后再试 |
| 3 | 需要登录 / 凭据 | 见 §4 |
| 4 | 资源不存在 | 核对路径，或先跑 `manga doctor` |
| 5 | 被取消 | 见 §9 |
| 6 | **红线拒绝** | **不要绕过、不要重试、不要换命令绕过**，见 §5 |

`ok: false` 时**绝不**把这一轮说成「已完成」。`warnings` 非空时要提一句。

**常见 exit 2 的错误码**（在 `error.code` 里）：

| code | 含义 | 你怎么办 |
|---|---|---|
| `PARTIAL_DOWNLOAD` | 部分 ID 下载失败，但已有产物 | 报给用户；**重跑同一条命令即可续传** |
| `NO_ARTIFACT` | 「下载成功」但 0 张图落盘（假成功防护） | 别当成成功；查 ID 与代理 |
| `CORRUPT_ARTIFACT` | 产物里有读不动的 CBZ | 删掉坏包后重跑同一条命令 |
| `SCRIPT_ERROR` | PowerShell 脚本异常终止 | 看 `error.hint`（stderr 尾部） |
| `CANCEL_FAILED` | 取消**没生效**（子进程还在跑） | **别重跑**！先让用户结束那个进程 |

## 4. 登录与凭据

- **不要替用户索要密码或 Cookie**，不要输出、复制、猜测凭据。
- 需要登录时告诉用户二选一，由用户自己设置：
  - 账号密码：`setx JM_USERNAME "xxx"` / `setx JM_PASSWORD "xxx"`（推荐，html+api 都可用）；
  - 浏览器 Cookie（仅网页端、且**必须来自正在访问的那个域名**）。
- 两种**不要同时配**：同时配时若 Cookie 已过期，会**静默按未登录下载**。
- Cookie 不会自动刷新；换域名后 Cookie 立即失效 → 改用账号密码。

## 5. 红线（最高优先级，不可绕过）

1. **灰色来源（禁漫等未授权成人漫画扫描件）的产物不得进入 `Export\Manga`** —— 那是 Syncthing 共享区，
   会同步到手机。`manga download` / `manga export` 的落点若指向共享区，**CLI 直接以退出码 6 拒绝**
   （代码强制，改参数绕不过去）；`manga guard` 会检测共享区新增文件并同样拒绝。
2. **下载器不得直写 `Manga\Library`** —— 那是权威库，必须经 `ingest` 的校验闸门。CLI 同样拒绝（退出码 6）。
3. **不替用户分发、不做批量分发、不把灰色内容写进共享库。**
4. **不把 `Manga\Library` 设为 Syncthing 共享目录**（只共享 `Export\Manga`）。
5. 触发红线时：说明**哪个对象**越界、**为什么**，然后**停下**，不要换个命令再试一次。

## 6. 降级路径（CLI 还没覆盖的能力）

禁漫侧（搜索/详情/评论/下载/导出/域名）已由 CLI 覆盖。下列能力**直接用原生命令**，但仍然守上面的红线：

```powershell
# 通用站 / 图源（gallery-dl）—— P3 起由 CLI 封装
# ⚠ 走 CLI（manga gallery-dl）才有「入库合规命名」；直接调原生命令会产出
#    v01 c001 这类名字 → 过不了入库闸门。
gallery-dl --cbz -d "$env:USERPROFILE\Documents\Library\00-Incoming\Manga" "<URL>"

# MangaDex —— P3 起由 CLI 封装
mangadex-dl --save-as cbz --path "$env:USERPROFILE\Documents\Library\00-Incoming\Manga" "<URL>"
```

**所有下载器的落点统一是 `00-Incoming\Manga`，产物必须是一话一个 `.cbz`**（入库脚本只吃 cbz）。
下载完成后回到 §1 的「入库」三步。

> 禁漫走 `manga download`，落点是 `00-Incoming\_raw_jmcomic`（**不是** `00-Incoming\Manga`）——
> 散图先在暂存区，打包成 CBZ 后才进收件箱。沙箱会话里这些落点全部换成 `.manga-tmp\`（见 §7）。

## 7. 落点策略：工作区暂存优先（沙箱友好）

**默认落点（`%USERPROFILE%\Documents\Library\00-Incoming\*`）在 DSH 会话工作区之外。**
沙箱（workspace-write）模式下每一次写都要弹授权；更糟的是**被拒时可能静默失败**
（作业报成功、实际 0 个文件）。所以默认流程是：

**下载与打包一律落在本仓库的 `.manga-tmp\` 暂存区，只有最后的入库才碰书库。**

| 步骤 | 命令 | 写哪 | 要授权吗 |
|---|---|---|---|
| 1. 下载散图 | `manga job start download <id> --dest .manga-tmp\raw --apply` | 工作区内 | 否 |
| 2. 打包 CBZ | `manga convert-cbz --source-root .manga-tmp\raw --target-root .manga-tmp\cbz --apply` | 工作区内 | 否 |
| 3. 入库 | `manga ingest --inbox-root .manga-tmp\cbz --generate-comicinfo --zero-pad --apply` | 权威库 | **仅此一步** |
| 4. 验收 | `manga verify --series <名>` | 只读 | 否 |

规矩：

- `.manga-tmp\` 已被 `.gitignore` 覆盖（`.manga-tmp*/`），不会进版本库；它不是共享区，
  暂存灰色产物不碰红线（红线只挡 `Export\Manga` 与 `Manga\Library`，代码强制，见 §2 落点红线）。
- **落盘必核对**：`convert-cbz` / `ingest` 的 `--apply` 之后看信封 `data.report`——
  `nothing_to_do: true` 或 `errors > 0` 都**不要**当作成功继续；
  `planned_or_succeeded` 为 0 时列一次落点目录排查（写被拦时信封仍可能 `ok: true`）。
  只有 `report.parsed: false`（旧版工作区脚本没被认出）才回退到旧做法：列目录确认产物数量 > 0。
- 入库并验收通过后，清掉 `.manga-tmp\` 里对应的散图与 CBZ（要留档重跑也行，磁盘够就留）。
- 需要用户确认的仍然只有 §8 的 dry-run → `--apply` 环节；暂存区内的 dry-run 随便跑。

## 8. 写操作确认

`download` / `ingest` / `convert-cbz` **默认 dry-run**。流程固定：

1. 先跑**不带 `--apply`** 的版本，把计划（要下什么、要改什么、跳过什么、冲突什么）转述给用户；
2. 用户确认后再加 `--apply` 跑一次；
3. 跑 `manga verify` 确认退出码 0。

**不要**在用户没确认时直接 `--apply`。**不要**为了「让它过校验」而删文件或改元数据——校验器是闸门，不是待修的 bug。

## 9. 进度与长任务

**下载大本子可能跑几分钟到几十分钟 —— 别用一条同步命令死等。** 用作业模型：

```powershell
manga job start download 404005 390442 --apply     # 立刻返回 job_id，不等
manga job poll <job_id>                              # 几秒就回：state / 已完成几个 / 剩余几个
manga job result <job_id>                            # 终态：子进程的信封 + 逐项结果
manga job cancel <job_id>                            # 真能停：给已完成 / 未完成清单
```

| `job poll` 的 `state` | 含义 | 你怎么办 |
|---|---|---|
| `running` | 还在跑 | **等 15~30 秒再 poll 一次**，别高频轮询 |
| `succeeded` | 成功 | 读 `job result`，然后跑 `manga verify` |
| `failed` | 失败 | 读 `job result` 的 `error` / `stderr_tail` 再决定 |
| `cancelled` | 被取消 | 读 `job result` 的 `done` / `failed`，告诉用户下到哪了 |

**省调用次数的规矩**（硬指标：一次 2 话的真实下载，模型调用 ≤ 10 次）：

1. 一次确认整本 / 整批，**不要**一章一话地问用户；
2. `job start` 之后**只 poll 2~3 次**（每次间隔 ≥ 15 秒），不要 sleep 式轮询；
3. 拿到终态就 `job result` 一次拿全，**不要**再去翻日志；
4. 需要入库时：`convert-cbz --source-root .manga-tmp\raw --target-root .manga-tmp\cbz --apply` →
   `ingest --inbox-root .manga-tmp\cbz --apply` → `verify --series <名>`，**每步一次调用**（§7）。

**作业取消是协作式的**：`job cancel` 只写一个取消标记，**子进程自己**看到后退出
（受限环境里外部杀「别的进程创建出来的进程」常被拒 —— 沙箱、容器、CI 都这样）。
所以取消后最多等几秒，作业就会变成 `cancelled`；`job result` 里能看到已完成/未完成清单。

⚠ **取消只在「确认子进程真的退出了」之后才生效**：如果取消标记写不进去
（目录不可写）**且**强杀又被环境拒绝，CLI 会报 `CANCEL_FAILED`（退出码 2）
并**保持作业状态不变** —— 因为子进程还在下载、还在往库里写文件。
这时候**不要重跑**（两个任务会互相踩），先让用户在宿主上结束那个进程。

**两级取消**（jmcomic）：看门狗看到标记 → 先调 `DownloadControl.cancel()`（给 jmcomic
一个在下一个检查点**优雅停下**的机会，保留已下载的进度）→ 等 5 秒还没退出 → `os._exit` 强退。
外部下载器（gallery-dl / mangadex-dl）：直接杀孙进程（`Popen.kill`，不需要特权）。

- jmcomic 的进度日志（`jmcomic-download.log`）是**相对路径**，落点=当前工作目录 →
  CLI 调 jmcomic 前会先切到 `<书库根>\Manga\Metadata\logs`，日志跟着走，**不会掉进仓库根**。
  自己直接跑 `jmcomic` 命令时同理：别在仓库根跑。
- 断点续传：`download.cache: true`，中断后**重跑同一条命令**即可，已下的不会重来。
- 干跑（`download` 不加 `--apply`）取不到元数据时**不会失败**，会降级成最小计划并在
  `warnings` 里提示 → `--apply` 之前先看警告。
- 作业目录：`<书库根>\Manga\Metadata\jobs\<job_id>\`（`state.json` / `progress.json` /
  `final.json` / `envelope.json` / `error.log`）。书库不可写时自动退到系统临时目录。

## 10. 参考资料（按需读，别一次全读）

| 什么时候读 | 哪一篇 |
|---|---|
| 要跑脚本 / 想知道退出码语义 / 入库流水线细节 | [references/pipeline.md](references/pipeline.md) |
| 报错了、下载不动、结果异常 | [references/troubleshooting.md](references/troubleshooting.md) |
| 要用 jmcomic 的具体 API（搜索/详情/评论/导出/插件/异步） | [references/jmcomic.md](references/jmcomic.md) |
| 要改 option 配置 / 配插件 | [references/option-and-plugins.md](references/option-and-plugins.md) |
| 要设计文件夹名 / 写 ComicInfo | [references/naming.md](references/naming.md) |
| 选采集源（禁漫 / gallery-dl / MangaDex / FMD2 / Kapowarr…） | [references/sources.md](references/sources.md) |
| 服务端（Komga/Kavita）/ 阅读端 / 增强支线 | [references/servers.md](references/servers.md) |

> references 里带 **【实测】/【源码】/【文档】** 标记的段落可信度不同：
> 【文档】级的（html 端、收藏夹、签到、异步）**本机没验过**，用之前先小规模试。
> 上游 jmcomic 升级后，用 `uv run python tools/jmcomic_facts.py check` 查 references 是否过期。

## 11. 红线之外的硬规则

- **不索要、不输出、不复制**用户的密码 / Cookie / 授权码。
- **不自动更新**依赖、不自动 `pip install`。
- 站点域名频繁变化，**失败一次就换域名/报告，不要无脑重试**超过 2 次。
- 抓到的内容是**未授权扫描件**：只自用、不分发、不入共享区。
