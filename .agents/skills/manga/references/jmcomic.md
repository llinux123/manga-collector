# jmcomic 能力参考（何时读我：要调 jmcomic 的具体 API 时）

> 核对版本：**2.7.11**（本机 `.venv` 安装包，与官方文档渲染的源码一致）。
> 官方文档：<https://jmcomic.readthedocs.io/zh-cn/latest/>
> **文档只渲染带 docstring 的成员，源码里还有更多**；本文以源码为准。

## 0. 平台能力差异（最容易踩）

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
result.manifest      # DownloadManifest
result.duration      # 总耗时（秒）
album, dler = result # 可解包

batch = download_album(['123','456'])
batch.failed         # {album_id: Exception}
batch.all_succeeded  # bool
batch.total          # 去重后的 ID 数
for r in batch: ...  # 只遍历成功项
batch['123']; batch.get(aid); aid in batch

manifest.image_filepath_list              # 所有图片路径（含命中缓存的）
manifest.get_export_filepath_list('pdf')  # 导出物（后缀可省点）
```

## 3. 检索（5 个维度）

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
client.album_comment(video_id, comment)         # 发评论（仅 html）
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

## 5. 下载：过滤 / 取消 / 并发

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

配置式过滤器（写 option，不用写代码）：`image_suffix_filter`（`allowed_orig_suffix`）、
`find_update`（`{album_id: photo_id}` 只下新章）、`skip_photo_with_few_images`（`at_least_image_count`）。

### 取消

```python
from jmcomic import DownloadControl, jm_task_context, DownloadCancelledException
ctrl = DownloadControl()
with jm_task_context(control=ctrl, task_id='task-001'):
    download_album(['123456','789012'])
ctrl.cancel('用户取消')
# DownloadCancelledException: .reason / .completed_results / .unfinished_ids
```

### 并发（三层）

```python
from jmcomic import JmSyncRuntime
with JmSyncRuntime(album_workers=2, photo_workers=3, image_workers=8):
    download_album(['123','456'])
```

- 经典路径下**本子层并发 = 列表长度**，没有旋钮 → 想封顶必须用 Runtime。
- `JmAsyncRuntime(decode_workers=N)` 只管**解密**线程池（网络 IO 是协程）。
- 外传 `ThreadPoolExecutor` 给 Runtime 时，**Runtime 不会关它**（谁创建谁关）。

### 任务上下文

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

## 7. 登录 / 收藏 / 签到

```python
option.login_with_option_auth()        # 全局，幂等；写回 auth.cookies + auth.user_id
option.login_with_option_auth_async()
client.login('user', 'pass')            # 只影响该 client，不写回 option.auth

client.favorite_folder(page=1, order_by=JmMagicConstants.ORDER_FF_FAVORITE_TIME, folder_id='0')
for page in client.favorite_folder_gen(): ...
client.add_favorite_album('438696')
client.delete_favorite_album('438696')
client.toggle_favorite_album('438696', expected_type=None, rollback_when_mismatch=True)

client.get_daily()                      # 打卡日历
r = client.daily_checkin()              # r.code: 0 成功 / 1 已签到
```

**登录状态判定**（两条都满足才算已登录）：`auth.cookies` 非空 **且** 用户标识非空（html 看 `auth.username`，api 看 `auth.user_id`）。
`new_jm_client()` **从不**登录，只带已有 cookies → 要登录得自己调 `login_with_option_auth()`。

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

`advanced_retry`：多轮轮询域名列表 + 按历史失败次数排序 + 超过上限的域名拉黑。**`retry_config` 必填且必须是 dict。**
API 端另有自动域名更新（`FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN`）。

## 9. 异步

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

## 11. 常用常量

`JmMagicConstants`：`ORDER_BY_LATEST/VIEW/PICTURE/LIKE/SCORE/COMMENT`（`SCORE`/`COMMENT` 仅网页端）、
`ORDER_FF_FAVORITE_TIME/UPDATE_TIME`、`ORDER_MONTH/WEEK/DAY_RANKING`、`TIME_TODAY/WEEK/MONTH/ALL`、
`CATEGORY_ALL/DOUJIN/SINGLE/SHORT/ANOTHER/HANMAN/MEIMAN/DOUJIN_COSPLAY/3D/ENGLISH_SITE`、
`SUB_CHINESE/JAPANESE/OTHER/3D/COSPLAY/DOUJIN_CG/...`、`SCRAMBLE_220980/268850/421926`。

`JmModuleConfig` 常用：`DOMAIN_HTML_LIST` `DOMAIN_API_LIST` `DOMAIN_IMAGE_LIST` `DEFAULT_CLIENT_IMPL`(=`'api'`)
`PAGE_SIZE_SEARCH`(80) `PAGE_SIZE_FAVORITE`(20) `VAR_FILE_NAME_LENGTH_LIMIT`(100)
`AFIELD_ADVICE` / `PFIELD_ADVICE`（自定义字段扩展点）。
