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
| 转换脚本报「没有找到图片目录」 | 源目录结构不符 | 看 `dir_rule.rule`；应为 `Bd_Atitle/Pname`（两层：`<base>\<本子>\<章节>\*.jpg`） |
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
| 沙箱内 `%TEMP%` 不可写 | pytest 已配 `--basetemp` 落在工作区；普通命令不受影响 |
| `uv` 报缓存权限错 | 设 `UV_CACHE_DIR=<仓库>\.uv-cache` |
| Python 建的临时目录里不能再建子目录 | Windows 下 `tempfile.mkdtemp` 会套受限权限；用普通 `mkdir` |

## 6. jmcomic 的硬性陷阱（会静默出错，最值得记）

1. **`all_success` 在用了 filter 后恒为 `False`** —— 别拿它当成功判据，用 `manifest` 自己统计。
2. **`do_filter` 必须返回可 `len()` 的对象**，空的时候返回 `[]`，**不能返回 `None`**。
3. **`check_exception=True` 只对单个 ID 生效** —— 批量要读 `BatchResult.failed`。
4. **文档默认值 ≠ 代码默认值**：`threading.image` 文档 30 / 代码 **20**；`dir_rule.rule` 文档 `Bd / Ptitle` / 代码 **`Bd_Pname`**；`client.impl` 文档示例 `html` / 实际默认 **`api`**。以本机 2.7.11 源码为准。
5. **`--dry-run` 会忽略 `--option`** —— 干跑看不出你的落点配置。
6. **`dir_rule.base_dir` 默认 = 当前工作目录** —— 不改的话图会落在你当时所在的目录。
7. **并发是乘数**：本子数 × `threading.photo` × `threading.image`，默认可达 320 线程 → 用 `JmSyncRuntime` 封顶。
8. **异步端不支持 `domain_retry_strategy`** —— 传了直接 `TypeError`。
9. **自定义异步插件**要 `enter_wait_list` + `wait_until_finish`，主程序退出前会调 `option.wait_all_plugins_finish()`。
10. **★ `dir_rule.rule` 的分隔符与写法不能混用**：`Bd_Atitle/Pname` 会渲染成 `<base>/Bd_Atitle/<章节名>/`
    （字面量目录 `Bd_Atitle`）→ 转换脚本会把所有章节归到名为 `Bd_Atitle` 的系列下。
    正确写法：`Bd/Atitle/Pname`、`Bd_Atitle_Pname`、`Bd / {Atitle} / {Pname}`。详见 [naming.md](naming.md)。
