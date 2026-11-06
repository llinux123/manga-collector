# 协作与版本管理约定

本仓库按 **阶段（Phase）** 推进，**每个阶段至少一次 git 提交**。
阶段划分见 [docs/manga-skill-plan.md](docs/manga-skill-plan.md)。

## 1. 分支

| 分支 | 用途 |
|---|---|
| `main` | 始终可跑（测试全绿），只在阶段收口时合入 |
| `p/<阶段号>-<短名>` | 阶段开发分支，如 `p/1-jmcomic-adapter` |

阶段分支合入 `main` 时用 **squash**，保证 `main` 的历史里 **一个阶段 = 一个提交**。

```powershell
git switch -c p/1-jmcomic-adapter
# …开发…
git add -A
git commit -m "feat(P1): jmcomic 适配层 —— 搜索/详情/下载/导出/域名发现"
git switch main
git merge --squash p/1-jmcomic-adapter
git commit -m "feat(P1): jmcomic 适配层 —— 搜索/详情/下载/导出/域名发现"
```

## 2. 提交信息

格式：`<类型>(P<阶段号>): <一句话祈使句>`

| 类型 | 用于 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复（**必须写清触发条件与后果**） |
| `docs` | 文档 / 参考表 |
| `refactor` | 不改行为的结构调整 |
| `test` | 只动测试 |
| `chore` | 依赖、配置、杂项 |

正文（可选）列出**做了什么 / 为什么 / 怎么验证**。

示例：

```
fix(P1): jmcomic 的 dir_rule.rule 分隔符与写法混用会产出字面量目录

分隔符为「/」时按「/」切段，首段 Bd_Atitle 既不等于 Bd 也不以 A/P 开头，
落到 f-string 解析器被原样输出 → 生成字面量目录 Bd_Atitle，
Convert-JmComicToCbz.ps1 会把所有章节误归到同名系列下（静默污染库）。

已改为 Bd/Atitle/Pname，并用 tests/test_dirrule.py 做渲染级回归断言。
```

## 3. 提交粒度

- 一个提交只做**一件事**；不顺手格式化无关文件。
- 改了行为就同时改测试；**测试与实现同一次提交**。
- 修 bug 先写能复现的测试，再改实现（红 → 绿）。
- 文档与实现不一致时，以**实测**为准，并同步改文档（同一个提交里改完）。

## 4. 每阶段收口检查

```powershell
uv pip install -e ".[dev]"
uv run pytest                     # 全绿
uv run manga doctor               # 环境体检全 ok
git status --short                # 干净（除阶段收口提交本身）
```

阶段收口提交的信息里写清**验收标准**是否达成（见计划文档的验收清单）。

## 5. 不入库的东西

| 内容 | 原因 |
|---|---|
| `.venv/` `.uv-cache/` | 环境 |
| `.test-tmp/` `.pytest-tmp/` | 沙箱内 `%TEMP%` 不可写，临时目录必须落在工作区内 |
| `*.log` | jmcomic 把日志固定写在「当前工作目录」，下完记得清理 |
| `.export-snapshot.json` | 红线守卫的本机快照 |
| 实机凭据（Cookie / 账号密码） | 只走环境变量，不进仓库 |
| 灰色来源的产物 | 不进仓库、不进共享区 |

## 6. 与 manga 工作区的边界

本仓库只做**采集侧**；书库侧脚本在 manga 工作区。改流水线脚本 → 在那边改，本仓库只同步参考表。
详见 [docs/workspace-dependency.md](docs/workspace-dependency.md)。
