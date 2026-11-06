# 流水线参考（何时读我：用户要入库、校验、导出，或 CLI 报退出码相关的问题时）

## 1. 五层结构

```
采集（白/绿/灰三档来源）
   ↓  落点统一：00-Incoming\Manga
00-Incoming\Manga\            # 收件箱：待整理的下载件（CBZ）
   ↓  重命名 + 写 ComicInfo.xml
Manga\Library\<系列名>\        # 权威库：一话一个 CBZ + 根目录 ComicInfo.xml
   ↓  校验（退出码 0 才放行）
Export\Manga\<系列名>\        # 干净导出 → Syncthing 共享给手机（唯一共享区）
   ↓
手机（KOReader / Mihon / Komikku / Kotatsu-Redo）—— PC 关机也能读
```

**旁路（灰色来源专用，绝不进共享区）**

```
00-Incoming\_raw_jmcomic\     # jmcomic 散图暂存（含 .nomedia，不参与 Syncthing）
   ↓  Convert-JmComicToCbz.ps1
00-Incoming\Manga\            # 仍走同一条入库流水线，但**停在权威库**
```

## 2. 四个脚本与退出码

| 脚本 | 位置 | 作用 | 退出码 |
|---|---|---|---|
| `Test-MangaLibrary.ps1` | `scripts/` | 只读校验器 | 0 全通过 / 1 仅有告警 / 2 有错误 |
| `Invoke-LibraryIngest.ps1` | `scripts/` | 入库整理器（默认 dry-run） | 0 / 1 冲突或跳过 / 2 错误 |
| `Initialize-MangaLayout.ps1` | `scripts/` | 建目录（只新增） | 0 / 1 有失败项 / 2 库根不可用 |
| `Export-MangaLibrary.ps1` | `scripts/` | 导出到共享区（HardLink） | 0 / 1 冲突 / 2 源库不可用 |
| `Convert-JmComicToCbz.ps1` | `jmcomic/` | 散图 → CBZ | 0 / 1 有跳过 / 2 参数或环境错误 |

**CLI 的映射**：PS `0` → CLI `0`；PS `1` → CLI `0` 且 `warnings` 非空；PS `2` → CLI `2`。
即 **PS 的 1 是「告警」不是「错误」**，别把它当失败。

## 3. 入库标准动作（固定三步）

```powershell
# ① 打���（图片 → CBZ），先干跑
manga convert-cbz                      # 默认 dry-run
manga convert-cbz --apply

# ② 入库（重命名 + 写 ComicInfo.xml），先干跑
manga ingest                           # 默认 dry-run
manga ingest --apply --generate-comicinfo --zero-pad

# ③ 校验（闸门）
manga verify                           # 退出码 0 才算入库成功
```

`--generate-comicinfo`：包内没有 `ComicInfo.xml` 时生成一份；**已存在则保留原样，不覆盖**。
`--zero-pad`：纯数字话号补零到 3 位（`1` → `001`），同系列内排序才稳。

## 4. 命名规范（唯一口径）

```
<系列名>\<卷或话号> - <标题>.cbz
```

- 校验器内置正则：`^(?:(?:vol|volume|卷)\s*\.?\s*)?(?:第\s*)?(?<number>\d+(?:\.\d+)*(?:[-–—]\d+)?)\s*(?:(?:[-–—]|:|：)\s*|\s+)?(?<title>.+)?$`
- `01 - 标题`、`001 - 标题`、`Vol.3 - 标题`、`第01话 标题`、`Chapter 01` 都算合规；没有话号前缀（如 `chapter_two.cbz`）算违规。
- 图片**平铺在包根目录**（不要子目录）——Mihon 本地源会忽略包内子目录。
- 完整规范在 manga 工作区的 `spec/naming-and-metadata.md`（见 [workspace-dependency.md](../../../docs/workspace-dependency.md)）。

## 5. ComicInfo.xml 最小字段

包**根目录**（不是子目录）：

```xml
<ComicInfo>
  <Series>系列名</Series>
  <Number>1</Number>
  <Volume>1</Volume>
  <Count>1</Count>
  <Manga>Yes</Manga>
  <LanguageISO>zh-CN</LanguageISO>
  <PageCount>5</PageCount>
  <Summary>...</Summary>
</ComicInfo>
```

- `Number` **必填**，缺失报 `Metadata` 错误。
- `LanguageISO` 由目录前缀推断（`zh/ja/ko/en` → `zh-CN/ja/ko/en`）。
- 写不了 ComicInfo 时可用 ComicTagger（GUI，1.5.5）补。

## 6. 校验器查什么（问题类别 → 后果）

| 类别 | 级别 | 含义 |
|---|---|---|
| `Naming` | 错误 | 文件名不合规 → 刮削会失败或重复入库 |
| `Directory` | 错误/告警 | 文件直接躺在库根 / 层级过深 |
| `Archive` | 错误 | CBZ 打不开（损坏） |
| `EmptyVolume` | 错误 | 零字节 / 无图片页 / 页数 < 阈值（默认 5） |
| `Metadata` | 错误 | 缺 `ComicInfo.xml`、解析失败、或 `Number` 缺失 |
| `NumberMismatch` | 错误 | 文件名编号 ≠ `ComicInfo.Number` → 排序错乱 + 被误判重复 |
| `NumberFormat` | 告警 | 编号不是纯数字（如 `SP01`），跳过数值比对 |
| `Duplicate` | 错误 | 同系列内 `Number + Volume` 组合重复（真重复） |
| `Split` | 告警 | 同话号不同卷号 = 一话分卷，**不算重复** |
| `WidthInconsistent` | 告警 | 同系列话号补零宽度不一致 |
| `SeriesMismatch` | 告警 | `ComicInfo.Series` ≠ 所在目录名 |
| `Layout` | 告警 | 图片在包内子目录 |
| `DuplicateName` | 告警 | 同名副本 |
| `CbrDependency` | 告警 | CBR/RAR 需 unrar 依赖 |

**重复判定口径**：同系列目录内按 `Number + Volume` 组合键；同号不同卷 = 分卷（告警），同号同卷 = 真重复（错误）。
**编号一致性口径**：文件名编号与 `ComicInfo.Number` 做**数值**比较（去前导零，允许一位小数 `012`≡`12`、`12.5`≡`12.50`）；
文件名带 `Vol.`/`Volume`/`卷` 前缀、文件名不含「编号 - 标题」结构、`ComicInfo.Number` 为空 → 跳过比较，不误报。

## 7. 导出（仅白/绿来源）

`Export-MangaLibrary.ps1` 默认 **HardLink**（0 额外磁盘占用，跨卷/非 NTFS 自动降级 Copy）。

- 目标存在且大小 + SHA256 一致 → 跳过（`up-to-date`）；
- 目标存在但内容不同 → 记为**冲突**并跳过，**绝不覆盖**（退出码 1）；
- 导出前**先跑校验器**，只有退出码 0 才允许导出；
- `-Prune` 只删 CBZ，**非 CBZ 一律不删**（`.nomedia` 等），目录不删。

**红线**：灰色来源**不走这一步**。`manga guard` 检测到共享区新增文件会以退出码 6 拒绝。

## 8. 目录布局（本机实测）

```
C:\Users\muxin\Documents\Library\
├── 00-Incoming\Manga\              # 收件箱（待整理）
├── 00-Incoming\_raw_jmcomic\       # jmcomic 散图暂存（不参与 Syncthing）
├── Manga\Library\<系列名>\          # 权威库
├── Manga\Metadata\                 # 校验报告 CSV、操作日志
├── Export\Manga\<系列名>\          # 干净导出 → Syncthing → 手机（唯一共享区）
├── Archive\Manga\{BadFormats,Duplicates,Rejected}\
└── Backups\Manga\                  # 与库同盘，**不算备份**（3-2-1 需 ≥1 份异地副本）
```
