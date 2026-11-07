# 故障排查（何时读我：命令报错、下载不动、结果和预期不一致时）

## 1. 先分诊

```powershell
manga doctor      # PowerShell？脚本在不在？jmcomic 装没装？代理端口通不通？目录在不在？
```

`doctor` 的 `proxy` 一项显示代理**不可连接** → 90% 的「下载全部超时」都是这个原因。

## 2. 下载类

| 现象 | 原因 | 处理 |
|---|---|---|
| 全部超时 / 连不上 | 代理没开或端口不对 | 开 Clash Verge → 确认 `127.0.0.1:7897` 在监听 → 改 `jmcomic/option.yml` 的 `client.postman.meta_data.proxies` |
| 改了代理没生效 | 写在了 `postman` 外层 | **必须写在 `client.postman.meta_data.proxies`**；写外层会被**静默忽略** |
| 中途大量 `curl: (56) Connection closed abruptly` | 并发相对站点偏高 | `download.threading.image: 20 → 10`、`photo: 8 → 4`；日志里能看到 `[req.retry] 次数: [n/5]` |
| 下到一半断了 | 网络抖动 | **直接重跑同一条命令**（`download.cache: true` 生效，已下的不会重来） |
| 下到的是乱码图 | 解混淆没开 | `download.image.decode: true`（默认已开，**必须开**） |
| 图片后缀被改了 | 配了 `download.image.suffix` | 本项目铁律「只重打包不重编码」→ 保持 `suffix: null` |
| 域名全失效 | 站点换域名 | 启用 `advanced_retry` 插件；或 `JmModuleConfig.get_html_domain_all()` 动态取；或 `manga pick-domain` 测可达性 |
| 日志文件涨到几十 MB | `jmcomic-download.log` **固定写在当前工作目录** | 在固定目录跑命令；下完删掉。仓库根别跑 |
| 需要登录 | 敏感题材 / 收藏夹 / 签到 | `auth` 填账号密码（推荐）或 `AVS` Cookie；**两者不要同时配** |
| 配了登录还是没登录上 | Cookie 过期但库以为已登录 | 删掉 `auth.cookies`，只留账号密码 |
| 换了域名后 Cookie 失效 | Cookie **按域名区分** | 改用账号密码 |
| API 端发评论报错 | api 端不支持发评论 | 发评论只能 `impl: html` |

## 3. 入库 / 校验类

| 现象 | 原因 | 处理 |
|---|---|---|
| 校验器报 `EmptyVolume` 但包只有 1 页 | 休刊公告 / 新年贺这类**公告页** | 属误报；`-MinPages 1` 复核，或单独归类。**不要**为了过校验删文件 |
| 报 `NumberMismatch`（文件名编号 ≠ 元数据编号） | 元数据是旧的 | 重跑 `manga ingest --apply --generate-comicinfo`（已存在则不覆盖 → 需先删旧 `ComicInfo.xml` 或用 ComicTagger） |
| 报 `Duplicate` 但你知道不是重复 | **先看 `NumberMismatch`** | 同号同卷才是真重复；同号不同卷 = `Split`（告警） |
| 报 `Naming` | 文件名没话号前缀 | `manga ingest` 会自动改名；或按 §命名规范手改 |
| 报 `Metadata`（缺 ComicInfo） | 没生成元数据 | `manga ingest --apply --generate-comicinfo` |
| 报 `Layout`（图片在包内子目录） | 打包时没平铺 | 重打包时图片放**包根目录** |
| 转换脚本报「没有找到图片目录」 | 源目录结构不符 | 看 `dir_rule.rule`；应为 `Bd/Atitle/Pname`（三层：`<base>\<本子>\<章节>\*.jpg`）。⚠ **不是** `Bd_Atitle/Pname` —— 那样会生成字面量目录 `Bd_Atitle`，所有章节会被归到同一个系列下 |
| 入库后文件没动 | 忘了 `--apply` | 默认 dry-run。先干跑看计划，确认后加 `--apply` |
| 入库报冲突 | 同名目标已存在 | **正常**：策略是「跳过不覆盖」，源文件留在收件箱。确认是同一本再决定是否改名 |
| 校验器很慢 | 库在 SMB / 网络盘 | 库放本地 SSD；网络盘只当备份 |
| 改完文件名服务端还是旧标题 | 没重新扫描 / 没刷新元数据 | Komga/Kavita 手动 Scan；再 Refresh metadata / Analyze |

## 4. CLI / 信封类

| exit | 含义 | 处理 |
|---|---|---|
| 0 | 成功 | 看 `data`；`warnings` 非空要转述 |
| 1 | 用法错 | 照 `error.message` 改参数 |
| 2 | 执行错误 | `error.hint` 里有脚本输出尾部 |
| 3 | 需要凭据 | 见 SKILL.md §4 |
| 4 | 资源不存在 | 跑 `manga doctor`；或 `--scripts-dir` 指错 |
| 6 | 红线拒绝 | **停下报告，不要绕过** |

## 5. 环境类

| 现象 | 处理 |
|---|---|
| `找不到脚本：X.ps1` | `--scripts-dir` 或环境变量 `MANGA_SCRIPTS_DIR` 指向仓库 `scripts/`；`Convert-JmComicToCbz.ps1` 在 `jmcomic/` 下，CLI 会自动找 |
| 控制台中文乱码 | 脚本必须是 UTF-8 **with BOM**；PowerShell 7（pwsh）默认正常 |
| 沙箱内 pytest 临时目录不可写 | 本项目**不用** pytest 的 `tmp_path`/`--basetemp`：Windows 上 `\\?\` 扩展路径前缀沙箱不认，`tempfile.mkdtemp` 又会给目录套受限权限 → 改用 `tests/conftest.py` 的 `workdir` fixture（工作区内普通 `mkdir`）。普通命令不受影响 |
| `uv` 报缓存权限错 | 设 `UV_CACHE_DIR=<仓库>\.uv-cache` |
| Python 建的临时目录里不能再建子目录 | Windows 下 `tempfile.mkdtemp` 会套受限权限；用普通 `mkdir` |

## 5b. 依赖与登录态（P4 加固后新增）

`manga doctor` 现在会多报三块：`jmcomic_version`（含版本下限 `2.7.11`）、`auth`（登录态）、`dependencies`（缺哪些可选依赖）。

| 现象 | 处理 |
|---|---|
| `DEP_MISSING`：导出缺少 `img2pdf` | 退出码 **4**（缺东西，不是执行错）。`uv pip install img2pdf`（`--format longimg` 缺的是 `pillow`；加密 zip 缺 `pyzipper`/`py7zr`；加密 pdf 缺 `pikepdf`）。**CLI 不会自动装**，它只给命令 |
| `TOOL_MISSING`：找不到 `gallery-dl` / `mangadex-dl` | 退出码 **4**（同上：缺东西）。hint 里给安装命令（`uv tool install gallery-dl` / `scoop install gallery-dl`）。**CLI 不会自动装** |
| `ENV_PYINSTALLER`：gallery-dl 报 `Failed to create parent directory structure` | 不是 URL 问题，是 **PyInstaller 版 gallery-dl 要往临时目录解压运行时**，而受限环境的临时目录不可写。本适配层已把 `TEMP`/`TMP` 指到可写目录；仍失败就换非 PyInstaller 版（`uv tool install gallery-dl`） |
| `auth.logged_in = false` 但有 `has_username`/`has_password` | 那只是**凭据**不是登录态 —— 下载时 jmcomic 才会去登录。失败点会出现在下载阶段（退出码 3），不在配置阶段 |
| `auth.logged_in = false` 且有 cookies | cookies 在，但缺**用户标识**：`api` 客户端要 `auth.user_id`，`html` 客户端要 `auth.username` |
| `doctor` 报 `jmcomic` 版本低于下限 | option 新字段会被**静默忽略**（表现为「配了不生效」）→ 升级 jmcomic |
| 干跑里 `plan[].degraded = true` | 取不到元数据（站点抽风 / 代理不通 / 本子被删）。干跑**不会**因此失败，已降级为最小计划；但 `warnings` 里会提示，`--apply` 前先确认 |

## 5b-2. 校验闸门是**整库**的

`manga verify` 扫的是**整个权威库**：库里只要有一条 error，退出码就是 2。
存量库（上千个 CBZ、几千条历史问题）永远拿不到 0 —— **这不是你刚入库那本的错**。

**增量验收**用 `--series`：

```powershell
manga verify --series 幸福督市            # 只验这一个系列
manga verify --series "18comic/繼母與繼姐"  # 嵌套系列用相对路径
manga verify --series 18comic              # 来源层会自动摊平成叶子系列
```

- `--series` 会把选中的系列**复制**到临时影子库根再验，**原库一个字节都不动**，验完即删；
- 之所以要复制：校验脚本只认「库根/系列/文件.cbz」，把库根指向系列目录会让文件落在
  库根下 → 被判成「文件直接位于库根目录」→ 退出码 2；
- `manga verify` 的输出里有 `series_available`：库里所有直接装着 CBZ 的目录（相对路径），
  agent 据此选，不用猜；
- 退出码 2 且问题都在**别的**系列里 → 说明那是你要单独处理的历史问题，不是这次的锅。

## 5c. 日志落在哪

`manga download` / `manga export` 在调用 jmcomic **之前**会把工作目录切到
`<书库根>\Manga\Metadata\logs` → jmcomic 的进度日志（`jmcomic-download.log`，相对路径）
跟着落在这里，**不会**掉进仓库根。

## 6. jmcomic 的硬性陷阱（会静默出错，最值得记）

1. **`all_success` 在用了 filter 后恒为 `False`** —— 别拿它当成功判据，用 `manifest` 自己统计。
2. **`do_filter` 必须返回可 `len()` 的对象**，空的时候返回 `[]`，**不能返回 `None`**。
3. **`check_exception=True` 只对单个 ID 生效** —— 批量要读 `BatchResult.failed`。
4. **文档默认值 ≠ 代码默认值**：`threading.image` 文档 30 / 代码 **20**；`dir_rule.rule` 文档 `Bd / Ptitle` / 代码 **`Bd_Pname`**；`client.impl` 文档示例 `html` / 实际默认 **`api`**。以本机 2.7.11 源码为准。
5. **`--dry-run` 会忽略 `--option`** —— 干跑看不出你的落点配置。
6. **`dir_rule.base_dir` 默认 = 当前工作目录** —— 不改的话图会落在你当时所在的目录。
7. **并发是乘数**：2.7.11 的经典路径是**每层一个独立临时池**（`JmTempRuntime(workers=count_batch)`）——
   章节层 = `threading.photo`(默认 24)，图片层 = `threading.image`(默认 20)，批量层再加「每个 ID 一个线程」。
   所以单个本子最坏情况是 **24 × 20 = 480 个并发图片下载**，批量再乘本子数 → 用 `JmSyncRuntime` 封顶。
8. **异步端不支持 `domain_retry_strategy`** —— 传了直接 `TypeError`。
9. **★ `wait_all_plugins_finish()` 上游只定义、**不自动调用**【源码】：`JmOption.wait_all_plugins_finish()`
   存在（`jm_option.py:1050`），但 2.7.11 里**没有任何调用点** —— 自定义异步插件要自己
   `enter_wait_list` + `wait_until_finish`，并由**调用方**在退出前显式调 `option.wait_all_plugins_finish()`
   （本项目的 CLI 还没接这一步，见 P4 待办）。
10. **★ `dir_rule.rule` 的分隔符与写法不能混用**：`Bd_Atitle/Pname` 会渲染成 `<base>/Bd_Atitle/<章节名>/`
    （字面量目录 `Bd_Atitle`）→ 转换脚本会把所有章节归到名为 `Bd_Atitle` 的系列下。
    正确写法：`Bd/Atitle/Pname`、`Bd_Atitle_Pname`、`Bd / {Atitle} / {Pname}`。详见 [naming.md](naming.md)。
11. **★ jmcomic 的日志默认打在 `stdout`**（`jm_config.setup_default_jm_logger`）——
    一条 `[2026-11-07 10:24:52] ...` 就能让 JSON 解析失败。
    本项目的 CLI 已把 jmcomic 日志改道到 stderr（`runtime.route_jm_logs_to_stderr`）；
    自己写脚本调用 jmcomic 时必须自己做这层隔离。
12. **★ `album.episode_list` 是元组不是对象**：`(photo_id, photo_index, photo_title, photo_pub_date)`。
    按 `item.photo_id` 取会抛 `AttributeError` → 被 `_safe` 吞成 `None` →
    「话数对得上、每话字段全空」的假成功。
13. **★ 下载清单在 `downloader.manifest_dict`（按实体索引的 dict）**，**没有** `downloader.manifest` 属性。
    取错属性 → 「下载成功但 0 张图」的假成功（图片其实已在磁盘上）。
    统计下载结果一律自己数 `manifest.image_filepath_list`。
14. **入库脚本的 `-BackupRoot` 默认是 `./backup`（CWD 相对）、`-LogPath` 默认是「脚本目录\logs」**：
    前者会把备份写进你当时所在的目录（仓库根！），后者在工作区外可能写不动 →
    脚本退出码 1、文件没动。本项目的 CLI 已显式传这两个参数。
