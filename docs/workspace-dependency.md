# 与 manga 工作区的依赖契约

本项目（manga-collector）只做**采集侧**。书库侧的流水线脚本属于 **manga 工作区**，
本项目**引用而不复制** —— 脚本逻辑的单一事实来源始终在 manga 工作区。

## 1. 工作区定位

`manga.config.find_workspace()` 按顺序探测：

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | 环境变量 `MANGA_WORKSPACE` | 显式指路，优先级最高 |
| 2 | `<本项目上级目录>/manga` | 同级仓库布局 |
| 3 | `~/Downloads/dsh/manga` | 历史默认位置 |

判定标志：`<工作区>/scripts/Test-MangaLibrary.ps1` 存在。

## 2. 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `MANGA_WORKSPACE` | manga 工作区路径 | 探测（见上） |
| `MANGA_SCRIPTS_DIR` | 4 个流水线脚本所在目录 | `<工作区>/scripts` |
| `MANGA_LIBRARY_ROOT` | 书库根 | `%USERPROFILE%\Documents\Library` |
| `MANGA_OPTION_FILE` | jmcomic option 文件 | 本仓库 `config/jmcomic-option.yml`；不存在则用 `<工作区>/jmcomic/option.yml` |
| `JM_OPTION_PATH` | jmcomic 自己的 option 路径（上游约定） | 未设 |
| `JM_USERNAME` / `JM_PASSWORD` | 禁漫账号 | 未设 |

> `MANGA_SCRIPTS_DIR` 只影响**脚本目录**；`Convert-JmComicToCbz.ps1` 不在 `scripts/` 下，
> 由 `find_script()` 在 `<工作区>/jmcomic/` 里找。

## 3. 被调用的脚本

| 脚本 | 归属 | 用途 | 退出码 |
|---|---|---|---|
| `Test-MangaLibrary.ps1` | `<工作区>/scripts/` | 只读校验权威库 | 0 / 1 告警 / 2 错误 |
| `Invoke-LibraryIngest.ps1` | `<工作区>/scripts/` | 入库整理（默认 dry-run） | 0 / 1 / 2 |
| `Initialize-MangaLayout.ps1` | `<工作区>/scripts/` | 建目录（只新增） | 0 / 1 / 2 |
| `Export-MangaLibrary.ps1` | `<工作区>/scripts/` | 导出到共享区 | 0 / 1 / 2 |
| `Convert-JmComicToCbz.ps1` | `<工作区>/jmcomic/` | 散图 → CBZ | 0 / 1 / 2 |

退出码映射：PS `0` → CLI `0`；PS `1` → CLI `0` + `warnings`；PS `2` → CLI `2`。

## 4. 目录契约

本项目**不拥有**书库目录，只读写其中约定的位置：

```
<MANGA_LIBRARY_ROOT>\
├── 00-Incoming\Manga\        # 收件箱：本项目的下载落点（写）
├── 00-Incoming\_raw_jmcomic\ # jmcomic 散图暂存（写）
├── Manga\Library\            # 权威库（脚本写；本项目只读校验）
├── Manga\Metadata\           # 校验报告与日志（脚本写）
└── Export\Manga\             # Syncthing 共享区（**本项目只读 + 守卫**）
```

**红线**：灰色来源的产物只允许出现在 `00-Incoming\*` 与 `Manga\Library`，
**绝不允许**进入 `Export\Manga`。`manga guard` 以退出码 6 拒绝。

## 5. 命名与元数据口径

唯一规范在 manga 工作区：`spec/naming-and-metadata.md`。
本项目只**引用**，不改写。本仓库 `references/naming.md` 是它的浓缩副本，
以 **jmcomic 2.7.11 源码实测**为准（文档与代码有 3 处默认值不一致，见评估文档）。

## 6. 目录规则（DirRule）契约

`config/jmcomic-option.yml` 的 `dir_rule.rule` 必须是能渲染出**两层**（本子/章节）的写法：

| 写法 | 渲染 | |
|---|---|---|
| `Bd/Atitle/Pname` | `<base>/<本子标题>/<章节名>` | ✅ 本项目采用 |
| `Bd_Atitle_Pname` | `<base>/<本子标题>/<章节名>` | ✅ |
| `Bd / {Atitle} / {Pname}` | `<base>/<本子标题>/<章节名>` | ✅ |
| `Bd_Atitle/Pname` | `<base>/`**`Bd_Atitle`**`<章节名>` | ❌ 字面量目录 |

`Convert-JmComicToCbz.ps1` 假设两层结构 `<base>\<本子>\<章节>\*.jpg`；
写成字面量目录会把所有章节误归到名为 `Bd_Atitle` 的系列下。
`tests/test_dirrule.py` 对本仓库的 `config/jmcomic-option.yml` 做了渲染级回归断言。

## 7. 变更约定

- **改流水线脚本** → 在 manga 工作区改，本仓库不动；本仓库的 `references/pipeline.md` 同步更新。
- **改命名/元数据规范** → 在 manga 工作区改 `spec/`；本仓库的 `references/naming.md` 同步更新。
- **改 jmcomic 配置** → 本仓库改 `config/jmcomic-option.yml`；实机那份在 manga 工作区的 `jmcomic/option.yml`。
  两处必须一致（可用 `MANGA_OPTION_FILE` 指到其中一份）。
- **本仓库不复制、不 fork、不改写**工作区的脚本与规范。
