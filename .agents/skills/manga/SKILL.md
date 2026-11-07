---
name: manga
description: >
  漫画采集与入库：搜索/详情/评论/下载/导出/收藏夹/签到（禁漫 jmcomic、gallery-dl、MangaDex），
  以及 CBZ 转换、入库整理、库校验、共享区红线守卫。
  Use when 用户提到：漫画/本子/禁漫/JM/jmcomic/CBZ/入库/校验/追更/收藏夹/签到；
  或要把下载件变成一话一包 + ComicInfo.xml 的收藏库；或要检查漫画库质量。
  不做：不装第三方扩展；不替用户索要密码或 Cookie；不把灰色来源导出到手机同步区；
  不做 OCR/翻译/超分（另有独立 skill）。
version: 0.3.0
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
| 「下载 JM 404005」 | `manga doctor` 确认环境 → `manga download 404005`（**dry-run**，报章节数/图片数）→ 用户确认 → `manga download 404005 --apply` → §7 的入库三步 |
| 「搜个本子 / 找 XX 的图」 | `manga search "关键词" --mode site\|tag\|author\|work\|actor` |
| 「这本是啥 / 有几话」 | `manga album 404005` |
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
| `manga search` | 禁漫搜索（site/tag/author/work/actor 五维） | 否 |
| `manga album` | 本子详情（含章节列表） | 否 |
| `manga comments` | 本子评论（含回评树） | 否 |
| `manga domains` | 当前域名池（API 静态池 + 网页端域名） | 否 |
| `manga pick-domain` | 并发探测域名池，列出最快的可用域名 | 否 |
| `manga download` | 下载本子/章节到暂存区 | **默认 dry-run**，要 `--apply` |
| `manga export` | 导出 ZIP / PDF / 长图（走 jmcomic Feature） | 是 |
| `manga verify` | 只读校验权威库（命名/元数据/可读率/编号一致性/重复）；`--series <名>` 可只校验单个系列（增量验收） | 否 |
| `manga ingest` | 入库整理：重命名 + 写 ComicInfo.xml + 移入权威库 | **默认 dry-run**，要 `--apply` |
| `manga convert-cbz` | jmcomic 散图 → `<编号> - <标题>.cbz`（图片平铺包根） | **默认 dry-run**，要 `--apply` |
| `manga guard` | 红线守卫：查看/快照/比对共享区 `Export\Manga` | 只读（写快照文件除外） |

常用参数：`--library-root`（书库根，默认 `%USERPROFILE%\Documents\Library`）、
`--scripts-dir`（脚本目录）、`--report-path`（校验报告 CSV）、`--top-n`（问题条数）、
`--dest`（下载/导出落点，默认 `00-Incoming\_raw_jmcomic`）、`--impl api|html`。

**落点红线**：`manga download` / `manga export` 的落点若落在
`Export\Manga`（共享区）或 `Manga\Library`（权威库）→ **退出码 6**，这是代码强制的，改参数绕不过去。

## 3. 输出契约（必须遵守）

**stdout 只有一个 JSON 对象**，形状恒定：

```json
{"ok": true, "data": {}, "error": null, "warnings": [], "exit": 0}
```

退出码：

| exit | 含义 | 你怎么办 |
|---|---|---|
| 0 | 成功（**可能带 warnings**） | 读 `data`，把 `warnings` 一并转述给用户 |
| 1 | 参数 / 用法错 | 照 `error.message` 改参数，不重试 |
| 2 | 执行错误 | 读 `error.hint`（含脚本输出尾部），定位后再试 |
| 3 | 需要登录 / 凭据 | 见 §4 |
| 4 | 资源不存在 | 核对路径，或先跑 `manga doctor` |
| 6 | **红线拒绝** | **不要绕过、不要重试、不要换命令绕过**，见 §5 |

`ok: false` 时**绝不**把这一轮说成「已完成」。`warnings` 非空时要提一句。

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
gallery-dl --cbz -d "$env:USERPROFILE\Documents\Library\00-Incoming\Manga" "<URL>"

# MangaDex —— P3 起由 CLI 封装
mangadex-dl --save-as cbz --path "$env:USERPROFILE\Documents\Library\00-Incoming\Manga" "<URL>"
```

**所有下载器的落点统一是 `00-Incoming\Manga`，产物必须是一话一个 `.cbz`**（入库脚本只吃 cbz）。
下载完成后回到 §1 的「入库」三步。

> 禁漫走 `manga download`，落点是 `00-Incoming\_raw_jmcomic`（**不是** `00-Incoming\Manga`）——
> 散图先在暂存区，打包成 CBZ 后才进收件箱。

## 7. 写操作确认

`download` / `ingest` / `convert-cbz` **默认 dry-run**。流程固定：

1. 先跑**不带 `--apply`** 的版本，把计划（要下什么、要改什么、跳过什么、冲突什么）转述给用户；
2. 用户确认后再加 `--apply` 跑一次；
3. 跑 `manga verify` 确认退出码 0。

**不要**在用户没确认时直接 `--apply`。**不要**为了「让它过校验」而删文件或改元数据——校验器是闸门，不是待修的 bug。

## 8. 进度与长任务

- 下载大本子可能上百话：**不要一章一话地问用户**，一次确认整本，然后等结果。
- jmcomic 的进度日志（`jmcomic-download.log`）是**相对路径**，落点=当前工作目录 →
  CLI 调 jmcomic 前会先切到 `<书库根>\Manga\Metadata\logs`，日志跟着走，**不会掉进仓库根**。
  自己直接跑 `jmcomic` 命令时同理：别在仓库根跑。
- 断点续传：`download.cache: true`，中断后**重跑同一条命令**即可，已下的不会重来。
- 干跑（`download` 不加 `--apply`）取不到元数据时**不会失败**，会降级成最小计划并在
  `warnings` 里提示 → `--apply` 之前先看警告。

## 9. 参考资料（按需读，别一次全读）

| 什么时候读 | 哪一篇 |
|---|---|
| 要跑脚本 / 想知道退出码语义 / 入库流水线细节 | [references/pipeline.md](references/pipeline.md) |
| 报错了、下载不动、结果异常 | [references/troubleshooting.md](references/troubleshooting.md) |
| 要用 jmcomic 的具体 API（搜索/详情/评论/导出/插件/异步） | [references/jmcomic.md](references/jmcomic.md) |
| 要改 option 配置 / 配插件 | [references/option-and-plugins.md](references/option-and-plugins.md) |
| 要设计文件夹名 / 写 ComicInfo | [references/naming.md](references/naming.md) |
| 选采集源（禁漫 / gallery-dl / MangaDex / FMD2 / Kapowarr…） | [references/sources.md](references/sources.md) |
| 服务端（Komga/Kavita）/ 阅读端 / 增强支线 | [references/servers.md](references/servers.md) |

## 10. 红线之外的硬规则

- **不索要、不输出、不复制**用户的密码 / Cookie / 授权码。
- **不自动更新**依赖、不自动 `pip install`。
- 站点域名频繁变化，**失败一次就换域名/报告，不要无脑重试**超过 2 次。
- 抓到的内容是**未授权扫描件**：只自用、不分发、不入共享区。
