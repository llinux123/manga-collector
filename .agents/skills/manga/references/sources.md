# 采集源选路（何时读我：用户要下载某个站点的漫画、或问「用什么工具下」时）

> 版本核对日期 **2026-11-07**（GitHub Releases API / Codeberg API / 官方站点实抓）。
>
> **本机状态复核（2026-11-07）**：
> - `gallery-dl` **已装 1.32.19**（scoop：`~/scoop/apps/gallery-dl/1.32.19`）✅ 与上表一致
> - `mangadex-dl` **本机未安装**（P3 做适配器前要先装）
> - `jmcomic` / `jmv` 已装（2.7.11）✅
> - ⚠ `gallery-dl.exe --version` 在受限环境下会报
>   `[PYI-xxxxx:ERROR] Failed to create parent directory structure.`
>   —— PyInstaller 想在不可写的位置建目录。**不是坏了**，换个可写的工作目录即可；
>   P3 的适配器要处理这个（先 chdir 到可写目录再调它）。

## 1. 选路矩阵

| 源 | 版本 | 形态 | 收件箱产物 | 自动写入 ComicInfo | 推荐度 | 何时用 |
|---|---|---|---|---|---|---|
| **jmcomic** | 2.7.11 | Python CLI + API | 散图 → 需 `convert-cbz` | ❌ | ★★★★★（禁漫唯一解） | 禁漫天堂 / JM 系 |
| **gallery-dl** | 1.32.19（上游已迁 Codeberg） | CLI | **直接产出 CBZ** | ❌ | ★★★★★（通用事实标准） | 绝大多数站点，268 分类 |
| **mangadex-downloader** | 3.1.4（**本机未安装**） | CLI | `--save-as cbz` | ✅ **默认写** | ★★★★ | MangaDex 专项 |
| **FMD2** | 2.0.34.5 | GUI + Lua 模块 | 可选 CBZ/PDF/EPUB | 未核实 | ★★（人工） | gallery-dl 不收录的站；**需手动操作** |
| **Kapowarr** | V1.3.2 | 服务端（*arr 式） | 系列/卷/期 CBZ | ❌ | ★★（人工） | 追更 + 种子场景；需 ComicVine API key |
| ~~Suwayomi-Server~~ | v2.4.2366 | 服务端 + 扩展 | `downloadsPath` | 只读不写 | ✖ **排除** | ADR-011：需自备扩展仓库 + 常驻 JVM + PC 在线 |
| ~~Hakuneko~~ | 6.1.7 | GUI | CBZ | ❌ | ✖ **排除** | 2020 年起 Unmaintained |

## 2. 各源要点

### jmcomic（禁漫）

- **gallery-dl 不收录 18comic**：该站图片是混淆 URL（`/msp/<scramble>`），要还原必须实现站点自己的解密算法。
  jmcomic 已内置（`download.image.decode: true`），并且自带域名自动切换。
- 唯一有完整 Python API 的源 → 做自动化、收藏夹、签到、评论只能靠它。
- 落点：`dir_rule.base_dir` 指向 `00-Incoming\_raw_jmcomic`，`rule: Bd/Atitle/Pname`（两层），再由
  `Convert-JmComicToCbz.ps1` 打包成 CBZ。
  ⚠ 别写成 `Bd_Atitle/Pname` —— 分隔符与写法混用会生成字面量目录 `Bd_Atitle`（详见 [naming.md](naming.md)）。
- **红线**：产物**只进权威库，不进 `Export\Manga`**。
- 域名天天变 → 必须配 `advanced_retry` + 留 `client.domain` 自动发现。

```powershell
jmv 404005 -y --option ".\jmcomic\option.yml"                     # 先看详情（不下载）
jmcomic 404005 --dry-run --option ".\jmcomic\option.yml"          # 干跑（会忽略 --option）
jmcomic 404005 --option ".\jmcomic\option.yml"                    # 下载
jmcomic p404005 p390442 --option ".\jmcomic\option.yml"           # 只下指定章节
```

### gallery-dl（通用）

- 事实标准，268 个分类；**本机已装 1.32.19**（scoop），备选 `uv tool install gallery-dl`。
- **不写 ComicInfo.xml** → 由 `Invoke-LibraryIngest.ps1 -GenerateComicInfo` 或 ComicTagger 补。
- **不提供** AniList / MyAnimeList / Bangumi 元数据（这三个分类不存在）。
- **不收录**：知乎、豆瓣、小红书、抖音、B站视频、微信公众号、淘宝/京东，以及 bato.to、mangasee123、
  mangakatana、chapmanganato、tsumino、kakibz。
- MangaDex 默认**不限语言**，要中文必须写 `lang=zh`。
- 离线自检（不下载不联网）：`pwsh -File .\gallery-dl\Test-GalleryDlUrl.ps1 -Url "<URL>"`。

```powershell
gallery-dl -s "<URL>"                                   # 模拟，不落盘
gallery-dl --cbz -d "$env:USERPROFILE\Documents\Library\00-Incoming\Manga" "<URL>"
```

### mangadex-downloader

- 只做 MangaDex；v3.1.4 之后未见新 release。
- API 限速要 `--delay-requests`。
- **默认写 ComicInfo.xml**（`--no-metadata` 关闭）；另可写 `manga_info.csv/json/mihon`。

```powershell
mangadex-dl --save-as cbz --path "$env:USERPROFILE\Documents\Library\00-Incoming\Manga" "<URL>"
```

### FMD2（人工补位）

- 原生 Pascal，**不需 .NET/Java**；章节可转 ZIP/CBZ/PDF/EPUB；Favorites 自动追更新章。
- **Lua 模块机制**（`--lua-dofile`、`LUA-REFERENCE.md`），模块要跟着源站维护。
- 失效点是公开的：仓库 issue 里几乎全是 `[bug-website module] <站点>`（源站一改模块就挂）。
- 下载目录设置、输出命名模板、能否写 ComicInfo **均未核实**。
- → **agent 不驱动它**；只在「gallery-dl 不支持该站 + 用户愿意手动操作」时推荐。

### Kapowarr（人工补位）

- *arr 式：RSS **每 30 分钟**抓新发布（比全量搜索更温和），但**旧期不会自动补**，要手点 "Search Monitored"。
- 元数据来自 **ComicVine**，需 API key。
- 跨 OS 远程路径映射**不支持**（Windows 客户端 + Linux 服务端组合无法翻译路径）。
- 模板变量 `{series_name}` `{volume_number}` `{issue_number}` + `Issue Padding`（`00x`/`000x`）、
  `Volume Padding`（`0x`）→ 与本项目「话号 3 位、卷号 2 位」口径一致。
- Library Import：扫 root folder 未匹配文件 → ComicVine 匹配 → Import / Import and Rename。
- → agent 不驱动它；只做选路说明。

## 3. 通用约定

- **所有下载器的落点统一是 `00-Incoming\Manga`，产物必须是一话一个 `.cbz`**（入库脚本只吃 cbz）。
- **绝不**让任何下载器直接写 `Manga\Library`（绕过校验闸门）。
- CBR/RAR 先转 CBZ（只重打包不重编码），未转件暂存 `Archive\Manga\BadFormats`。
- 断点续传靠各下载器自己的缓存；jmcomic 是 `download.cache: true`。
- 下载完统一走：打包 → `manga ingest` → `manga verify`。

## 4. 常见下载失败归因

| 现象 | 大概率原因 | 处理 |
|---|---|---|
| 全部超时 | 代理没开 / 端口错 | `manga doctor` 看 proxy 项 |
| `curl: (56) Connection closed abruptly` | 并发过高 | 降 `threading.image` / `threading.photo` |
| 下到乱码图 | `decode` 没开 | 开 `download.image.decode` |
| 「该 URL 不支持」 | gallery-dl 无此提取器 | 换源或人工用 FMD2 |
| 中途断了 | 网络抖动 | 重跑同一条命令（缓存生效） |
| 域名全失效 | 站点换域名 | `advanced_retry` + 运行时域名发现 |
