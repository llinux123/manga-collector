# jmcomic 能力参考（何时读我：要调 jmcomic 的具体 API 时）

> 核对版本：**2.7.11**（本机 `.venv` 安装包）。
> 官方文档：<https://jmcomic.readthedocs.io/zh-cn/latest/>
> **文档只渲染带 docstring 的成员，源码里还有更多**；本文以源码为准。
>
> **校对日期：2026-11-07**

## 证据等级（每节都标了，别把推断当事实用）

| 标记 | 含义 |
|---|---|
| 【实测】 | 本机 2.7.11 **真跑过**，有输出证据（本项目 2026-11-07 冒烟） |
| 【源码】 | 读 2.7.11 安装包源码确认（位置随文给出） |
| 【文档】 | 来自官方文档 / 上游 docstring，**本机未验证** —— 用之前先小规模试 |

> ⚠ **【文档】级别的段落不要直接写进自动化**：html 端、收藏夹、签到、异步这几块
> 本机**没有实测过**（灰色来源 + 未配置登录态），真要用先拿一个 ID 试。

## 0. 平台能力差异（最容易踩） 【文档】

| 能力 | `impl: api`（APP 端，默认） | `impl: html`（网页端） |
|---|---|---|
| IP 地区限制 | 无 | **有**（`Restricted Access!` / 403） |
| `sub_category`（副分类） | 不支持 | 支持 |
| `category`（高级搜索） | `search` 不支持 | 支持 |
| 评论总数 / 页数 | 有 | **为 `None`** |
| 发评论 `album_comment` | **NotImplementedError** | 支持 |
| 浏览器 Cookie 登录 | 无效 | 有效 |
| 域名 | API 域名池（自动更新） | 发布页抓取 + 永久网域跳转 |

**结论**：默认 `api` 更稳但功能少；需要高级搜索或发评论才切 `html`，且要能接受 IP 地区限制。

【源码】`impl` 的**代码默认值是 `api`**（`JmModuleConfig.DEFAULT_CLIENT_IMPL`），
官方文档示例里写的 `html` 只是例子。

## 1. 入口 API

```python
from jmcomic import *

option = create_option_by_file('op.yml')      # 从文件
option = create_option_by_env('JM_OPTION_PATH')# 从环境变量
option = create_option_by_str(yaml_text)       # 从字符串
option = JmOption.default()                    # 默认

download_album(album_id, option=None, downloader=None, *,
               check_exception=True, extra=None, control=None, login=True)
download_photo(photo_id, ...)                  # 同上
# 批量：传 list / 迭代器 → BatchResult
```

- `extra=` 挂 Feature（导出）；`control=` 挂取消控制器；`login=False` 跳过自动登录。
- `check_exception=True` **只对单个 ID 生效**（上抛 `PartialDownloadFailedException`）；批量查 `BatchResult.failed`。

## 2. 结果对象

```python
result = download_album('123')
result.detail        # JmAlbumDetail
result.downloader    # JmDownloader
result.manifest      # DownloadManifest ← 是 property：downloader.manifest_dict[detail]
result.duration      # 总耗时（秒）
album, dler = result # 可解包（DownloadResult 是 NamedTuple）

batch = download_album(['123','456'])
batch.failed         # {album_id: Exception}
batch.all_succeeded  # bool
batch.total          # succeeded + failed
for r in batch: ...  # 只遍历成功项（succeeded 是 list）
batch['123']; batch.get(aid); aid in batch

manifest.image_filepath_list              # 所有图片路径（含命中缓存的）
manifest.get_export_filepath_list('pdf')  # 导出物（后缀可省点）
```

【源码】`BatchResult` 的真实成员：`succeeded`(list) / `failed`(dict) / `all_succeeded` / `total`
/ `__iter__` / `__getitem__` / `get` / `__contains__`（`jm_downloader.py:462`）。

> ⚠ **★ 批量场景的 manifest 陷阱**（本项目踩过，差点报「下载成功但 0 张图」）：
>
> - `result.manifest` ✅ 存在 —— 它是 property，指向 `downloader.manifest_dict[detail]`；
> - 但 `for album, dler in batch` 遍历出来的 **`dler` 是 downloader，没有 `.manifest` 属性**，
>   清单在 **`downloader.manifest_dict`**（按实体索引的 dict）里。
> - 统计下载结果要**自己合并** `manifest_dict` 里各实体的 `image_filepath_list`，
>   别拿 `all_success` 当成功判据。

## 3. 检索（5 个维度） 【文档】

```python
client = option.new_jm_client()
page = client.search_site(search_query='+MANA +无修正', page=1)  # +包含 -排除 空格AND
page = client.search_tag('無修正', page=1)
page = client.search_author('作者名', page=1)
page = client.search_work('作品名', page=1)
page = client.search_actor('角色名', page=1)

page = client.categories_filter(page=1, time=JmMagicConstants.TIME_ALL,
                                category=JmMagicConstants.CATEGORY_ALL,
                                order_by=JmMagicConstants.ORDER_BY_VIEW)
page = client.month_ranking(1); page = client.week_ranking(1); page = client.day_ranking(1)

# 翻页生成器 + 动态改条件
gen = client.categories_filter_gen(page=1, time=JmMagicConstants.TIME_WEEK)
page = next(gen)
page = gen.send({'order_by': JmMagicConstants.ORDER_BY_LATEST})
```

结果容器 `JmSearchPage`（`JmCategoryPage` 是别名）：`total` / `page_size`(80) / `page_count` /
`iter_id_title()` / `iter_id_title_tag()` / `single_album`。

【实测】`client.search_site(term, page)` 走 api impl **可用**：一次调用返回 80 条 `id/title`；
`iter_id_title()` 遍历正常。`search_site` 的默认排序是 `order_by='mr'`（最新入库，
`JmMagicConstants.ORDER_BY_LATEST`）。

## 4. 详情与评论

```python
album = client.get_album_detail('427413')
photo = client.get_photo_detail(photo_id, fetch_album=False)
client.download_album_cover('427413', './cover.png')   # 后缀可 jpg/webp/png
client.download_by_image_detail(image, './a.jpg')
client.download_image(url, './a.jpg', scramble_id=None, decode_image=True)

page = client.album_pagination('123456')       # 本子评论（含回评树）
for c in page: c.content / c.nickname / c.likes / c.is_spoiler / c.replies
for page in client.album_pagination_gen('123456'): ...
page = client.forum_pagination(page=1)         # 全站评论
for page in client.forum_pagination_gen(page=1): ...
client.album_comment(video_id, comment)         # 发评论（仅 html）【文档】
```

实体三层字段（UI 直接用）：

- `JmAlbumDetail`：`album_id` `series_id` `name` `description` `page_count` `pub_date` `update_date`
  `likes` `views` `comment_count` `works` `actors` `authors` `tags` `is_favorite` `liked`
  `episode_list` `related_list`；派生 `oname`（剥离汉化组等标签后的原名）`authoroname`（`【作者】原名`）
  `idoname`（`[ID] 原名`）。
- `JmPhotoDetail`：`photo_id` `name` `sort` `page_arr`（图片文件名列表）`data_original_domain`
  `data_original_0` `from_album`；派生 `album_index`（从 1 开始）`indextitle`（`第N話 名称`）`tags`。
- `JmImageDetail`：`img_url` `img_file_name` `img_file_suffix` `index`；派生 `download_url`（带 `?v=` 缓存击穿参数）
  `is_gif`（动图不解码不转码）。

> ⚠ **★ `episode_list` 是元组，不是对象**【实测】
>
> `JmAlbumDetail.episode_list` 的元素是 **`(photo_id, photo_index, photo_title, photo_pub_date)`**
> 四元组（`jm_entity.py:507`，去重后按 index 排序）。
> 按 `item.photo_id` 取会抛 `AttributeError` —— 被容错代码吞掉后就是
> 「话数对得上、每话字段全空」的假成功。取法：`photo_id, index, title, pub_date = item`。

## 5. 下载：过滤 / 取消 / 并发 【文档】

### 过滤（`do_filter`）

```python
class MyDownloader(JmDownloader):
    def do_filter(self, detail):
        if detail.is_photo():
            return detail[:3]      # 只下前 3 张图
        return detail
MyDownloader.use()                 # 新写法（旧的：JmModuleConfig.CLASS_DOWNLOADER = MyDownloader）
```

- 三级：album 层收到 `JmAlbumDetail`（可迭代出 photo），photo 层收到 `JmPhotoDetail`（可迭代出 image）。
- 返回值必须支持 `len()`；**过滤后为空返回 `[]`，不能返回 `None`**。
- **用了 filter 后 `downloader.all_success` 恒为 `False`**。
  【源码】`all_success` 的 docstring 原话：「如果使用了 filter 机制，例如通过 filter 只下载3张图片，
  那么 all_success 也会为 False」；实现是逐层比对 `len(album) != len(photo_dict)`（`jm_downloader.py:185`）。

配置式过滤器（写 option，不用写代码）：`image_suffix_filter`（`allowed_orig_suffix`）、
`find_update`（`{album_id: photo_id}` 只下新章）、`skip_photo_with_few_images`（`at_least_image_count`）。

### 取消 【文档】

```python
from jmcomic import DownloadControl, jm_task_context, DownloadCancelledException
ctrl = DownloadControl()
with jm_task_context(control=ctrl, task_id='task-001'):
    download_album(['123456','789012'])
ctrl.cancel('用户取消')
# DownloadCancelledException: .reason / .completed_results / .unfinished_ids
```

### 并发（三层） 【源码】

```python
from jmcomic import JmSyncRuntime
with JmSyncRuntime(album_workers=2, photo_workers=3, image_workers=8):
    download_album(['123','456'])
```

- 经典路径（不挂 Runtime）是**每层一个独立临时池**：`JmTempRuntime(workers=count_batch)`，
  章节层 = `threading.photo`（默认 24），图片层 = `threading.image`（默认 20）；
  批量层再加「**每个 ID 一个线程**」（`download_batch` 的 docstring 原话）。
  → 单个本子最坏 24 × 20 = **480 个并发图片下载**，批量再乘本子数。
- 所以要封顶就得显式挂 `JmSyncRuntime`（`album_workers`/`photo_workers`/`image_workers`）。
- `JmAsyncRuntime(decode_workers=N)` 只管**解密**线程池（网络 IO 是协程）。
- 外传 `ThreadPoolExecutor` 给 Runtime 时，**Runtime 不会关它**（谁创建谁关）。
- 【源码】批量入口是 `download_batch(download_api, jm_id_iter, ...)`；它**逐项捕获异常**
  放进 `result.failed`，**不会**因为 `check_exception=True` 整体抛 → 批量查 `failed` 字典。

### 任务上下文 【文档】

`jm_task_context(option=..., runtime=..., control=..., task_id=..., session_id=..., user_id=...)`
用 `ContextVar` 跨线程/协程传播，日志行会带 `[task_id=...; album=...]`。

## 6. 导出 Feature

```python
from jmcomic import download_album, Feature
download_album('123', option, extra=Feature.export_pdf)
download_album('123', option, extra=Feature.export_pdf + Feature.export_zip)   # + | & 或 list
download_album('123', option, extra=Feature.export_zip(zip_dir='D:/zips',
                                 filename_rule='Atitle', delete_original_file=True))
```

- 三个内置：`export_pdf`（img2pdf，可 pikepdf 加密）、`export_zip`（zip/7z，可加密）、`export_long_img`（PNG 长图）。
- `filename_rule`：`A` 前缀 = 本子字段，`P` 前缀 = 章节字段；**`download_album` 只能用 `Axxx`**，用 `Pxxx` 会报错。
- 时机自适应：`should_invoke` 按 `download_type` 选 `after_album` / `after_photo`。
- 默认输出目录 = `option.dir_rule.base_dir`；默认文件名 `[JM{Aid}]{Atitle}` / `[JM{Pid}]{Ptitle}`。
- 自定义 Feature：继承 `Feature`，实现 `invoke(option, **kwargs)`，可覆写 `should_invoke(when)`。

【源码】`Feature.export_pdf/export_zip/export_long_img` 三个 Feature 分别包
`Img2pdfPlugin` / `ZipPlugin` / `LongImgPlugin`（`jm_feature.py:172`），
输出目录由 `_adapt_plugin_kwargs` 自动指向 `option.dir_rule.base_dir`。
**依赖**：pdf→img2pdf（加密再+pikepdf）、zip→未加密用标准库 `zipfile`、长图→Pillow（隐式依赖）。

## 7. 登录 / 收藏 / 签到

```python
option.login_with_option_auth()        # 全局，幂等；写回 auth.cookies + auth.user_id
option.login_with_option_auth_async()
client.login('user', 'pass')            # 只影响该 client，不写回 option.auth

client.favorite_folder(page=1, order_by=JmMagicConstants.ORDER_FF_FAVORITE_TIME, folder_id='0')
for page in client.favorite_folder_gen(): ...
client.add_favorite_album('438696', folder_id='0', rollback_when_mismatch=True)
client.delete_favorite_album('438696', folder_id='0', rollback_when_mismatch=True)
# ⚠ 2.7.11 **没有** toggle_favorite_album —— 收藏/取消是两个独立方法

client.get_daily()                      # 打卡日历
r = client.daily_checkin()              # r.code: 0 成功 / 1 已签到
```

**登录状态判定**（两条都满足才算已登录）：`auth.cookies` 非空 **且** 用户标识非空
（html 看 `auth.username`，api 看 `auth.user_id`）。
【源码】`JmOption._is_auth_logged_in()`（`jm_option.py:601`）就是这个口径。
`new_jm_client()` **从不**登录，只带已有 cookies → 要登录得自己调 `login_with_option_auth()`。

【实测】`auth.cookies` 为空 + `username`/`password` 为 `${JM_USERNAME}` 未设时，
本机 `manga doctor` 报「未登录（只有凭据）」；**账号密码是凭据，不是登录态**。

> 收藏夹 / 签到 / 发评论这几块**本机未实测**（需要登录态 + 灰色来源）——
> 标【文档】，用之前先小规模验证。

## 8. 域名与重试

```python
JmModuleConfig.get_html_domain_all()     # 抓发布页 → 全部网页域名
JmModuleConfig.get_html_domain()         # 永久网域跳转 → 一个当前可用域名
JmModuleConfig.DOMAIN_HTML_LIST = domains  # 替换全局默认
option.new_jm_client(domain_list=[...], impl='html')
```

```yaml
plugins:
  after_init:
    - plugin: advanced_retry
      kwargs:
        retry_config: {retry_rounds: 3, retry_domain_max_times: 5}
```

`advanced_retry`：多轮轮询域名列表 + 按历史失败次数排序 + 超过上限的域名拉黑。
【源码】**`retry_config` 必填且必须是 dict**（`invoke(self, retry_config, **kwargs)`）。

【实测】**API 端有自动域名更新**：命中「更新 API 域名失败 → 换下一个地址 → 成功」后，
`JmModuleConfig.DOMAIN_API_LIST` 会被**替换成新域名池**（日志里能看到 `(new)[...] ---→ (old)[...]`）。
开关是 `JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN`。

## 9. 异步 【文档】

```python
album, dler = await download_album_async('438696')
await download_photo_async('456')
batch = await download_batch_async(download_album, ['123','456'])

async with option.new_jm_async_client() as cl:     # 自动初始化 + 自动释放
    album = await cl.get_album_detail(123)
async for page in cl.search_gen('+MANA'): ...
```

- 异步客户端方法与同步**同构**（`get_album_detail` / `search` / `categories_filter` / `favorite_folder` / `daily_checkin` / 评论 / `asend`）。
- 异步端**不支持** `domain_retry_strategy`（传了 `TypeError`）。

## 10. 异常

| 异常 | 含义 | 属性 |
|---|---|---|
| `MissingAlbumPhotoException` | 本子/章节不存在 | `.error_jmid` |
| `JsonResolveFailException` | 解析失败 | `.resp`（`.status_code` / `.text`） |
| `RequestRetryAllFailException` | 重试耗尽 | — |
| `PartialDownloadFailedException` | 部分图片失败（`check_exception=True`） | `.downloader`（`.download_failed_photo` / `.download_failed_image`） |
| `DownloadCancelledException` | 被取消 | `.reason` / `.completed_results` / `.unfinished_ids` |
| `JmcomicException` | 兜底基类 | — |

【实测】`MissingAlbumPhotoException` / `RequestRetryAllFailException` 都能按类名捕获并映射退出码
（本项目 `src/manga/adapters/jmcomic.py` 的 `_map_exception`）。

## 11. 常用常量 【源码】

`JmMagicConstants`：`ORDER_BY_LATEST/VIEW/PICTURE/LIKE/SCORE/COMMENT`（`SCORE`/`COMMENT` 仅网页端）、
`ORDER_FF_FAVORITE_TIME/UPDATE_TIME`、`ORDER_MONTH/WEEK/DAY_RANKING`、`TIME_TODAY/WEEK/MONTH/ALL`、
`CATEGORY_ALL/DOUJIN/SINGLE/SHORT/ANOTHER/HANMAN/MEIMAN/DOUJIN_COSPLAY/3D/ENGLISH_SITE`、
`SUB_CHINESE/JAPANESE/OTHER/3D/COSPLAY/DOUJIN_CG/...`、`SCRAMBLE_220980/268850/421926`。

`JmModuleConfig` 常用：`DOMAIN_HTML_LIST` `DOMAIN_API_LIST` `DOMAIN_IMAGE_LIST` `DEFAULT_CLIENT_IMPL`(=`'api'`)
`PAGE_SIZE_SEARCH`(80) `PAGE_SIZE_FAVORITE`(20) `VAR_FILE_NAME_LENGTH_LIMIT`(100)
`AFIELD_ADVICE` / `PFIELD_ADVICE`（自定义字段扩展点，默认空字典）。

## 12. 本项目校准过的其它事实 【实测/源码】

| 事实 | 出处 |
|---|---|
| jmcomic 的日志默认打在 **stdout** → 会污染 JSON 信封 | `jm_config.setup_default_jm_logger`（本项目已改道 stderr） |
| 内置插件**恰好 21 个**，注册表 = `JmModuleConfig.REGISTRY_PLUGIN` | 源码 + 实测 |
| `option.dir_rule.rule_dsl` = option 里写的**原始 rule 字符串** | `jm_option.py:72` |
| `JmOption.default_dict()` 的默认值（`Bd_Pname`、threading 20/24、impl=api…） | 源码；见 [option-and-plugins.md](option-and-plugins.md) |
| 21 个插件的 invoke 形参与依赖 | 源码；见 [option-and-plugins.md](option-and-plugins.md) |
| DirRule 可用变量共 **50** 个（A 25 + P 25） | 源码；见 [naming.md](naming.md) |

**重新校准**：`uv run python tools/jmcomic_facts.py plugins|dirrule|check`
