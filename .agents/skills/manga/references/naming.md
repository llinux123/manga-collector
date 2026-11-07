# 命名与目录规则（何时读我：设计文件夹名、写 ComicInfo、判断校验器报错时）

> 唯一权威规范在 **manga 工作区**：`spec/naming-and-metadata.md`（模板 `templates/ComicInfo.template.xml`）。
> 本篇是它的浓缩副本，以 **jmcomic 2.7.11 源码实测**为准；工作区路径见 [workspace-dependency.md](../../../docs/workspace-dependency.md)。

## 1. 两套命名，各管一层

| 层 | 谁决定 | 例子 |
|---|---|---|
| **库内路径** | 目录名 = 系列名 | `Manga\Library\_sample series\<编号> - <标题>.cbz` |
| **下载落点** | jmcomic 的 `dir_rule.rule` | `00-Incoming\_raw_jmcomic\<本子标题>\<章节名>\00001.webp` |

**两套必须对得上**：jmcomic 的 `dir_rule.rule` 用 `Bd/Atitle/Pname`（两层：`<本子>\<章节>`），
`Convert-JmComicToCbz.ps1` 才认得出「哪张图属于哪本哪话」。
改了 `rule` 却不同步改转换脚本 → 转换会失败或归错系列。**本项目锁定 `Bd/Atitle/Pname`。**

> ⚠ **分隔符与写法必须配套，不能混用**（2026-11-06 实测，src 2.7.11）：
>
> | 写法 | 渲染结果 | |
> |---|---|---|
> | `Bd/Atitle/Pname` | `<base>/<本子标题>/<章节名>` | ✅ |
> | `Bd_Atitle_Pname` | `<base>/<本子标题>/<章节名>` | ✅ |
> | `Bd / {Atitle} / {Pname}` | `<base>/<本子标题>/<章节名>` | ✅ |
> | `Bd_Atitle/Pname` | `<base>/`**`Bd_Atitle`**`<章节名>` | ❌ **字面量目录** |
>
> 原因：分隔符是 `/` 时按 `/` 切段，首段 `Bd_Atitle` 既不等于 `Bd`、又不以 `A`/`P` 开头 →
> 落到 f-string 解析器被原样输出 → 生成一个字面量叫 `Bd_Atitle` 的目录。
> 后果：`Convert-JmComicToCbz.ps1` 会把所有章节归到名为 `Bd_Atitle` 的系列下（静默污染库）。
> **本仓库 2026-11-06 已修正 `jmcomic/option.yml`，并用 `tests/test_dirrule.py` 钉死。**

## 2. 文件名规范（库内）

```
<卷或话号> - <标题>.cbz
```

- 纯数字话号补零到 **3 位**（`1` → `001`）；卷号补零到 2 位。同系列内宽度必须一致，否则报 `WidthInconsistent`。
- **合规**（用校验器正则实测）：`01 - 标题`、`001 - 标题`、`001-标题`、`Vol.3 - 标题`、`第01话 标题`、`12.5 - 标题`。
- **违规**：没有数字话号前缀（`chapter_two.cbz`）、话号含中文数字、**`Chapter 01`**（编号必须以数字开头，英文 `Chapter` 不认）。
- 非法字符按 F-05 替换：`/` → `／`，`:` → `：`，其余 `\*?"<>|` → `-`；Windows 保留名（`CON`/`COM1`…）前加 `_`。
- **一话一个包**，包内图片**平铺在根目录**（不放子目录）。

## 3. ComicInfo.xml（必填字段与坑）

```xml
<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>章节标题</Title>
  <Series>系列名</Series>          <!-- 必须与所在目录名逐字一致 -->
  <Number>1</Number>              <!-- 必填；与文件名编号数值一致 -->
  <Volume>1</Volume>              <!-- 卷式命名用；非卷式可留 1 -->
  <Count>1</Count>                <!-- 1=连载中 0=完结 -->
  <Manga>Yes</Manga>
  <LanguageISO>zh-CN</LanguageISO>
  <PageCount>42</PageCount>
  <Summary>简介</Summary>
  <Genre>分类</Genre>
  <Tags>标签1,标签2</Tags>
</ComicInfo>
```

| 字段 | 校验器**实际**怎么查 | 坑 |
|---|---|---|
| `ComicInfo.xml` | 缺失 → `Metadata` 错误；存在但**解析失败** → 也按「缺必填元数据」报错误 | 错误消息里列了 `Series/Number/Volume/Count/Manga/LanguageISO`，那是**举例文案**，不是逐字段校验 |
| `Number` | **唯一逐字段必填**：为空 → `Metadata` 错误；与文件名编号做**数值**比对（`012`≡`12`） | 与文件名不符报 `NumberMismatch` → 排序错乱 + 被误判重复 |
| `Series` | 与目录名不一致 → **告警** `SeriesMismatch`（不是错误） | Kavita 会用元数据**覆盖**解析结果 → 目录名与元数据不一致时，元数据说了算 |
| `Volume` | **不校验**；只参与「重复判定」的组合键（Number+Volume）；卷式命名（`Vol.`/`Volume`/`卷` 前缀）跳过编号比对 | Komga 会把系列名改写成 `<Series> (<Volume>)` |
| `Manga` | **不校验**（是阅读端的事） | **Komga 只认 `YesAndRightToLeft` 才 RTL**，写 `Yes` 不生效 |
| `PageCount` | **不校验** | 缺省时服务端可能算错 |
| `LanguageISO` | **不校验**；入库脚本按路径前缀推断，没命中就留空 | 空值会让阅读端的多语言筛选失效 |

**服务端映射**：Komga 的 `Series` 取各书**最常见值**、`Publisher`/`Genre`/`LanguageISO` 同理；
Kavita 把 `Number`→章号、`Volume`→卷、`Count`→完结状态、`Format`→Special、`SeriesGroup`→Collection、`StoryArc`→Reading List。

## 4. DirRule（下载落点的 DSL）

配置在 `dir_rule`：

```yaml
dir_rule:
  base_dir: D:/a/b/c/        # 默认 = 当前工作目录（务必改）
  rule: Bd / {Atitle} / {Pname}
  normalize_zh: null         # zh-cn 简体 / zh-tw 繁体 / null 不转换
```

### 语法规则

- `Bd` = 根目录（可省略，自动补在最前）。
- **分隔符 `/` 与 `_` 二选一，不能混用**：
  - 规则里含 `/` → 按 `/` 切段；此时首段**不能带 `Bd_` 前缀**（`Bd/Atitle` ✅，`Bd_Atitle` ❌ 会变字面量目录）；
  - 规则里不含 `/` 但含 `_` → 按 `_` 切段（`Bd_Atitle_Pname` ✅）。
  - 旧写法 `Bd_Pname` ≡ `Bd / {Pname}`；`Bd / Atitle / Pindextitle` ≡ `Bd / {Atitle} / {Pindextitle}`。
- `{...}` 走 Python `str.format()`，可混排文字与变量；`Axxx`/`Pxxx` 是传统写法（取实体属性）。
- 每段结果会做：繁简归一化 → Windows 非法字符修正 → `strip()`；文件名长度上限 **100**（`VAR_FILE_NAME_LENGTH_LIMIT`）。
- `base_dir` 支持 `${ENV}`。
- 本子根目录只保留 `Bd` 段和 `A` 开头的段（photo 级片段跳过）。

### 变量表（源码可用 **50** 个）

> **校对日期：2026-11-07 · 对标 jmcomic 2.7.11 源码**。
> 口径 = `JmBaseEntity.get_properties_dict()`：实例字段 + MRO 上的 property，
> 按类名前缀取（`JmAlbumDetail` → `A`，`JmPhotoDetail` → `P`）。
> 重新校对：`uv run python tools/jmcomic_facts.py dirrule`；
> 漂移检查：`uv run python tools/jmcomic_facts.py check`。
>
> ⚠ 旧版本这张表只写了 29 个，**漏了一半** —— 漏的那些在下面「其余可用变量」里。

**本子（A 前缀，25 个）**

| 变量 | 含义 | 示例 |
|---|---|---|
| `{Aid}` / `{Aalbum_id}` | 本子 ID | `248965` |
| `{Aname}` / `{Atitle}` | 本子标题（原始全名） | `喂我吃吧 老師! [欶瀾漢化組]` |
| `{Aoname}` | 剥离标签后的原名 | `喂我吃吧 老師!` |
| `{Aidoname}` | `[ID] 原名` | `[248965] 喂我吃吧 老師!` |
| `{Aauthor}` | 第一作者（无作者时是默认值） | `BLVEFO9` |
| `{Aauthors}` | 全部作者（列表） | `[BLVEFO9]` |
| `{Aauthoroname}` | `【作者】原名` | `【BLVEFO9】喂我吃吧 老師!` |
| `{Adescription}` | 简介 | |
| `{Atags}` | 标签（列表） | |
| `{Aactors}` | 登场人物（列表） | |
| `{Aworks}` | 作品（列表） | |
| `{Apage_count}` | 总页数（int） | `42` |
| `{Apub_date}` | 发布日期 | `2023-01-15` |
| `{Aupdate_date}` | 更新日期 | `2023-06-20` |
| `{Alikes}` | 点赞数 | `1K` |
| `{Aviews}` | 观看数 | `40K` |
| `{Acomment_count}` | 评论数（int） | `128` |
| `{Aepisode_list}` | 章节元组列表 `[photo_id, index, title, pub_date]` | ⚠ 内部结构，别用于命名 |
| `{Arelated_list}` | 相关作品 ID 列表 | ⚠ 内部结构 |
| `{Ais_favorite}` | 是否已收藏（bool） | ⚠ 内部状态 |
| `{Aliked}` | 是否已点赞（bool） | ⚠ 内部状态 |
| `{Aseries_id}` / `{Ascramble_id}` | 混淆相关 ID | ⚠ 内部字段，命名里用会得到乱码目录 |

**章节（P 前缀，25 个）**

| 变量 | 含义 | 示例 |
|---|---|---|
| `{Pid}` / `{Pphoto_id}` | 章节 ID | `212214` |
| `{Pname}` / `{Ptitle}` | 章节名 | `94 突然打來` |
| `{Poname}` | 剥离标签后的章节原名 | `94 突然打來` |
| `{Pidoname}` | `[ID] 章节原名` | |
| `{Pauthor}` | 章节作者（优先取本子作者） | `BLVEFO9` |
| `{Pauthoroname}` | `【作者】章节原名` | |
| `{Pindex}` / `{Palbum_index}` | 章节在本子中的序号（从 **1** 开始） | `3` |
| `{Pindextitle}` | `第X話 章节名` | `第3話 94 突然打來` |
| `{Palbum_id}` | 所属本子 ID | `248965` |
| `{Psort}` | 章节排序值（int） | `3` |
| `{Ppage_arr}` | 图片页数组 | ⚠ 内部结构 |
| `{Pfrom_album}` | 所属本子实体 | ⚠ 内部结构 |
| `{Pis_single_album}` | 是否「单章本子」 | ⚠ 内部状态 |
| `{Pdata_original_0}` / `{Pdata_original_domain}` / `{Pdata_original_query_params}` | 混淆图片的原始信息 | ⚠ 内部字段 |
| `{Pscramble_id}` / `{Pseries_id}` | 混淆/系列 ID | ⚠ 内部字段 |
| `{Ptags}` / `{P_author}` / `{P_series_id}` / `{P_tags}` | 章节的标签/作者/系列（`_` 开头的是私有存储） | ⚠ 私有字段，别用 |

**易错点**

- `{Aaidoname}` **不存在**（旧文档写错过）—— 正确拼写是 `{Aidoname}`。
- `{Pindex}` 与 `{Palbum_index}` 是**同一个值**（章节序号，1 起）；`{Pindextitle}` 才是 `第X話 名字`。
- `Bd` 段不是字段，是落点根目录（`base_dir`）。
- 段落以 `A`/`P` 开头 → 取对应实体的属性；其它（含 `{...}`）→ 按 f-string 解析。
  两套混用时，**首段若不是 `Bd` 会自动补 `Bd` 在最前**。

### 实战 rule 样例

| 场景 | rule |
|---|---|
| 最基础 | `Bd / {Pname}` |
| 本子→章节两层 | `Bd/Atitle/Pname` |
| 作者+标题 | `Bd / 【{Aauthor}】{Atitle}` |
| 车号+标题 | `Bd / JM{Aid}-{Aoname}` |
| 第X话 | `Bd / {Pindextitle}` |
| 按月归档 | `Bd / {Apub_date} / [{Aid}]{Aoname}` |
| 三层（作者→本子→章节） | `Bd / {Aauthor} / [{Aid}]{Aoname} / {Pindextitle}` |

### 自定义字段

实体的 `get_properties_dict()` 返回全部实例字段 + `@property`（带 `album_`/`photo_` 前缀）。
要加自己的字段：

```python
JmModuleConfig.AFIELD_ADVICE['myname'] = lambda album: '自定义名称'   # 之后可用 {Amyname}
```

## 5. 繁简归一化

```yaml
dir_rule:
  normalize_zh: zh-cn   # 或 zh-tw；null = 不转换
```

依赖可选库 **`zhconv`**；未安装或转换失败会**回退为原字符串继续**，不报错（所以发现目录名还是繁体时，先查 `zhconv` 装没装）。
