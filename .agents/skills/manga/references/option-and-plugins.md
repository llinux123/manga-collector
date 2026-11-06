# option 配置与插件（何时读我：要改配置、加插件、排查插件不生效时）

> 本机配置：`jmcomic/option.yml`（已按实测环境生成并用 jmcomic 解析器验证过）
> 字段全量参考：`jmcomic/option.reference.yml`（本机 2.7.11 导出的快照）

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
  domain: []          # {html: [...], api: [...]}；留空=自动发现+轮询
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
    photo: 8          # 同时下章节数（默认 = CPU 核数）

dir_rule:
  base_dir: C:/Users/muxin/Documents/Library/00-Incoming/_raw_jmcomic
  rule: Bd_Atitle/Pname
  normalize_zh: null  # zh-cn / zh-tw / null

plugins:
  dependencies_strategy: failed-fast   # failed-fast / auto-install / ignore-only-log
  valid: log                          # ignore / log / raise
  after_init:
    - plugin: advanced_retry
      kwargs:
        retry_config: {retry_rounds: 3, retry_domain_max_times: 5}
```

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

### 21 个内置插件

| plugin_key | 事件 | 主要 kwargs | 依赖 |
|---|---|---|---|
| `login` | 任意 | `username` `password` `impl`（遗留，会写进 `auth`） | — |
| `usage_log` | after_init | `interval`(1) `enable_warning`(True) `warning_cpu_percent`(70) `warning_mem_percent`(70) `warning_thread_count`(100) | psutil |
| `download_progress` | after_init | `log_file`(`jmcomic-download.log`) `terminal_log_lines`(6) | rich |
| `find_update` | after_init | `{album_id: photo_id}` 只下新章 | — |
| `image_suffix_filter` | 任意 | `allowed_orig_suffix`（如 `['.gif']`） | — |
| `replace_path_string` | 任意 | `replace`（路径文本替换字典） | — |
| `client_proxy` | after_init | `proxy_client_key` `whitelist`（按 impl 分流代理）+ 客户端构造 kwargs | — |
| `auto_set_browser_cookies` | after_init | `browser`(chrome) `domain`；**站点校验设备，需同时配匹配的浏览器 headers** | browser_cookie3 |
| `jm_server` | after_init | `password` `base_dir` `run:{host,port,debug}` | jm-view-server |
| `subscribe_album_update` | after_init | `album_photo_dict` `email_notify` `download_if_has_update` `auto_update_after_download` | — |
| `download_cover` | before_album | `dir_rule`(必填) `size`（如 `_3x4`） | — |
| `zip` | after_album/after_photo | `delete_original_file` `filename_rule`(`Ptitle`) `suffix`(zip/7z) `zip_dir` `dir_rule` `encrypt:{impl,type,password}` `level`(已废弃) | pyzipper / py7zr |
| `delete_duplicated_files` | after_album | `limit`(必填, md5 出现次数) `delete_original_file` | — |
| `send_qq_email` | after_album | `msg_from` `msg_to` `password`(授权码) `title` `content`（前三项必填，缺了直接报错） | — |
| `calibre_metadata` | after_album | `dir_rule`(必填) `include_cover` `fields`（Dublin Core，如 `language: zh`） | jmcomic-calibre |
| `img2pdf` | after_photo/after_album | `pdf_dir` `filename_rule`(`Pid`) `dir_rule` `delete_original_file` `encrypt:{password}` | img2pdf / pikepdf |
| `long_img` | after_photo/after_album | `img_dir` `filename_rule`(`Pid`) `dir_rule` `delete_original_file` | Pillow |
| `skip_photo_with_few_images` | before_photo | `at_least_image_count`(必填，如 3 → 跳过韩漫公告章) | — |
| `favorite_folder_export` | main | `save_dir` `zip_enable` `zip_filepath` `zip_password` `delete_original_file` `max_retry`(2) | — |
| `log_topic_filter` | 任意 | `whitelist`（日志 topic 白名单） | — |
| `advanced_retry` | after_init | `retry_config:{retry_rounds, retry_domain_max_times}`（**必填 dict**） | — |

全量依赖：`pip install jmcomic[plugins]`。

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
