---
name: local-seek
version: 1.6.0
description: 本机文件与代码内容搜索（rg/fd/mdfind/grep 统一入口 seek.py，工具输出即答案）。与 argo 联网搜索互补：搜本机目录、代码符号、文件名、文档全文、文件结构。触发：搜本地、找文件、找代码、本机搜 xx、代码在哪、统计本地命中。
---

# local-seek — 本地高效搜索（v1.6.0）

搜本机文件和内容就从这里开始。**工具的输出就是答案**，不用再自己翻一遍。
与 Argo（联网搜索）分工：Argo 搜网络，本技能搜本机。

## 三层递进：先定位、再看上下文、最后精读

不要一上来就读文件。一层层往里走，每层的结果都不多：

| 层 | 干什么 | 命令 | 输出量 |
|----|--------|------|--------|
| L1 定位 | 找到「在哪个文件」 | seek.py "词" --count 或 --filename | 每文件一行 |
| L2 上下文 | 看命中位置附近 | seek.py "词" --context 2 | 每命中 3 行 |
| L3 精读 | 读关键段落 | read_file 按行号局部读取 | 按需 |

每次搜索先想清楚停在哪一层，默认只做第一层。

## 统一入口

统一走 `scripts/seek.py`（相对 argo 根：`sub-skills/local-seek/scripts/seek.py`），
由它选工具，不要自己拼 rg/fd 参数：

```bash
# 以下以 argo 安装根为 cwd；或写死 $ARGO_HOME/sub-skills/local-seek/scripts/seek.py
python3 sub-skills/local-seek/scripts/seek.py "查询词"                 # 默认：当前目录全文
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes   # 指定目录
python3 sub-skills/local-seek/scripts/seek.py "词" --path ~/.agents --dot  # 连 . 开头目录与软链一起搜
python3 sub-skills/local-seek/scripts/seek.py "词" --path . --include-noise  # 噪声目录与真源平权
python3 sub-skills/local-seek/scripts/seek.py "查询词" --count          # 先数命中数
python3 sub-skills/local-seek/scripts/seek.py "查询词" --filename       # 按文件名
python3 sub-skills/local-seek/scripts/seek.py "查询词" --spotlight      # 全盘兜底
python3 sub-skills/local-seek/scripts/seek.py "查询词" --type py,ts
python3 sub-skills/local-seek/scripts/seek.py "查询词" --json           # 结构化输出
python3 sub-skills/local-seek/scripts/seek.py "查询词" --exact          # 精确匹配（关扩展）
python3 sub-skills/local-seek/scripts/seek.py "符号名" --symbol          # 精确符号/词边界匹配
python3 sub-skills/local-seek/scripts/seek.py "查询词" --exclude 某文件 # 额外排除（可重复）
python3 sub-skills/local-seek/scripts/seek.py --outline 文件路径        # 文件结构（def/class/标题/顶层key）
python3 sub-skills/local-seek/scripts/seek.py --lines 10-50 文件路径    # 按行读取，替代 read_file 全文
python3 sub-skills/local-seek/scripts/seek.py "裸except" --structural   # 结构搜索（空catch/裸except/装饰函数等）
python3 sub-skills/local-seek/scripts/seek.py --git-log 文件路径        # 文件的最近提交历史
python3 sub-skills/local-seek/scripts/seek.py --git-blame 12 文件路径   # 第 12 行的提交归属
```

内置智能行为（固定字符串回退、中文 2-gram 扩展、PCRE2 检测、结构搜索、
git 联动等）自动生效，明细见 references/auto-behaviors.md。

## 工具选择（何时换工具）

| 场景 | 用 | 为什么 |
|------|-----|--------|
| 搜代码/正文关键词 | rg（默认） | 毫秒级，尊重 .gitignore，自动排除 node_modules 等 |
| 只记得文件名 | --filename（fd） | 按文件名模糊匹配 |
| 搜 PDF/邮件/已归档内容 | --spotlight（mdfind） | 用系统已建好的索引，不额外花时间（Windows/Linux 无此工具，改用 rg） |
| 找代码模式（裸except/空catch等） | --structural | 按语义不按字符串 |
| 追文件历史/单行归属 | --git-log / --git-blame | 免开终端敲 git |
| 当前目录搜不到 | 先扩大 --path，再 --spotlight | 先窄后宽 |
| 大仓库担心输出爆炸 | --count + --max 20 | 先看分布再深入 |

## 执行纪律

1. **先窄后宽**：先限定目录/扩展名，搜不到再扩大。禁止一开始就全盘扫。
2. **先数后看**：--count 看分布，--context 0（默认）看命中行，最后才读文件。
3. **指定类型**：代码场景加 --type（py,ts,go…），文档场景用 --scope doc。
4. **绝对路径优先**：涉及读文件时用 read_file + 绝对路径。
5. **不读整个文件**：L3 精读用行号偏移局部读取，命中上下文不够再扩。
6. **结果为空先换词**：换同义词/拆词/去大小写，再换工具（fd→mdfind），
   不要重复同一查询。
7. **先结构后正文**：大文件先 --outline 看结构，再 --lines N-M 按需读取，
   禁止直接 read_file 整个文件。
8. **结构搜索按语义**：找「是不是有裸 except」这类问题用 --structural，
   不要手写正则碰运气；可用语义清单见 references/structural-search.md。
9. **git 只读标题**：--git-log 只给标题，够用就停；要看具体改动再手动 git show。

## 排序与性能（v1.6.0）

内容命中按 **BM25F 相关度重排**（实现文件不再被文档顺带提及淹没），池子按
**文件轮转配额**（每文件 ≤8 行）；文件名场景用 **fzf FuzzyMatchV2** 评分。
确定性不变：与 rg 发射序无关，并列分按 (路径,行号)。

大树上的内容搜索可开 **FTS5 索引快路**：`ARGO_LOCAL_FTS=1`（默认关），
首次对该目录先跑 `scripts/seek_fts.py rebuild <目录>` 预热，之后毫秒级返回；
rg 永远兜底（索引未建成/带正则/零命中时自动回退）。明细见
references/auto-behaviors.md。

逃生门（默认行为已是最优，仅在回退/实验时用）：
- `ARGO_SEEK_CONTENT_RERANK=0` 关闭内容重排，恢复 (路径,行号) 原序
- `ARGO_SEEK_FRECENCY=1` 开启访问频次加成（实验项，默认关）
- 排序质量用 `scripts/eval_rank.py` 评测（MRR/耗时，支持 --compare）

## 配置

排除规则与知识域：`sub-skills/local-seek/config/domains.yaml`
查看当前规则：`python3 sub-skills/local-seek/scripts/seek.py --domains`

## 参考

按场景的完整命令配方（进阶，非常规操作再读）：

- references/rg-recipes.md — rg 分场景配方（函数定义/跨文件引用/多词/正则）
- references/auto-behaviors.md — 自动行为明细（重排/FTS/中文扩展/PCRE2/git）
- references/fd-mdfind-recipes.md — fd 与 Spotlight 配方
- references/strategies.md — 三层做法的详细说明，以及怎么少花 token
- references/structural-search.md — 结构搜索手册（语义规则表/别名/扩展新规则）
- references/git-integration.md — git 联动用法、场景与边界
- references/remote-handoff.md — 本地搜不到时的扩大与联网交接清单
