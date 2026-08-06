# Git 联动手册（--git-log / --git-blame）

本地搜索找到文件只是第一步，经常要接着回答「这行谁改的、为什么改、什么时候
改的」。git 联动让 seek.py 直接给出答案，不用再开终端敲 git。

## 用法

```bash
# 文件的最近提交历史（默认 10 条，oneline）
python3 ~/.agents/skills/argo/sub-skills/local-seek/scripts/seek.py --git-log 文件路径

# 某一行是谁在哪个提交改的
python3 ~/.agents/skills/argo/sub-skills/local-seek/scripts/seek.py --git-blame 12 文件路径
```

文件路径作为位置参数（或 `--path`），两者互斥时位置参数优先。

## 输出示例

```text
# --git-log
local-seek: seek.py 最近 3 条提交
cd02e96 补全结构搜索与 git 联动
a1b2c3d 中文扩展：空格噪音修复

# --git-blame 12
local-seek: seek.py 第 12 行
cd02e96f (taxueseek 2026-07-03 20:58:20 +0800 3)  ap.add_argument("--git-blame"...
```

## 适用场景

| 场景 | 命令 | 读什么 |
|------|------|--------|
| 这文件最近在动什么 | --git-log | 提交标题序列 |
| 某行代码的来历 | --git-blame N | 提交哈希、作者、时间 |
| 拿到哈希后看完整改动 | --git-log 后再 `git -C 目录 show 哈希` | diff 正文 |
| 谁改坏了某段逻辑 | --git-blame 定位行 → show 对应提交 | 上下文 |

## 边界与局限

- **未跟踪文件**：untracked（从未 git add）的文件无历史，报「无提交历史」，
  不是错误。把文件 git add 后即有。
- **仓库外文件**：不在任何 git 仓库内的文件报「不在 git 仓库中」。
- **行号漂移**：blame 按当前行号定位，改动过的文件行号会移动，属正常。
- **大文件历史**：--git-log 只读标题不看 diff，是低成本路径；要 diff 再手动
  git show，不要指望 seek.py 输出正文。
- **子模块/符号链接**：git -C 父目录 对符号链接指向的仓库无效，需先解析真实路径。

## 与本地搜索的组合套路

1. 搜到文件里的目标行：`seek.py "关键词" --path ~/repo --max 5`
2. 定位关键行号后 blame：`seek.py --git-blame 行号 文件路径`
3. 拿提交哈希再看 diff：`git -C ~/repo show 哈希`
