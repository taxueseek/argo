# local-seek — 本地高效搜索（v1.2.0）

搜本地文件与内容的第一入口。核心承诺：**工具输出即答案，模型零探索成本**。
与 Argo（联网搜索）互补：Argo 搜网络，本 skill 搜本机。

## 三层渐进式策略（token 纪律的核心）

不要一上来就读文件。按层递进，每层输出都是压缩过的：

| 层 | 干什么 | 命令 | 输出量 |
|----|--------|------|--------|
| L1 定位 | 找到「在哪个文件」 | seek.py "词" --count 或 --filename | 每文件一行 |
| L2 上下文 | 看命中位置附近 | seek.py "词" --context 2 | 每命中 3 行 |
| L3 精读 | 读关键段落 | read_file 按行号局部读取 | 按需 |

任何一次搜索，先想清楚该停在哪一层。默认只做 L1。

## 统一入口

所有搜索走本 skill 内 `scripts/seek.py`（相对 argo 根：`sub-skills/local-seek/scripts/seek.py`），
让它决定路由，不要自己拼 rg/fd 参数：

```bash
# 以下以 argo 安装根为 cwd；或写死 $ARGO_HOME/sub-skills/local-seek/scripts/seek.py
python3 sub-skills/local-seek/scripts/seek.py "查询词"                 # 默认：当前目录全文
python3 sub-skills/local-seek/scripts/seek.py "查询词" --path ~/notes   # 指定目录
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
- **中文扩展**：中文查询自动「精确优先、扩展兜底」，精确无命中时按 2-gram
  放宽（如「数据抓取」放宽到「数据」「抓取」），提升召回；--exact 关闭。
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
| 搜 PDF/邮件/已归档内容 | --spotlight（mdfind） | 走 macOS 系统索引，零成本预建 |
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
7. **中文搜索**：rg 原生支持 UTF-8，直接搜中文；长中文词自动扩展召回，
   精确命中优先。
8. **先结构后正文**：大文件先 --outline 看结构，再 --lines N-M 按需读取，
   禁止直接 read_file 整个文件。
9. **结构搜索按语义**：找「是不是有裸 except」这类问题用 --structural，
   不要手写正则碰运气；可用语义清单见 references/structural-search.md。
10. **git 只读标题**：--git-log 是低成本路径，要 diff 再手动 git show，
    不让 seek.py 输出正文。

## 配置

排除规则与知识域：`sub-skills/local-seek/config/domains.yaml`
查看当前规则：`python3 sub-skills/local-seek/scripts/seek.py --domains`

## 参考

按场景的完整命令配方（进阶，非常规操作再读）：

- references/rg-recipes.md — rg 分场景配方（函数定义/跨文件引用/多词/正则）
- references/fd-mdfind-recipes.md — fd 与 Spotlight 配方
- references/strategies.md — 渐进式策略与 token 纪律详解
- references/structural-search.md — 结构搜索手册（语义规则表/别名/扩展新规则）
- references/git-integration.md — git 联动用法、场景与边界
- references/remote-handoff.md — 本地搜不到时的扩大与联网交接清单
