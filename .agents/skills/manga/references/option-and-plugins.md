# option 配置与插件（何时读我：要改配置、加插件、排查插件不生效时）

> 本机配置：`jmcomic/option.yml`（已按实测环境生成并用 jmcomic 解析器验证过）
> 字段全量参考：`jmcomic/option.reference.yml`（本机 2.7.11 导出的快照）
>
> **校对日期：2026-11-07 · 对标 jmcomic 2.7.11**（默认值取自 `JmOption.default_dict()`，
> 插件形参取自 `inspect.signature`，不是照文档抄的）。
> 重新校准：`uv run python tools/jmcomic_facts.py check`。

## 1. option 结构总览

```yaml
version: '2.1'        # 配置格式版本（用于旧版兼容迁移）
log: true             # true / false / 'pretty'（彩色，Windows 自动开 VT100）

auth:                 # 登录（只认账号密码 或 只认 cookies，不要同时配）
  username: ${JM_USERNAME}
  password: ${JM_PASSWORD}
  # cookies: {AVS: '...'}      # 仅 impl: html，且必须来自实际访问的域名
  # user_id: null              # 运行期写回，to_file() 不持久化

client:
  impl: api           # api=APP端（不限IP，稳定）/ html=网页端（高效但限IP地区）
  async_impl: null    # null=跟随 impl；可显式 async_api / async_html。配了不会自动开异步
  domain: []          # 留空=自动发现 + 轮询
  retry_times: 5
  cache: true         # null/false=关 | true|level_option=option级共享 | level_client=每client独立
  postman:
    type: curl_cffi
    meta_data:        # ★ proxies / impersonate / headers 必须在这一层
      impersonate: chrome
      headers: null
      proxies: 127.0.0.1:7897   # system / clash / v2ray / host:port / {http:,https:} / null

download:
  cache: true         # 磁盘已有文件不重复下载（断点续传）
  image:
    decode: true      # ★ 必须开：还原站点的混淆图
    suffix: null      # null=保持原格式不重编码；设 .jpg 则统一转格式
  threading:
    image: 20         # 同时下图片数（站点单次上限 50）
    photo: 8          # 同时下章节数（**上游默认 24**，偏激进，8 更稳）

dir_rule:
  base_dir: C:/Users/muxin/Documents/Library/00-Incoming/_raw_jmcomic
  rule: Bd/Atitle/Pname   # ★ 上游默认是 Bd_Pname；本项目锁 Bd/Atitle/Pname（见 naming.md）
  normalize_zh: null  # zh-cn / zh-tw / null

plugins:
  dependencies_strategy: failed-fast   # failed-fast / auto-install / ignore-only-log
  valid: log                          # ignore / log / raise
  after_init:
    - plugin: advanced_retry
      kwargs:
        retry_config: {retry_rounds: 3, retry_domain_max_times: 5}
```

**上游默认值（`JmOption.default_dict()` 实测，2026-11-07）**

| 字段 | 默认值 | 备注 |
|---|---|---|
| `dir_rule.rule` | `Bd_Pname` | **不是** `Bd / Ptitle`（那是文档里的例子） |
| `dir_rule.base_dir` | **当前工作目录** | 不写就把图丢进你当时所在的项目目录 |
| `download.threading.image` | 20 | 文档写 30，**代码是 20** |
| `download.threading.photo` | **24** | 不是 CPU 核数 |
| `download.image.decode` | true | 迁移旧配置时别改掉 |
| `download.image.suffix` | null | 保持原格式（webp 就还是 webp） |
| `client.impl` | api | 文档示例写 html，**实际默认 api** |
| `client.cache` | null | 真要缓存得显式写 true / level_option / level_client |
| `client.retry_times` | 5 | |
| `client.postman.type` | curl_cffi | |
| `client.postman.meta_data.impersonate` | chrome | |
| `plugins.valid` | log | |
| `plugins.dependencies_strategy` | failed-fast | 缺依赖直接启动失败 |

> ⚠ `client.postman.meta_data.proxies` 的默认值会**跟着系统代理走** ——
> 本机装了 Clash 系统代理时，`default_dict()` 里就是 `127.0.0.1:7897`。
> 想固定行为就显式写死端口。

**三个最常踩的位置错误**

1. `proxies` 必须写在 `client.postman.meta_data.proxies`，写外层**静默忽略**；
2. `dir_rule.base_dir` 默认 = **当前工作目录**，不写会把图丢进你当时所在的项目目录；
3. `download.image.decode` 必须 `true`，否则下到的是混淆图。

## 2. 插件机制

### 钩子（事件）

| 事件 | 何时 |
|---|---|
| `after_init` | option 对象创建后 |
| `before_album` / `after_album` | 下载本子前 / 后 |
| `before_photo` / `after_photo` | 下载章节前 / 后 |
| `before_image` / `after_image` | 下载图片前 / 后 |
| `main` | 一次性任务（收藏夹导出） |
| `after_download` | 整批下载结束后（workflow 脚本用） |

**自定义事件名也允许**（`option.call_all_plugin('my_event')`）。

### 插件条目结构

```yaml
plugins:
  after_album:
    - plugin: zip            # 必填：已注册的 plugin_key
      kwargs: {...}          # 插件参数；支持 ${ENV}
      log: false             # 可选：屏蔽该插件日志
      safe: true             # 可选：吞掉异常（默认 true）
      valid: log             # 可选：覆盖 plugins.valid
```

### 21 个内置插件（**源码校对版**）

> **校对日期：2026-11-07 · 对标 jmcomic 2.7.11 安装包源码**（`JmModuleConfig.REGISTRY_PLUGIN`）。
> 重新校准：`uv run python tools/jmcomic_facts.py plugins`；
> 漂移检查：`uv run python tools/jmcomic_facts.py check`（`tests/test_reference_facts.py` 也在跑它）。
>
> 表里的形参是 `invoke()` 的**真实签名**（`inspect.signature` 取的，不是照文档抄的）。
> `album` / `photo` / `downloader` / `image` 是**框架注入**的 —— 由钩子调用方传，
> 你在 YAML 里写它们没用。

| plugin_key | 类 | invoke 形参（源码校对） | 依赖 | 建议挂载事件 |
|---|---|---|---|---|
| `advanced_retry` | AdvancedRetryPlugin | `retry_config`（**必填** dict），另收 `**kwargs` | — | after_init |
| `auto_set_browser_cookies` | AutoSetBrowserCookiesPlugin | `browser`（必填）, `domain`（必填） | browser_cookie3 | after_init |
| `calibre_metadata` | CalibreMetadataPlugin | `dir_rule`（**必填**）, `include_cover=False`, `fields=None`，另收 `**kwargs` | jmcomic-calibre | after_album |
| `client_proxy` | ClientProxyPlugin | `proxy_client_key`（必填）, `whitelist=None`，另收 `**kwargs`（→ 代理客户端构造器） | — | after_init |
| `delete_duplicated_files` | DeleteDuplicatedFilesPlugin | `limit`（**必填**，md5 出现次数）, `delete_original_file=True`，另收 `**kwargs` | — | after_album |
| `download_cover` | DownloadCoverPlugin | `dir_rule`（**必填**）, `size=''`，另收 `**kwargs` | — | after_album |
| `download_progress` | DownloadProgressPlugin | `log_file='jmcomic-download.log'`, `terminal_log_lines=6` | rich | after_init |
| `favorite_folder_export` | FavoriteFolderExportPlugin | `save_dir=None`, `zip_enable=False`, `zip_filepath=None`, `zip_password=None`, `delete_original_file=False`, `max_retry=2` | — | main |
| `find_update` | FindUpdatePlugin | （无显式参数），另收 `**kwargs` —— **kwargs 就是 `{album_id: photo_id}` 字典** | — | after_init |
| `image_suffix_filter` | ImageSuffixFilterPlugin | `allowed_orig_suffix=None` | — | 任意 |
| `img2pdf` | Img2pdfPlugin | `pdf_dir=None`, `filename_rule='Pid'`, `dir_rule=None`, `delete_original_file=False`, `encrypt=None`，另收 `**kwargs` | img2pdf（加密 pdf 另需 pikepdf） | after_album / after_photo |
| `jm_server` | JmServerPlugin | `password=''`, `base_dir=None`, `run=None`，另收 `**kwargs` | jm-view-server（**源码未声明 `plugin_dependencies`，运行时才 import**） | after_init |
| `log_topic_filter` | LogTopicFilterPlugin | `whitelist`（必填） | — | 任意 |
| `login` | JmLoginPlugin | `username=None`, `password=None`, `impl=None` | — | 任意 |
| `long_img` | LongImgPlugin | `img_dir=None`, `filename_rule='Pid'`, `delete_original_file=False`, `dir_rule=None`，另收 `**kwargs` | Pillow（**隐式**：`plugin_dependencies` 是空的，但 invoke 里 `from PIL import Image`） | after_album / after_photo |
| `replace_path_string` | ReplacePathStringPlugin | `replace`（必填，路径文本替换字典） | — | 任意 |
| `send_qq_email` | SendQQEmailPlugin | `msg_from`（必填）, `msg_to`（必填）, `password`（必填，授权码）, `title`（必填）, `content`（必填） | — | after_album |
| `skip_photo_with_few_images` | SkipPhotoWithFewImagesPlugin | `at_least_image_count`（**必填**，如 3 → 跳过韩漫公告章），另收 `**kwargs` | — | before_photo |
| `subscribe_album_update` | SubscribeAlbumUpdatePlugin | `album_photo_dict=None`, `email_notify=None`, `download_if_has_update=True`, `auto_update_after_download=True` | — | after_init |
| `usage_log` | UsageLogPlugin | （无显式参数），另收 `**kwargs` —— **kwargs 透传给监控线程：`interval=1` `enable_warning=True` `warning_cpu_percent=70` `warning_mem_percent=70` `warning_thread_count=100`** | psutil | after_init |
| `zip` | ZipPlugin | `downloader`（必填）, `delete_original_file=False`, `level=None`, `filename_rule='Ptitle'`, `suffix='zip'`, `zip_dir='./'`, `dir_rule=None`, `encrypt=None` | **动态**：不加密用标准库 `zipfile`；加密 → pyzipper；7z → py7zr | after_album / after_photo |

**全量依赖**：`pip install jmcomic[plugins]`。

**校对时抓到的三个坑**（文档与源码曾经对不上）：

1. `zip` 的 `plugin_dependencies` 是**空的** —— 未加密走标准库，只有 `encrypt` 才要 pyzipper/py7zr；
   `required_dependencies_for(kwargs)` 才是正确口径（见 `ZipPlugin`）。
2. `long_img` 的依赖 Pillow 是**隐式**的（invoke 里 import），`plugin_dependencies` 没声明 ——
   依赖探测要单独算，否则会以为「没缺依赖」。
3. `jm_server` 同理：`jm-view-server` 是运行时 import 的，源码里没声明。

### 依赖策略

| `plugins.dependencies_strategy` | 行为 |
|---|---|
| `failed-fast`（默认） | 缺依赖 → 启动即失败并给安装指引 |
| `auto-install` | 静默 `pip install` |
| `ignore-only-log` | 只 warn，插件可能静默跳过 |

**插件参数校验异常**的处理由 `plugins.valid` 决定：`ignore` / `log`（默认）/ `raise`。

### ZIP 加密（v2.6.0+）

```yaml
encrypt:
  impl: '7z'      # 7z = 头部加密（隐私最好，无注释）
  type: 'password' # 或 'random'（48 位随机，打印在日志里，并写进 ZIP 注释）
  password: 'xxx'
```

`type` 与 `password` 二选一，必填其一。

### 自定义插件

```python
from jmcomic import JmOptionPlugin, JmModuleConfig

class MyPlugin(JmOptionPlugin):
    plugin_key = 'myplugin'
    def invoke(self, word) -> None:
        print(word)

JmModuleConfig.register_plugin(MyPlugin)
```

YAML 里用 `plugin: myplugin` + `kwargs: {word: hello}` 引用。

异步插件要优雅停机：

```python
class MyAsyncPlugin(JmOptionPlugin):
    plugin_key = 'my_async_plugin'
    def invoke(self, **kwargs) -> None:
        self.enter_wait_list()      # 主线程退出前会等我
        self.is_running = True
        self.thread = threading.Thread(target=self.work); self.thread.start()
    def wait_until_finish(self):
        self.is_running = False
        if self.thread.is_alive(): self.thread.join()
```

主程序退出前会调 `option.wait_all_plugins_finish()` 遍历挂起的插件。

### 插件基类可用工具

`self.log(msg, topic)` · `self.require_param(case, msg)` · `self.warning_lib_not_install(lib, throw)` ·
`self.execute_deletion(paths)`（不删非空目录）· `self.execute_cmd(cmd)` / `execute_multi_line_cmd(cmd)` ·
`self.decide_filepath(album, photo, filename_rule, suffix, base_dir, dir_rule_dict)` ·
`self.jm_task_context`（当前任务上下文快照）。

## 3. 日志

```yaml
log: true          # true / false / 'pretty'（ANSI 彩色）
```

- `jmcomic.disable_jm_log()` 彻底关日志。
- `log_topic_filter` 插件按 topic 白名单过滤；或自定义 `logging.Filter` 按 `record.topic`。
- 插件条目上 `log: false` 屏蔽单个插件日志。
- `jm_task_context(task_id='task-1')` 给并发下载打标；日志行会带 `[task_id=...; album=...]`。
- 收集任务日志：自定义 `logging.Handler` 读 `record.jm_task_context['task_id']`。
- 重定向到文件：`jm_logger.handlers.clear()` + `logging.FileHandler('jm_download.log', encoding='utf-8')`。
- 完全接管：`JmModuleConfig.EXECUTOR_LOG = custom_log(topic, msg, error)`。
- **进度插件会把完整日志写到 `jmcomic-download.log`（固定在当前工作目录）** → 别在仓库根跑。

## 4. 旧配置迁移

`JmOption.compatible_with_old_versions()` 自动处理：

| 旧 | 新 |
|---|---|
| `download.threading.batch_count` | `download.threading.image` |
| `plugin:`（单数） | `plugins:` |
| zip 插件的 `level: album/photo` | 改为挂在 `after_album` / `after_photo` 事件 |
| `client.postman.meta_data.cookies` | `auth.cookies` |
| `login` 插件 | `auth.username` / `auth.password` |
