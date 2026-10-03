---
name: local-seek
version: 1.4.0
description: 本机文件与代码内容搜索（rg/fd/mdfind/grep 统一入口 seek.py，工具输出即答案）。与 argo 联网搜索互补：搜本机目录、代码符号、文件名、文档全文、文件结构。触发：搜本地、找文件、找代码、本机搜 xx、代码在哪、统计本地命中。
---

# local-seek — 本地高效搜索（v1.4.0）

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

所有搜索都走本技能里的 `scripts/seek.py`（相对 argo 根：`sub-skills/local-seek/scripts/seek.py`），
由它决定用哪个工具，不要自己拼 rg/fd 参数：

```bash
# 以下以 argo 安装根为 cwd；或写死 $ARGO_HOME/sub-skills/local-seek/scripts/seek.py
python3 sub-skills/local-seek/scripts/seek.py "查询词"                 # 默认：当前目录全文
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes   # 指定目录
python3 sub-skills/local-seek/scripts/seek.py "词" --path ~/.agents --dot  # 连以 . 开头的目录和软链一起搜；搜 ~/.agents、~/.zcode 时要加（Spotlight 收不到这类目录，--dot 对 --spotlight 没用）
python3 sub-skills/local-seek/scripts/seek.py "词" --path . --include-noise  # repos/、tests/、tmp/、日期归档目录与真源平权（默认这些排在真源之后，但**仍然搜得到**）
python3 sub-skills/local-seek/scripts/seek.py "查询词" --count          # 先数命中，再决定是否深入
python3 sub-skills/local-seek/scripts/seek.py "查询词" --filename       # 按文件名
python3 sub-skills/local-seek/scripts/seek.py "查询词" --spotlight      # 全盘兜底（PDF/邮件/笔记）
python3 sub-skills/local-seek/scripts/seek.py "查询词" --type py,ts
python3 sub-skills/local-seek/scripts/seek.py "查询词" --json           # 结构化输出
python3 sub-skills/local-seek/scripts/seek.py "查询词" --exact          # 关闭中文扩展，精确匹配
python3 sub-skills/local-seek/scripts/seek.py "查询词" --exclude 某文件 # 额外排除（可重复）
python3 sub-skills/local-seek/scripts/seek.py --outline 文件路径        # 文件结构（def/class/标题/顶层key）
python3 sub-skills/local-seek/scripts/seek.py --lines 10-50 文件路径    # 按行读取，替代 read_file 全文
python3 sub-skills/local-seek/scripts/seek.py "裸except" --structural   # 结构搜索（空catch/裸except/装饰函数等）
python3 sub-skills/local-seek/scripts/seek.py --git-log 文件路径        # 文件的最近提交历史
python3 sub-skills/local-seek/scripts/seek.py --git-blame 12 文件路径   # 第 12 行的提交归属
```

内置智能行为（无需手动指定）：

- **固定字符串**：查询是纯字面量时自动用 rg -F；含 regex 元字符但解析失败
  （如 interface{}）自动回退 -F。
- **中文扩展**：中文查询先按原词精确匹配，没命中再拆成 2 字词（2-gram）放宽
  （如「数据抓取」放宽到「数据」「抓取」），这样能多搜到一些；--exact 关闭。
- **PCRE2 检测**：查询含 look-around 时检查本机 rg 是否支持，不支持给出
  明确提示而非报错。
- **结构搜索**：--structural 按语义检索代码模式（裸 except、空 catch、
  装饰函数、函数/类定义），中英文别名都认，零安装（rg -U 多行实现）。
- **Git 联动**：--git-log / --git-blame 直接回答「这行谁改的、最近动过什么」，
  文件不在仓库或未跟踪时给出明确区分，不报错。

## 工具选择（何时换工具）

| 场景 | 用 | 为什么 |
|------|-----|--------|
| 搜代码/正文关键词 | rg（默认） | 毫秒级，尊重 .gitignore，自动排除 node_modules 等 |
| 只记得文件名 | --filename（fd） | 按文件名模糊匹配 |
| 搜 PDF/邮件/已归档内容 | --spotlight（mdfind） | 用 macOS 已经建好的系统索引，不额外花时间（Windows/Linux 上没有，改用 rg 搜正文） |
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
7. **中文搜索**：rg 原生支持 UTF-8，直接搜中文；长中文词会自动放宽匹配，
   多搜到一些，原词命中优先。
8. **先结构后正文**：大文件先 --outline 看结构，再 --lines N-M 按需读取，
   禁止直接 read_file 整个文件。
9. **结构搜索按语义**：找「是不是有裸 except」这类问题用 --structural，
   不要手写正则碰运气；可用语义清单见 references/structural-search.md。
10. **git 只读标题**：--git-log 只给标题，够用就停；要看具体改动再手动 git show，
    不让 seek.py 输出整段正文。

## 配置

排除规则与知识域：`sub-skills/local-seek/config/domains.yaml`
查看当前规则：`python3 sub-skills/local-seek/scripts/seek.py --domains`

## 参考

按场景的完整命令配方（进阶，非常规操作再读）：

- references/rg-recipes.md — rg 分场景配方（函数定义/跨文件引用/多词/正则）
- references/fd-mdfind-recipes.md — fd 与 Spotlight 配方
- references/strategies.md — 三层做法的详细说明，以及怎么少花 token
- references/structural-search.md — 结构搜索手册（语义规则表/别名/扩展新规则）
- references/git-integration.md — git 联动用法、场景与边界
- references/remote-handoff.md — 本地搜不到时的扩大与联网交接清单
