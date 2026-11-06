# 漫画采集聚合层 · 形态选型与工时评估

> 版本：v1.0 ｜ 评估日期：2026-11-06 ｜ 范围：`manga` 项目的**采集侧聚合层**
> 配套：[manga-skill-plan.md](manga-skill-plan.md)（分期实施计划）｜[decision-record.md](decision-record.md)｜[manga-reading-toolchain-plan.md](manga-reading-toolchain-plan.md)

---

## 0. 一分钟结论

| 问题 | 答案 |
|---|---|
| 「把 jmcomic 文档上提到的东西全做一遍」要多久？ | **纯程序形态：CLI 30 人日 / Web 45 人日**；**Agent Skill 形态：24.5 人日**（含把 gallery-dl 等其他源一起聚合进来） |
| 该做成程序还是 skill？ | **Skill + 薄 CLI**。Skill 是界面，CLI 是手，agent 是大脑。成本比 Web 低 46%，覆盖面更大，加新源成本从 3–6 人日降到 0.5–1.5 人日 |
| 为什么不是程序？ | 聚合层的成本不在「接几个工具」，而在把工具的**选项 / 坑 / 选路条件**编码成机器可读的知识。程序形态把它做成表单 + schema + 页面（贵、僵硬）；skill 形态它是 markdown 参考表（便宜、agent 运行时按需读） |
| 和本项目 ADR 契合吗？ | **完全契合**。ADR-008「主机按需开机 + 离线优先」→ skill 零常驻进程；ADR-011「不装第三方扩展」→ 只动 PC 侧；ADR-004「来源分级」→ 灰色源禁入 `Export\Manga` 由 CLI 硬性执行，不靠提示词约束 |
| 最大风险 | 形态 C 每次操作过模型（token + 延迟）。已用「长任务作业模型」把每本子的模型调用压到 10–30 次 |

---

## 1. 评估口径与证据基础

### 1.1 工时口径

- **1 人日** = 1 名熟练 Python 工程师 1 个工作日（8h）。
- **基线** = 净开发工时；**交付预期** = 基线 × 1.2（缓冲）。
- 不含：等待站点/网络的时间、需求变更、上游 jmcomic 版本迁移、合规审查。
- 日历换算按单人全职、每周 5 个工作日。

### 1.2 证据基础（本次实测，不是印象）

| 证据 | 方式 | 时间 |
|---|---|---|
| jmcomic 官方文档 **25 个页面**全量能力清单 | 逐页抓取 + 子代理交叉核对 | 2026-11-06 |
| jmcomic **2.7.11 源码**逐条验证 | 本机 `.venv` 安装包 AST 抽取 + grep（文档只渲染带 docstring 的成员，源码更全） | 2026-11-06 |
| CLI 真实参数 | 直接读 `.venv/Lib/site-packages/jmcomic/cli.py` | 2026-11-06 |
| 插件注册表 | `.venv/.../jm_plugin.py` 中 21 个 `plugin_key` 逐个 grep | 2026-11-06 |
| 异步客户端 | `.venv/.../jm_async_client.py`（与 sync 同构） | 2026-11-06 |
| 其他采集源 / 阅读端 / 服务端 | [drafts/route12.md](../drafts/route12.md)、[drafts/route34.md](../drafts/route34.md)、[drafts/versions-2026-11-05.md](../drafts/versions-2026-11-05.md)（28 项版本实抓） | 2026-11-05 |
| DSH skill 发现机制 | 读 DSH 安装包内 skill scope 表 | 2026-11-06 |

> readthedocs 的 `option_file_syntax` 渲染页会截断，`tutorial/1_github_actions` 只有截图无 YAML。
> 两者都用上游仓库源文件与本机安装包补齐，**结论以源码为准**。

---

## 2. 聚合范围

「汇聚到一起」不等于「把 jmcomic 包一层」。真实的聚合是五层：

```
Tier 1  采集源      jmcomic · gallery-dl · mangadex-downloader · (FMD2 / Kapowarr 仅人工)
   ↓
Tier 2  整理流水线   Convert-JmComicToCbz → Invoke-LibraryIngest → Test-MangaLibrary → Export-MangaLibrary
   ↓                                          （唯一命名/元数据口径：spec/naming-and-metadata.md）
Tier 3  权威库      Manga\Library（CBZ + 根目录 ComicInfo.xml）
   ↓
Tier 4  干净导出    Export\Manga（HardLink，唯一 Syncthing 共享区）→ 手机离线读
   ↓
Tier 5  消费方      Komga / Kavita（可选增强层）· KOReader / Mihon / Komikku / Venera-Next
```

增强支线（KCC 12.1.5 / manga-ocr 0.1.16 / mokuro 0.2.5 / Real-CUGAN / BallonsTranslator 1.5.20）
**建议拆成独立 skill**，聚合层只做路由——它们是一次性批处理，混进日常采集上下文会稀释分派规则。

---

## 3. jmcomic 文档全量能力清单（16 模块）

已核对 `jmcomic.readthedocs.io/zh-cn/latest/` 全部 25 个页面（首页 / roadmap / option_file_syntax /
api×8 / tutorial×16），并与本机 2.7.11 源码逐条对账。

### M1 连接 · 配置 · 登录 · 域名 · 重试 · 代理

| 能力 | 具体 API / 配置 |
|---|---|
| option 加载 | `create_option_by_file` / `create_option_by_env`(`JM_OPTION_PATH`) / `create_option_by_str` / `JmOption.default()` / `to_file` / `copy_option` |
| 客户端双实现 | `JmHtmlClient`（网页端，受 IP 地区限制）/ `JmApiClient`（APP 端，不限 IP）；`new_jm_client` / `build_jm_client` / `new_jm_async_client` |
| 域名三层策略 | ①静态列表 `client.domain.{html,api}` ②运行时发现 `JmModuleConfig.get_html_domain_all()` / `get_html_domain()` / `get_html_url()` ③`advanced_retry` 插件多轮轮询 + 单域名失败上限 + 失败计数排序 + 拉黑；api 端另有 `FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN` 自动更新路径 |
| 重试 | `client.retry_times`（默认 5）；`domain_retry_strategy` 可插拔（**异步端不支持，传了直接 TypeError**） |
| 代理 | `client.postman.meta_data.proxies`：`system` / `clash` / `v2ray` / `host:port` / `{http:,https:}`；`client_proxy` 插件按 impl 白名单分流 |
| TLS 指纹 | `client.postman.meta_data.impersonate`（默认 `chrome`） |
| 登录（5 种） | ①账号密码（推荐，html+api）②浏览器 cookies `AVS`（**仅 html**、**按域名区分**、不会自动刷新）③`${ENV}` 环境变量 ④遗留 `login` 插件（已废弃）⑤遗留 `client.postman.meta_data.cookies`（已废弃） |
| 登录写回 | `option.login_with_option_auth(force)` / `_async()` 幂等；`client.login()` 仅影响该 client；`auth.cookies` + `auth.user_id` 回写；`to_file()` **不**持久化 cookies 与 user_id |
| 缓存 | `client.cache`：`null`/`false`=关，`true`/`level_option`=option 级共享，`level_client`=每 client 独立（`CacheRegistry`） |

### M2 检索（5 个维度）+ 分类 + 排行 + 高级搜索

- `search_site`（站内，语法 `+包含` / `-排除` / 空格 AND）/ `search_tag` / `search_author` / `search_work` / `search_actor` —— 共 5 个 `main_tag` 维度
- `categories_filter(page, time, category, order_by, sub_category)` + `categories_filter_gen`
- 排行榜：`month_ranking` / `week_ranking` / `day_ranking`
- **高级搜索（category + sub_category）仅网页端**；**移动端不支持 category / sub_category**
- 分页生成器 `*_gen` 支持 `send()` 动态改条件（搜索 / 分类 / 收藏夹通用），异步版用 `asend()`
- 结果容器 `JmSearchPage`：`total` / `page_size`(80) / `page_count` / `iter_id_title()` / `iter_id_title_tag()` / `single_album`
- 常量 `JmMagicConstants`：`ORDER_BY_*`(6) / `ORDER_FF_*`(2) / `ORDER_*_RANKING`(3) / `TIME_*`(4) / `CATEGORY_*`(10) / `SUB_*`(12) / `SCRAMBLE_*`(3)

### M3 内容读取 · 评论 · 收藏状态

- `get_album_detail` / `get_photo_detail`；章节列表与图片列表可迭代
- `download_album_cover` / `download_by_image_detail` / `download_image(url, path, scramble_id, decode_image)`
- 实体三层：`JmAlbumDetail`（`oname`/`authoroname`/`idoname`/`distinct_episode`/`get_dirname`）、
  `JmPhotoDetail`（`album_index`/`indextitle`/`page_arr`/`get_img_data_original`）、
  `JmImageDetail`（`download_url` 带 `?v=` 缓存击穿参数 / `is_gif` 动图不解码不转码）
- 评论：本子评论 `album_pagination(_gen)`（`page_size`=10，含**回评树** `comment.replies`）/ 全站评论 `forum_pagination(_gen)` / **发表评论 `album_comment`（仅网页端，api 端抛 NotImplementedError）**
- 收藏状态 `is_favorite` / `liked`

### M4 下载引擎 · 过滤 · 取消 · 并发

- `download_album` / `download_photo`（单 + 批量）；`download_batch(_async)`
- 结果对象：`DownloadResult`（`detail`/`downloader`/`manifest`/`duration`，可解包成 `album, dler`）、`BatchResult`（`failed`/`all_succeeded`/`total`）、`DownloadManifest`（`get_export_filepath_list`）
- 过滤：重写 `do_filter(detail)`（album / photo / image 三级）；**必须返回可 `len()` 的对象，空时返回 `[]` 不能返回 `None`**
- 配置式过滤器：`image_suffix_filter` / `find_update` / `skip_photo_with_few_images`
- 图片解混淆 `download.image.decode`（必须开）、后缀统一 `download.image.suffix`、磁盘缓存续传 `download.cache`
- 并发三层：`download.threading.image`(默认 20，站点上限 50) / `download.threading.photo`(默认 CPU 核数) / 本子层 = 列表长度
- Runtime：`JmSyncRuntime(album_workers, photo_workers, image_workers)` / `JmAsyncRuntime(decode_workers)` / `JmTempRuntime`；**外传 executor 不会被 runtime 关闭（谁创建谁关）**
- 取消：`DownloadControl` + `jm_task_context(control=)` + `DownloadCancelledException`（带 `reason`/`completed_results`/`unfinished_ids`）
- 任务上下文：`ContextVar` 跨线程/协程传播 `task_id` / `download_type` / `jm_id` / `session_id` / `user_id`
- 异常体系：`MissingAlbumPhotoException` / `JsonResolveFailException` / `RequestRetryAllFailException` / `PartialDownloadFailedException` / `DownloadCancelledException` / `JmcomicException`
- 进度：`download_progress` 插件（rich，两级进度条 + 6 行日志尾窗 + `jmcomic-download.log` 全量落盘 + 非交互终端降级为汇总面板 + `preview()` 干跑预览）

### M5 导出 Feature

- `Feature.export_pdf`（img2pdf，支持 pikepdf 加密）、`Feature.export_zip`（zip / 7z，支持 pyzipper / py7zr 加密）、`Feature.export_long_img`（PNG 长图）
- 组合：`+` / `|` / `&` / list；`PluginFeature.__call__` 继承默认参数
- 自适应：`should_invoke` 按 `download_type` 决定 `after_album` / `after_photo`；`filename_rule` 默认 `[JM{Aid}]{Atitle}` / `[JM{Pid}]{Ptitle}`；目录默认取 `dir_rule.base_dir`
- 传统 YAML 插件写法 `img2pdf` / `zip` / `long_img` 等价
- 附加插件：`download_cover` / `calibre_metadata`（`jmcomic-calibre`）/ `delete_duplicated_files`（MD5 去重）/ `send_qq_email`

### M6 目录命名 DirRule

- `base_dir`（支持 `${ENV}`，默认 = 当前工作目录）
- `rule` DSL：**`/` 与 `_` 二选一、不可混用**；`Bd` = 根目录（可省，自动补）；f-string `{Aid}` 等 **29 个变量**（A 前缀 15 + P 前缀 14）；传统 `Axxx` / `Pxxx` 兼容写法
- `normalize_zh`（`zh-cn` / `zh-tw`，需可选依赖 `zhconv`，失败回退原串）
- Windows 非法字符修正 + 文件名长度上限 `VAR_FILE_NAME_LENGTH_LIMIT = 100`
- 自定义字段扩展：`JmModuleConfig.AFIELD_ADVICE` / `PFIELD_ADVICE`
- 路径全局替换插件 `replace_path_string`

### M7 插件框架 · 21 内置插件 · 自定义插件

- **7 个钩子**：`after_init` / `before_image` / `after_image` / `before_album` / `after_album` / `before_photo` / `after_photo`；另有 `main`（收藏夹导出）与 `after_download`（workflow 脚本用）
- **21 个 `plugin_key`**：`login` / `usage_log` / `download_progress` / `find_update` / `image_suffix_filter` /
  `replace_path_string` / `client_proxy` / `auto_set_browser_cookies` / `jm_server` / `subscribe_album_update` /
  `download_cover` / `zip` / `delete_duplicated_files` / `send_qq_email` / `calibre_metadata` / `img2pdf` /
  `long_img` / `skip_photo_with_few_images` / `favorite_folder_export` / `log_topic_filter` / `advanced_retry`
- 插件基类 `JmOptionPlugin`：`invoke` / `async_invoke` / `plugin_dependencies` / `required_dependencies_for(kwargs)` /
  `check_plugin_dependency(strategy)` / `install_missing_dependencies` / `build` / `require_param` /
  `execute_deletion` / `execute_cmd` / `execute_multi_line_cmd` / `enter_wait_list` / `leave_wait_list` /
  `wait_until_finish` / `decide_filepath` / `jm_task_context`
- 依赖策略：`plugins.dependencies_strategy` = `failed-fast`(默认) / `auto-install` / `ignore-only-log`；校验异常策略 `plugins.valid` = `ignore` / `log` / `raise`
- 自定义插件：继承 `JmOptionPlugin` → `JmModuleConfig.register_plugin(MyPlugin)` → YAML 里按 `plugin` + `kwargs` 引用
- 异步插件优雅停机：主程序退出前调 `option.wait_all_plugins_finish()`

### M8 CLI

| 命令 | 参数 |
|---|---|
| `jmcomic` | `id_list`（`p` 前缀 = 章节，`a` 前缀 = 本子，裸数字 = 本子）· `--option PATH`（默认取环境变量 `JM_OPTION_PATH`）· `--no-progress` · `--dry-run`（**忽略 `--option`**、与 `--no-progress` 互斥、需 `rich`） |
| `jmv` | `text`（正则抽全部数字拼接成车号）· `--option PATH` · `-y/--yes`（跳过回车暂停） |

> 本机 2.7.11 无 `__main__.py`，`python -m jmcomic` 不可用，必须用 console script。

### M9 收藏夹 · 签到

`favorite_folder(page, order_by, folder_id, username)` / `favorite_folder_gen` / `iter_folder_id_name`；
`add_favorite_album` / `delete_favorite_album` / `toggle_favorite_album`（`expected_type` 校验 + 失败回滚）；
`favorite_folder_export` 插件（CSV + 可选带密码 zip，事件 = `main`）；
`get_daily`（打卡日历）/ `daily_checkin`（`code` 0 成功 / 1 已签到）。

### M10 GitHub Actions 自动化

`download.yml`（push 触发）/ `download_dispatch.yml`（**10 个 inputs**：ID / CLIENT_IMPL / IMAGE_SUFFIX /
IMAGE_QUALITY / DIR_RULE / PDF_OPTION / PDF_NAME_RULE / ZIP_NAME / UPLOAD_NAME）/
`export_favorites.yml`（`main` 事件）/ `benchmark.yml` / `test_api.yml` / `test_html.yml` / `release_auto.yml`。
secrets：`JM_USERNAME` / `JM_PASSWORD` / `EMAIL_*` / `ZIP_PASSWORD`。产物走 `upload-artifact`（90 天保留）。
**只有两个 test workflow 有 cron，下载 workflow 全是按需触发。**

### M11 toolkit · 实体模型 · 常量

`JmcomicText`（`parse_to_jm_id` / `format_album_url` / `get_album_cover_url` / `to_zh` / `tokenize` /
`parse_daily_id`）· `HtmlTextParser` / `CommentParser` · `PatternTool` · `JmPageTool`（html + api 两套解析）·
`JmApiAdaptTool`（api 负载 → 实体）· `JmImageTool`（`decode_and_save` / `get_num` 解混淆）·
`JmCryptoTool`（`token_and_tokenparam` / `decode_resp_data`）· `JmModuleConfig`（8 个类方法 + 大量全局常量与 7 个 `FLAG_*` 开关）。

### M12 接入现有 manga 流水线

`Convert-JmComicToCbz.ps1` → `Invoke-LibraryIngest.ps1`（默认 dry-run）→ `Test-MangaLibrary.ps1`（退出码 0/1/2）
→ `Export-MangaLibrary.ps1`（HardLink）。
**红线：灰色源不走导出步**（`Export\Manga` 是 Syncthing 共享区）。

### M13 异步后端

`download_album_async` / `download_photo_async` / `download_batch_async`；
`AsyncJmHtmlClient` / `AsyncJmApiClient` / `AsyncJmcomicClient`（10 个抽象方法 + 异步生成器 `asend`）；
`JmAsyncDownloader`（IO 与 CPU 解密流水线化、`asyncio.Semaphore` 控并发）；异步版**不支持** `domain_retry_strategy`。

### M14 日志

`log: true/false/'pretty'`（Windows 自动开 VT100）· `disable_jm_log()` · `log_topic_filter` 插件 ·
自定义 `logging.Filter` 按 `record.topic` · 插件级 `log: false` ·
`jm_task_context(task_id=)` 任务打标（`record.jm_task_context`）· 自定义 `logging.Handler` 按任务聚合 ·
重定向到文件（`jm_logger.handlers.clear()` + `FileHandler`）· 接管 `JmModuleConfig.EXECUTOR_LOG(topic, msg, error)`。

### M15 测试 · M16 打包与文档

单元 + 离线集成 + 真实站点冒烟（`JM_LIVE=1` 门控）；`uv tool install` 打包；README / 命令手册 / 排错表。

---

## 4. 其他强力替代选择（聚合对象）

| 源 | 版本（2026-11-05 实抓） | 形态 | 聚合策略 | 理由 |
|---|---|---|---|---|
| **jmcomic** | 2.7.11 | Python CLI + 完整 API | **深度接入** | 唯一有完整 Python API 的源；内置混淆图还原 + 域名自动切换 |
| **gallery-dl** | 1.32.19（上游已迁 Codeberg） | CLI | **深度接入** | 事实标准，268 分类；**不收录 18comic**（混淆图 `/msp/<scramble>`） |
| **mangadex-downloader** | 3.1.4 | CLI | **接入** | MangaDex 专项，**默认写 ComicInfo.xml** |
| **FMD2** | 2.0.34.5 | GUI + Lua 模块 | **只做选路参考** | 无 CLI 自动化路径；源站改版即挂（issue 几乎全是 `[bug-website module]`） |
| **Kapowarr** | V1.3.2 | 服务端（*arr 式） | **只做选路参考** | 跨 OS 远程路径映射不支持；RSS 每 30 分钟抓新发布但**旧期不补**；元数据依赖 ComicVine API key |
| **Suwayomi-Server** | v2.4.2366 | 服务端 + 扩展 | **排除** | ADR-011：需自备扩展仓库 + 常驻 JVM + PC 在线 |
| **Hakuneko** | 6.1.7（Unmaintained） | GUI | **排除** | 2020 年起停更 |

### 消费方（只读）

- 服务端：Komga 1.30.0 / Kavita 0.9.1.4（ADR-012 选型待实测，按需启动，不进主路径）
- 手机：KOReader v2026.07.1 / Mihon v0.21.3 / Komikku v1.14.1 / Kotatsu-Redo 9.8.6（**不装扩展**）
- 跨端：Venera-Next 1.18.2（WebDAV 只读消费 `Export\Manga`；**不可**指向 `Manga\Library`，会复制/转码）

---

## 5. 三种形态的工时分解

### 5.1 形态 C · Agent Skill + 薄 CLI（推荐）

| # | 模块 | 人日 |
|---|---|---|
| C0 | Skill 骨架与发现验证 | 1.0 |
| C1 | CLI 核心（入口 / envelope / 退出码 / 配置） | 2.0 |
| C2 | jmcomic 适配层（16 模块薄封装） | 4.0 |
| C3 | 4 个 PS 脚本编排 + 红线闸门 | 2.0 |
| C4 | gallery-dl 适配 | 1.0 |
| C5 | mangadex-downloader 适配 | 0.5 |
| C6 | 长任务作业模型（start / poll / cancel / result） | 2.0 |
| C7 | SKILL.md 主体（分派规则 / 红线 / 确认流程 / 错误处置表） | 2.5 |
| C8 | references/ 7 篇（option 全字段 / 21 插件 kwargs / DirRule 变量 / 选路矩阵 / 流水线 / 故障排查） | 2.5 |
| C9 | 收藏夹 / 签到 / Actions 生成 / 域名探测 | 1.0 |
| C10 | 服务端联动（Komga / Kavita 手动扫描 API） | 1.5 |
| C11 | 选路矩阵（含 FMD2 / Kapowarr 等人工场景） | 0.5 |
| C12 | 测试（单测 + 离线集成 + 真实冒烟） | 2.5 |
| C13 | 文档与交付 | 1.0 |
| | **基线合计** | **24.5** |
| | **+20% 缓冲 → 29.4 人日 ≈ 5.9 周** | |

### 5.2 形态 A · 纯 CLI / TUI

| 模块 | 人日 |
|---|---|
| jmcomic 核心（检索 / 详情 / 评论 / 下载 / 导出 / 命名 / 插件配置界面） | 13.0 |
| 流水线编排 + 红线 | 2.0 |
| 其余 4 个源 | 2.0 |
| 检索 / 详情 / 评论 / 导出 / 命名 / 插件配置的**界面** | 5.5 |
| 队列 / 进度 / 日志呈现 | 1.5 |
| 服务端联动 | 1.5 |
| 测试 / 文档 / 打包 | 4.0 |
| **基线合计** | **30.0** → +20% = 36 人日 ≈ 7.2 周 |

### 5.3 形态 B · 本地 Web 应用（FastAPI + Jinja2 + 原生 JS + SSE）

| 模块 | 人日 |
|---|---|
| 骨架（FastAPI / 静态资源 / 免 Node 构建链 / 打包） | 3.5 |
| 服务层 | 2.5 |
| REST API（search / album / photo / comments / downloads / exports / favorites / config / plugins / logs） | 3.0 |
| 任务队列 + SSE 实时进度 + 取消 / 重试 / 并发闸门 | 3.0 |
| 检索页 | 2.5 |
| 详情页 | 2.0 |
| 下载中心 | 2.5 |
| 导出页 | 1.5 |
| 配置页（option + 21 插件 kwargs 表单） | 2.5 |
| 目录规则页 | 1.5 |
| 收藏夹 / 签到页 | 1.0 |
| 流水线编排 + 红线 | 2.0 |
| Actions 生成 | 1.0 |
| 异步后端 | 1.5 |
| 实体 / toolkit | 1.0 |
| 进度 / 日志呈现 | 1.0 |
| 其余 4 个源 | 5.0 |
| 服务端联动 | 2.5 |
| 测试（后端 + 前端 E2E） | 3.5 |
| 文档 + 启动脚本 | 1.5 |
| **基线合计** | **45.0** → +20% = 54 人日 ≈ 10.8 周 |

### 5.4 对比

| 形态 | 基线 | +20% | 日历 | 边际加一个源 | 常驻进程 |
|---|---|---|---|---|---|
| **C · Skill + CLI** | **24.5** | **29.4** | **≈ 5.9 周** | **0.5–1.5 人日** | **0** |
| A · CLI / TUI | 30.0 | 36.0 | ≈ 7.2 周 | 2–4 人日 | 0 |
| B · Web | 45.0 | 54.0 | ≈ 10.8 周 | 3–6 人日 | 1（需启停） |
| D · Skill + Web | 55.0 | 66.0 | ≈ 13.2 周 | — | 1 |

---

## 6. 形态 C 的取舍（诚实列出代价）

| 优点 | 代价 / 风险 | 缓解措施 |
|---|---|---|
| 最便宜（24.5 vs 45 人日） | 无 GUI，不能「随手翻库」 | 本项目是命令驱动型工作流，适配良好 |
| 加一个源只加一页 markdown + 一层包装 | 每次操作过模型 → token 与延迟 | 批量章节交 CLI 扛，agent 每本子只轮询 3–5 次（10–30 次模型调用） |
| 知识改一行就生效，不用发版 | 同一请求不同次可能走不同分支 | 分派规则写死在 SKILL.md；不确定时走 CLI 严格校验、失败即停不重试 |
| 与 ADR-008 / 011 完全契合，零常驻 | 长任务进度靠 agent 轮询，不如 Web 的 SSE 实时 | 作业状态落文件；真需要实时再叠 Web 层 |
| 确定性要求由 CLI 强制 | 不适合无人在场时自动跑 | 本项目本就是手动触发（ADR-008） |

---

## 7. 已核实的坑（必须进设计）

1. **文档 ≠ 源码**（3 处）：`download.threading.image` 文档 30 / 代码 **20**；`dir_rule.rule` 文档 `Bd / Ptitle` / 代码 **`Bd_Pname`**；`client.impl` 文档示例 `html` / 实际默认 **`api`**（`DEFAULT_CLIENT_IMPL`，源码注释与常量还自相矛盾）。
   → 以 2.7.11 源码为准；默认值运行时从 `JmOption.default()` 导出，不硬编码。
2. **html vs api 能力不对等**：评论 `total`/`page_count` 仅 api 有；category+sub_category 高级搜索仅 html 有；AVS cookies 仅 html 有效；**api 端不支持发评论**。
   → 参考表必须标「当前 impl 下不可用」。
3. **`all_success` 在用了 filter 后恒为 `False`** → 成功判据自建（基于 `manifest`）。
4. **`check_exception=True` 只对单个 ID 生效** → 批量必须读 `BatchResult.failed`。
5. **下载进度日志写死在 CWD**（`jmcomic-download.log` 无参数可改）→ CLI 必须 chdir 到固定工作目录。
6. **`dependencies_strategy: failed-fast`** 缺依赖直接启动失败 → CLI 启动探测 21 个插件的可选依赖
   （`img2pdf` / `pikepdf` / `psutil` / `py7zr` / `pyzipper` / `rich` / `browser_cookie3` / `zhconv` / `jmcomic-calibre` / `jm-view-server`）。
7. **自定义异步插件**需 `enter_wait_list` / `wait_until_finish` → CLI 退出前必须调 `option.wait_all_plugins_finish()`。
8. **DirRule 与现有 PS 脚本耦合**：`Convert-JmComicToCbz.ps1` 假设 `<base>\<本子>\<章节>\*.jpg`；改 `dir_rule.rule` 就失效
   → CLI 锁定 jmcomic 的 `rule` 为 `Bd/Atitle/Pname`，或把转换逻辑内收到 Python（推荐后者）。
9. **cookies 永不自动刷新**，且**按域名区分**——换域名即失效 → 域名切换后优先账号密码。
10. **并发乘数爆炸**：本子数 × `threading.photo` × `threading.image`，默认可达 320 线程 → 必须用 `JmSyncRuntime` 显式封顶。
11. **红线**：灰色源不得进入 `Export\Manga`（Syncthing 共享区）；`.nomedia` 只挡相册、挡不住阅读器扫描。
12. **上游发版极频**：2.7.0→2.7.11 约 5 个月 11 版（1–3 周一版）→ 锁版本 + 升级流程写进 README。
13. **合规**：禁漫内容为未授权成人漫画扫描件，仅自用、不分发、不入共享区。
14. **★ DirRule 分隔符与写法混用会静默产出错误目录**（2026-11-06 实测，本仓库原有 bug，已修）：
    `rule: Bd_Atitle/Pname` 渲染成 `<base>/`**`Bd_Atitle`**`<章节名>/` —— 因为分隔符是 `/`（按 `/` 切段），
    首段 `Bd_Atitle` 既不等于 `Bd`、又不以 `A`/`P` 开头 → 落到 f-string 解析器被原样输出。
    后果：`Convert-JmComicToCbz.ps1` 会把所有章节归到名为 `Bd_Atitle` 的系列下 → **静默污染库**。
    正确写法：`Bd/Atitle/Pname` / `Bd_Atitle_Pname` / `Bd / {Atitle} / {Pname}`。
    → 已修 `jmcomic/option.yml`，并用 `tests/test_dirrule.py` 钉死渲染结果。
    **教训：这类错误不报错、只产出错的目录结构 → 必须有渲染级回归测试，不能只测「配置能不能加载」。**

---

## 8. 维护成本

| 项 | 频率 | CLI 轨 | Skill 轨 | Web 轨 |
|---|---|---|---|---|
| 上游 jmcomic 版本跟进 + 实体字段变更适配 | 1–3 周一版 | 0.2 人日/月 | 0.2 | 0.2 |
| 站点域名 / 反爬变化 | 不定期 | 0.2 | 0.2 | 0.2 |
| skill 分派规则调优（agent 误分派） | 经常性 | — | 0.1 | — |
| 前端跟版 | 随 UI | — | — | 0.3 |
| **合计** | | **0.5** | **0.5** | **0.7** |

---

## 9. 验收标准

1. DSH 的 `skill` 工具能列出 `manga`（来源 `project-agents`，路径 `<repo>/.agents/skills/manga/SKILL.md`）。
2. 「下载 JM 404005」→ agent 分派到 `manga download`，先出计划再执行，返回 `save_path` 与失败明细。
3. 「把 JM 404005 入库」→ 自动跑 `CBZ 转换 → Invoke-LibraryIngest → Test-MangaLibrary`，最终校验退出码 0。
4. **红线断言**：灰色源的导出能力在 CLI 层不存在；`Export\Manga` 全流程后**零新增**（自动化断言）。
5. **不索要凭据**：agent 只提示设置 `${JM_USERNAME}` / `${JM_PASSWORD}`，不索取密码与 Cookie。
6. 写操作前有明确确认；同名冲突只跳过不覆盖。
7. 下载可取消，取消后返回已完成 / 未完成 ID 列表。
8. `manga pick-domain` 输出每个域名的可达性。
9. 单元测试 + 离线集成全绿；`JM_LIVE=1` 真实冒烟通过（1 本 2 话 → CBZ → 入库 → 校验退出码 0）。
10. `references/` 覆盖 option 全字段、21 插件 kwargs、DirRule 29 变量、采集源选路矩阵。

---

## 10. 明确假设

- 单人、熟悉 Python 与本仓库的工程师；换人 +20% 磨合。
- 口径：1 人日 = 8h 工作日；不含等待站点/网络、需求变更、上游版本迁移。
- 不含手机端 App、不含 `jm-view-server` 定制开发（按官方插件原样接入）、不含合规审查。
- 形态 C 的 token 成本按「每本子 10–30 次模型调用」估，超出则需加批处理作业。
