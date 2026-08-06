# fd 与 Spotlight（mdfind）配方

## fd — 按文件名查找

统一入口用 `seek.py "词" --filename`，手动配方：

```bash
# 文件名模糊匹配（忽略大小写）
fd "login"

# 指定目录
fd "auth" ~/src

# 限定扩展名
fd -e py "schema"
fd -e md -e txt "notes"

# 只看目录
fd -t d "config"

# 隐藏文件
fd -H ".*rc"
```

注意：fd 默认自动排除 .gitignore 中的内容，无需手动加 -E。

## mdfind — macOS Spotlight 全盘兜底

用系统预建索引，覆盖 rg 搜不到的东西（PDF 正文、邮件、已归档目录、图片元数据）。
统一入口用 `seek.py "词" --spotlight`。

```bash
# 全盘搜索（走系统索引，毫秒级）
mdfind "关键词"

# 限定目录
mdfind -onlyin ~/Documents "关键词"

# 限定文件名
mdfind -name "report"

# 文件名+内容混合（kMDItem 查询语法）
mdfind "kMDItemFSName == '*.pdf' && 关键词"

# 只看最近修改
mdfind "kMDItemFSContentChangeDate >= $time.today(-7) 关键词"
```

### 何时用 mdfind 而不是 rg

| 信号 | 用 |
|------|----|
| 全盘搜，不知道文件在哪 | mdfind |
| 搜 PDF/邮件/已归档内容 | mdfind |
| 目录未建立 Spotlight 索引（外接盘/网络盘） | 退回 rg + 明确 --path |
| 代码仓库内 | rg（永远优先） |

### 坑

- 外接盘、~/Library 部分目录可能未索引，mdfind 搜不到
- mdfind 的查询是「关键词匹配」，中文分词效果取决于系统
- 结果不带行号，定位到文件后需再 rg 找行
