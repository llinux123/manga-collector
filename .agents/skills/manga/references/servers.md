# 服务端 · 阅读端 · 增强支线（何时读我：用户问「手机怎么读」「能不能建个网页库」「要不要 OCR」时）

> 版本核对日期 **2026-11-05**（实抓）。
>
> ⚠ **2026-11-07 复核说明**：本篇是**离线环境**下写的，服务端/阅读端的版本号
> **没有重新联网核对**（本机也没装这些工具）。下面的版本号请当作「2026-11-05 的快照」，
> 采纳前先查一下官网 release。**唯一与本机相关、且已复核**的是 ComicTagger 1.5.5
> 与「Kavita 归档内元数据覆盖解析结果」这类行为约定。

## 1. 服务端（**可选增强层**，不进主路径）

| | Komga | Kavita |
|---|---|---|
| 版本 | **1.30.0**（2026-11-04） | **0.9.1.4**（2026-09-02） |
| Windows 安装 | 官方 `KomgaInstaller.exe` / `windows-amd64.zip` / `x64.msix`；GitHub release **只有 jar** | 原生 `kavita-win-x64.tar.gz`（免 Java） |
| 目录规则 | 每个子文件夹 = 一个 Series，每个文件 = 一本书 | 系列各自一个文件夹，**库根不能直接放文件**，同名系列不能并排两个文件夹 |
| 卷/章解析 | 文件名 | 文件名（`vol 1`/`v01`/`S01`/`卷2`/`2巻`/`c1`/`034.5`/`c001-006`/裸数字） |
| ComicInfo.xml | **必须在包根目录** | 同上（v2.1 草案） |
| 触发扫描 | UI 手动 Scan；API `POST /api/v1/libraries/:id/scan`（202，需 ADMIN，`X-API-Key` 或 Basic） | UI「Scan Library」；默认开 Folder Watching（约 10 分钟延迟） |
| 去重 | 开「compute hash for files/pages」后有 Duplicate Files/Pages 视图 | 以元数据/解析分组为主 |
| Web 阅读 | 内置 Webreader + OPDS v1/v2 | 内置漫画阅读器 + OPDS / OPDS-PS |
| 内存 | JVM，偏高 | .NET，约 150–200 MB 空闲 |

**关键坑**

- **Komga 会把系列名改写成 `<Series> (<Volume>)`**（Volume≠1 且开启追加时）→ 与「目录名 == ComicInfo.Series」打架。
  要么不写 `Volume`，要么统一接受「系列名 (卷号)」形态。
- **Komga 的 `Manga` 只认 `YesAndRightToLeft` 才 RTL**，写 `Yes` 不生效。
- **Kavita：归档内元数据覆盖解析结果** → 文件名与 ComicInfo 必须一致。
- **扫描不到新文件**：Komga 靠父目录 mtime，Kavita 只看**分钟级**目录 mtime；**重命名/移动目录不一定改 mtime**
  → 改完先跑全库/深扫描；Kavita 手动 Analyze 会强制重算（重 IO，远程盘慎用）。
- Komga 改**库根路径**会丢失系列/书/进度；改子目录只丢部分。
- 同一目录同时挂 Komga 与 Kavita：Kavita 的卷目录对 Komga 是「子目录=新系列」风险。
- 改完文件名/元数据后，除扫描外还要 **Refresh metadata / Analyze**，否则页面仍是旧标题。
- **不要边下边扫** → 会重复入库或刮削到半成品。

**本项目约定（ADR-001 / 008 / 012）**

- 服务端**只挂 `Manga\Library`**（只读、本地 SSD）；不要把 `00-Incoming`、`Export` 交给它扫。
- **手动触发**扫描，不开 watch（网络盘会漏事件）；**不设开机自启**。
- ADR-012：Komga vs Kavita 的选型**待实测**，标准是 ① 免 Java ② 空闲内存 < 300 MB ③ OPDS 可用
  ④ 进度能否与 Komikku 对齐 ⑤ 条漫体验。
- **停掉服务端，主链路完全不受影响**（反向验收）。

## 2. 阅读端

| 端 | 版本 | 角色 | 注意 |
|---|---|---|---|
| KOReader | v2026.07.1 | 本地 CBZ 主力 + **跨端进度同步**（KOReader↔KOReader） | 桌面/安卓/墨水屏都有 |
| Mihon | v0.21.3 | 安卓主力漫画客户端（本地源指向 Syncthing 接收目录） | **0.21 是最后支持 `.tachibk` 旧备份的大版本**，换机恢复必须先 0.21.x |
| Komikku | v1.14.1 | 多库容器 / 同步与扩展试验位 | 比 Mihon 发版慢 |
| Kotatsu-Redo | 9.8.6 | 不装扩展时的追更备胎 | 内置源灰色，仅自用 |
| YACReader | 10.4.0 | Windows 桌面库管理与刮削 | 条漫支持弱 |
| CDisplayEx | 1.10.33 | 桌面轻量阅读（秒开、manga mode） | |
| SumatraPDF | **3.6.1 稳定版**（3.7 仍 pre-release） | 键盘流阅读 | `Ctrl+7` 双页、`c` 连续滚动、`LimitToWindowWidth` 防溢出（**这些属 3.7 特性**） |
| Venera-Next | 1.18.2 | 跨端只读（WebDAV 消费 `Export\Manga`） | **不内置任何源**；不读 `.cbz`（需先解压）；**绝不可**指向 `Manga\Library`（会复制/转码） |
| ComicTagger | 1.5.5（2022-11-26，仓库最后提交 2026-09-06） | 写 ComicInfo 的 GUI | release 停在 1.5.5 |

**阅读模式 profile**

| 内容类型 | 模式 | 关键设置 |
|---|---|---|
| 日漫（RTL 分镜） | 右开双页 | **先设 RTL 再开双页**；双页按高度适配，宽图才拼；展开态锁双页 |
| 韩漫/国漫条漫 | 长条连续滚动 | **绝不开双页**；注意超高图内存占用 |
| 美漫 | 左开单页 | 跨页按原图宽拼接 |

**16KB 页大小**：targetSdk 35+ 才支持，老 APK 在部分新机上会崩 → 只用新版本。

## 3. 增强支线（**建议拆成独立 skill**，本层只做路由）

| 支线 | 工具 | 版本 | 顺序原则 |
|---|---|---|---|
| 超分 | Real-CUGAN（更快、线条更干净）> Real-ESRGAN ncnn-vulkan（GPU 4GB+）> waifu2x | Real-ESRGAN release v0.3.0（2022-09-20，仓库停更） | **先换更清的源 → 再调对比度去边 → 最后超分** |
| OCR | manga-ocr v0.1.16（日文竖排，CPU 可跑但慢）；mokuro v0.2.5（整卷 OCR → 可点查 HTML，适合日语学习，**不做翻译**） | | 已汉化资源不必做 |
| 翻译 | BallonsTranslator v1.5.20（检测/OCR/擦字/翻译/排版一条龙，**建议 6GB+ 显存**） | | **机翻必须人工校对** |
| 墨水屏 | KCC 12.1.5（CBZ/EPUB/PDF/图片夹 → EPUB/KEPUB） | | 图像漫画优先 KCC；Calibre 管库与元数据，KCC 管转码 |
| 元数据 | ComicTagger 1.5.5 / Bangumi 脚本（中文元数据补充） | | 内嵌 ComicInfo 最稳 |

**原则**：每条支线独立开关、独立目录、产出**派生文件**，**不改原库**；原库校验必须仍为退出码 0。

## 4. 本项目硬规则（任何服务端/阅读端相关操作都适用）

1. `Manga\Library` 是**唯一事实来源**，**不进 Syncthing 共享**；只有 `Export\Manga` 共享。
2. **PC 关机也能读** —— 任何依赖 PC 在线的功能只能做增强层。
3. `Backups\Manga` 与库同盘，**不算备份**；必须有 ≥1 份**异地**副本 + 季度恢复演练。
4. **灰色来源（禁漫等未授权内容）不入主库、不分发、不进共享区**（ADR-004）。
5. **不装第三方扩展仓库**（ADR-011）；不绕过官方付费墙，不导出 DRM 内容。
6. 正版 DRM 内容不可导出 → 流程上禁止混入本地库。
