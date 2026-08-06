# rg 分场景配方

统一入口 `seek.py` 已覆盖多数场景，以下配方用于非常规操作或手动微调。
所有命令均尊重 .gitignore，自动排除 node_modules 等（见 config/domains.yaml）。

## 基础

```bash
# 全文搜（当前目录）
rg "关键词"

# 指定目录
rg "关键词" ~/notes

# 忽略大小写
rg -i "keyword"

# 正则
rg "func[0-9]+\(" src/

# 固定字符串（避免正则转义，含特殊字符时用）
rg -F "a.b[c]" .
```

## 定位类

```bash
# 只看文件名（最省 token，先看分布）
rg -l "关键词" .
rg --files-with-matches "关键词" .

# 每文件命中数（先数后看）
rg --count-matches "关键词" .

# 只看行号不输出内容
rg -l --line-number "关键词" .
```

## 上下文类

```bash
# 前后 N 行
rg -C 2 "关键词" .
rg -A 3 "关键词" .   # 只后文
rg -B 1 "关键词" .   # 只前文

# 输出到文件避免刷屏（超大批量时）
rg "关键词" . > /tmp/rg_out.txt
```

## 类型过滤

```bash
# 按扩展名
rg -g "*.py" "关键词" .
rg -g "*.{py,ts,js}" "关键词" .

# 排除
rg -g "!test_*" "关键词" .
rg -g "!*.min.js" "关键词" .
```

## 常用语义场景

```bash
# 函数/类定义在哪
rg -n "def handle_request|class AuthService" src/
# 跨语言：rg "function (getUser|setUser)" .

# 跨文件引用（谁在调用某函数）
rg "send_email\(" .

# TODO/FIXME 盘点
rg -n "TODO|FIXME|HACK" --type-add 'todo:*.{py,js,ts,go,rs}' -t todo .

# 只搜中文
rg -n "[\u4e00-\u9fff]" .

# 找 JSON/YAML 里的键
rg -n '"max_results"' config/

# 日志排查（最近日志）
rg "ERROR|panic" --glob "*.log" .
```

## 性能

- 大仓库先 `--count` 或 `-l`，避免输出千行
- 指定 `-g` 类型或目录，rg 只扫必要范围
- 超过 30s 无结果：换词或换 mdfind（--spotlight），不要重复等待
- rg 退出码：0=有匹配，1=无匹配，2=报错。脚本已处理，手动用时注意

## 非代码文件（可选扩展）

rga（ripgrep-all）未安装时可临时用 mdfind 覆盖 PDF/Office 场景；
若常搜这类文件，装 rga：`brew install ripgrep-all`
