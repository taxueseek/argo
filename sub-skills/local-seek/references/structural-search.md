# 结构搜索手册（--structural）

按「语义」而非「字符串」检索代码模式：找裸 except、空 catch、装饰函数这类
「看着就知道有问题/有特征」的结构。本机装了 ast-grep（`brew install ast-grep`）
时走 AST 精确匹配；未装则整条回落 rg -U 多行正则（实现见文末）。

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

- 规则表与检索实现都在 `seek_structural.py`（从 seek.py 拆出，避 1000 行门禁）。
- **AST 路径**：本机有 `ast-grep`/`sg` 时逐 (扩展名, pattern) 跑
  `ast-grep run -p <pat> --json=stream --lang <LANG>`；命中按规范化签名去重。
- **rg 回落**：缺工具时每条规则 = 若干 (扩展名集合, 正则) 对，逐语言跑
  `rg -U -i --line-number`。`-U` 启用多行模式，decorated-fn 才能跨行匹配。
- 两条路径共用排除规则（domains.yaml + --exclude）；与正文搜索同口径。
- 局限：rg 回落是正则近似，缩进风格怪异的代码、嵌套场景可能漏报；
  `catch {}` 内只有注释也算空体（正则按字面空体判断）。装了 ast-grep 即消除。

## 扩展新规则

在 `seek_structural.py` 的 `STRUCTURAL_RULES` 字典追加一条（rg 回落是必填项）：

```python
"todo-count": {
    "aliases": ("待办统计", "todo盘点"),
    "patterns": [
        (("py", "js", "ts"), r"TODO|FIXME|HACK"),
    ],
    "ast_scope": "none",   # 纯文本特征，无 AST 模式可写
    "ast_patterns": [],
},
```

规则键即语义名，aliases 提供中文入口，patterns 按语言给正则。有等价 AST
节点时把 `ast_scope` 改成 `full`/`partial` 并补 `ast_patterns`（partial 必须
写 `why_rg`）。加完跑 `python3 sub-skills/local-seek/scripts/eval_seek.py`
确认无回归，再补一条结构用例。

## 升级路径（已实现，2026-11-26）

ast-grep 能精确匹配 AST 节点（真·空 catch 块、未捕获的裸 except），
消除正则的漏报误报。**已落地**：`seek_structural.py` 检测 `ast-grep`/`sg`
存在则走 AST，缺失时整条回落当前 rg -U 实现，行为对调用方透明；引擎标注随
后端切换（`astgrep-structural` / `rg-structural`），避免把 AST 的精确结果误读
成正则近似结果。

### 实测踩到的四个坑（改这块前先读）

1. **接口漂移**：`sg scan --pattern` 在 ast-grep 0.51.x 已被移除，实测报
   `unexpected argument '--pattern'`。正确入口是
   `ast-grep run -p <pat> --json=stream --lang <LANG>`。按旧文档实现会让装了
   新版的机器静默退回正则近似——能力降级且无提示。
2. **`--lang` 必须显式给**：省略时按扩展名推断，`.tsx` 会被当 `.ts`，含 JSX
   的文件零命中（实测）。故按扩展名硬映射到语言名，不依赖推断。
3. **退出码不能判命中**：`run --json=stream` 零命中也返回 0；非 0 只代表该
   语言扫描失败，须跳过而不是当成「无结果」。
4. **两条路径的行号与表述口径不同**：AST 从 0 起、报整段首行（不含 export）；
   rg `-U` 逐行给前缀且各自行号。按行号去重会在合并处留下成对重复，故统一按
   规范化签名（剥掉 async/export/pub 等修饰符后的定义头）去重。

### 规则表怎么加 AST 覆盖

`STRUCTURAL_RULES` 每条含 `patterns`（rg 回落）、`ast_patterns`（AST）、
`ast_scope`：

- `full`：AST 独立成路，不调 rg；
- `partial`：AST 与 rg 结果去重合并，此时 `why_rg` **必须**写明 rg 兜的缺口；
- `none`：无 AST 模式，纯 rg。

`ast_patterns` 条目可为 `(exts, pattern)`、`(exts, pattern, strictness)` 或
`(exts, pattern, strictness, {"not_any_regex": (...)})`：

- `strictness="cst"` 用于字面空体（`catch ($B) {}` 只命中真无内容，注释体不算）；
- `not_any_regex` 用于宽 pattern 的排除（`return $ERR` 会连
  `fmt.Errorf(...)`/`errors.New(...)` 一起命中，按规则声明排除）。
