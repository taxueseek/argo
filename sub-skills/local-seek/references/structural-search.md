# 结构搜索手册（--structural）

按「语义」而非「字符串」检索代码模式：找裸 except、空 catch、装饰函数这类
「看着就知道有问题/有特征」的结构。零安装实现：rg -U 多行匹配 + 语言感知
pattern，本机装 ast-grep 后可升级为 AST 精确匹配（见文末）。

## 用法

```bash
# 按语义名或中文别名检索，输出同普通搜索（路径:行号:片段）
python3 ~/.agents/skills/argo/sub-skills/local-seek/scripts/seek.py "裸except" --structural
python3 ~/.agents/skills/argo/sub-skills/local-seek/scripts/seek.py "empty-catch" --structural --path ~/repo
python3 ~/.agents/skills/argo/sub-skills/local-seek/scripts/seek.py "函数定义" --structural --path ~/repo --type py
python3 ~/.agents/skills/argo/sub-skills/local-seek/scripts/seek.py "不存在语义" --structural   # 报可用列表
```

查询词可写语义名（empty-catch）或任意中文别名（空catch、裸异常），大小写不敏感。

## 内置规则

| 语义名 | 中文别名 | 查什么 | 覆盖语言 |
|--------|---------|--------|---------|
| empty-catch | 空catch、空捕获、空异常处理 | `catch (...) {}` 空体 | js/jsx/ts/tsx |
| bare-except | 裸except、无类型except | `except:` 无异常类型 | py |
| unwrapped-error | 错误未包装、裸return err | `return err` 直接返回错误 | go |
| decorated-fn | 装饰函数、装饰器函数 | 带 `@decorator` 的函数定义 | py |
| function-def | 函数定义、找函数 | 函数定义行 | py/js/ts/go/rs |
| class-def | 类定义、找类 | 类/结构体定义行 | py/js/ts/go/java |

## 实现说明

- 每条规则 = 若干 (扩展名集合, 正则) 对，逐语言跑 `rg -U -i --line-number`。
- `-U` 启用多行模式，decorated-fn 才能跨行匹配 `@装饰器\n def`。
- 语言无命中（rg 退出码 1）静默跳过，不报错。
- 与正文搜索共用排除规则（domains.yaml + --exclude）。
- 局限：正则近似，非真实 AST。缩进风格怪异的代码、嵌套场景可能漏报；
  `catch {}` 内只有注释也算空体（正则按字面空体判断）。

## 扩展新规则

在 seek.py 的 `STRUCTURAL_RULES` 字典追加一条：

```python
"todo-count": {
    "aliases": ("待办统计", "todo盘点"),
    "patterns": [
        (("py", "js", "ts"), r"TODO|FIXME|HACK"),
    ],
},
```

规则键即语义名，aliases 提供中文入口，patterns 按语言给正则。
加完跑 `python3 scripts/eval_seek.py` 确认无回归，再补一条结构用例。

## 升级路径（装 ast-grep 后）

ast-grep 能精确匹配 AST 节点（真·空 catch 块、未捕获的裸 except），
消除正则的漏报误报。届时在 structural_search 里检测 `sg` 命令存在则优先
AST，缺失时回落当前 rg -U 实现，行为对调用方透明。
