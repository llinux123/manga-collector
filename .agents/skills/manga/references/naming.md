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
- 允许的写法：`01 - 标题`、`001 - 标题`、`Vol.3 - 标题`、`第01话 标题`、`Chapter 01`、`001-标题`。
- 不合规：没有话号前缀（`chapter_two.cbz`）、话号含中文数字。
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

| 字段 | 要求 | 坑 |
|---|---|---|
| `Series` | 与目录名**逐字一致** | 不一致报 `SeriesMismatch`（告警）；Kavita 会用元数据**覆盖**解析结果 |
| `Number` | **必填**，纯数字 | 缺失报 `Metadata`；与文件名不符报 `NumberMismatch` → 排序错乱 + 被误判重复 |
| `Volume` | 卷式命名填 | Komga 会把系列名改写成 `<Series> (<Volume>)` |
| `Manga` | `Yes` / `YesAndRightToLeft` | **Komga 只认 `YesAndRightToLeft` 才 RTL**，写 `Yes` 不生效 |
| `PageCount` | 图片页数 | 缺省时服务端可能算错 |

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

### 变量表（29 个）

**本子（A 前缀，15 个）**

| 变量 | 含义 | 示例 |
|---|---|---|
| `{Aid}` / `{Aalbum_id}` | 本子 ID | `248965` |
| `{Aname}` / `{Atitle}` | 本子标题（原始全名） | `喂我吃吧 老師! [欶瀾漢化組]` |
| `{Aoname}` | 剥离标签后的原名 | `喂我吃吧 老師!` |
| `{Aauthor}` | 第一作者 | `BLVEFO9` |
| `{Aauthoroname}` | `【作者】原名` | `【BLVEFO9】喂我吃吧 老師!` |
| `{Aaidoname}`→实为 `{Aidoname}` | `[ID] 原名` | `[248965] 喂我吃吧 老師!` |
| `{Adescription}` | 简介 | |
| `{Apage_count}` | 总页数（int） | `42` |
| `{Apub_date}` | 发布日期 | `2023-01-15` |
| `{Aupdate_date}` | 更新日期 | `2023-06-20` |
| `{Alikes}` | 点赞数 | `1K` |
| `{Aviews}` | 观看数 | `40K` |
| `{Acomment_count}` | 评论数（int） | `128` |

**章节（P 前缀，14 个）**

| 变量 | 含义 | 示例 |
|---|---|---|
| `{Pid}` / `{Pphoto_id}` | 章节 ID | `212214` |
| `{Pname}` / `{Ptitle}` | 章节名 | `94 突然打來` |
| `{Poname}` | 剥离标签后的章节原名 | `94 突然打來` |
| `{Pauthor}` | 章节作者（优先取本子作者） | `BLVEFO9` |
| `{Pauthoroname}` | `【作者】章节原名` | |
| `{Pidoname}` | `[ID] 章节原名` | |
| `{Psort}` | 章节排序值（int） | `3` |
| `{Pindex}` / `{Palbum_index}` | 章节在本子中的序号（从 **1** 开始） | `3` |
| `{Pindextitle}` | `第X話 章节名` | `第3話 94 突然打來` |
| `{Palbum_id}` | 所属本子 ID | `248965` |

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
