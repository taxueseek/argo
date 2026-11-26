#!/usr/bin/env python3
"""seek_structural.py — 结构搜索的规则表与 ast-grep 精确检索（AST 优先，rg 回落）。

## 为什么要拆出来

seek.py 已 1874 行，受 `tests/test_module_size_gate.py` 的 1000 行硬上限管辖
（它不在祖父清单里）。结构搜索原来直接内嵌在 seek.py 的 `STRUCTURAL_RULES`
+ `structural_search`：规则表随语义扩展只会更长，再往里塞 ast-grep 模式表必然
撞门。拆出后 seek.py 只留 re-export 壳子保持既有调用与测试签名不变，本模块专管
「一条语义规则怎么落到 AST 或正则」。

## 两条实现路径

- **ast-grep（本机装有 `ast-grep` 或 `sg` 时）**：按真实 AST 节点匹配，消除
  正则近似的漏报误报——`except:` 不会命中 `except Exception:`，`return err`
  不会命中 `return fmt.Errorf(...)`。
- **rg -U 多行正则（缺 ast-grep 时）**：与升级前逐字等价的回落路径，行为对调用方
  透明。外部工具缺失不得让能力消失，也不得静默改口径。

## 为什么 AST 表不全量替代正则表

AST 精确性有代价：每种语言的 pattern 都要按语法树写死，未覆盖的组合会「查了
但没报」——比漏报更坏。所以每条规则显式声明 `ast_scope`：`full` 表示 AST 覆盖
正则全部形态；`partial` 表示仍有缺口，此时 AST 与 rg 结果去重合并，rg 补缺口
（每条 `why_rg` 说明缺口在哪）。

## ast-grep CLI 契约（2026-11-26 实测 0.51.3）

- 正确入口是 `ast-grep run -p <pattern> --json=stream --lang <LANG>`；旧文档里的
  `sg scan --pattern` 已移除（实测报 unexpected argument）。
- `--json=stream` 每行一个 match 对象：`text` / `range.start.line` /
  `range.byteOffset.start` / `file` / `lines`。行号从 0 起。
- 零命中退出码仍为 0，故**不能**用退出码判有无结果，只看输出行数。
- `--no-ignore hidden` 对应 rg 的 `--hidden`；`--follow` 对应 `--follow`。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

# ast-grep 可执行名候选：官方 CLI 名为 ast-grep，社区惯用缩写 sg（新版会打
# deprecation 横幅，功能仍可用）。优先官方名。
_SG_CANDIDATES = ("ast-grep", "sg")

# 定义类结构的关键字与可前导的修饰符。用于把 ast-grep（报整段首行）与 rg
# （按行首锚定、含 export/async/pub）两种表述归一成同一签名后去重。
_DEF_KEYWORDS = {
    "def", "class", "function", "func", "fn", "type",
    "except", "catch", "try", "return",
}
_DEF_MODIFIERS = {
    "async", "export", "pub", "public", "private", "protected", "static",
    "default", "final", "abstract", "open", "inline", "extern",
}

# 扩展名 → ast-grep --lang。jsx 复用 js、pyi 复用 py 是实测可用的；tsx 必须
# 独立给 tsx，否则 `--lang ts` 对含 JSX 的文件零命中。
_EXT_TO_LANG = {
    "py": "py", "pyi": "py",
    "js": "js", "jsx": "js",
    "ts": "ts", "tsx": "tsx",
    "go": "go", "rs": "rs", "java": "java",
}


def truncate(text: str, n: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _resolve_sg() -> str | None:
    for name in _SG_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


_SG_PATH = _resolve_sg()


def sg_available() -> bool:
    """本机是否装有 ast-grep（决定走 AST 还是 rg 回落）。"""
    return _SG_PATH is not None


# ── 规则表 ─────────────────────────────────────────────────────────────
# patterns      ：rg 回落路径，(exts, regex) 列表
# ast_patterns  ：ast-grep 路径，(exts, pattern) 列表
# ast_scope     ：none = 无 AST 模式（纯 rg）；full = AST 独立成路；
#                 partial = AST 有缺口，与 rg 去重合并
# why_rg        ：partial 时说明 rg 兜的是什么缺口
STRUCTURAL_RULES: dict[str, dict[str, Any]] = {
    "empty-catch": {
        "aliases": ("empty catch", "空catch", "空catch块", "空捕获", "空异常处理"),
        "patterns": [
            (("js", "jsx", "ts", "tsx"), r"catch\s*\([^)]*\)\s*\{\s*\}"),
        ],
        "ast_scope": "partial",
        # 字面空体 `{}` + `--strictness cst`：只命中真无内容的 catch（实测
        # ec4.js 的注释体不命中）；rg 侧的正则按字面空体判，故两者互补
        "ast_patterns": [
            (("js", "jsx", "ts", "tsx"), "try { $$A } catch ($B) {}", "cst"),
        ],
        "why_rg": "AST 的字面空体不含「只有注释」的 catch；rg 补这一形态",
    },
    "bare-except": {
        "aliases": ("bare except", "裸except", "裸异常", "无类型except"),
        "patterns": [
            (("py",), r"except\s*:"),
        ],
        "ast_scope": "full",
        "ast_patterns": [
            (("py",), "except:\n  $$$BODY"),
        ],
    },
    "unwrapped-error": {
        "aliases": ("unwrapped error", "错误未包装", "裸错误返回", "裸return err"),
        "patterns": [
            (("go",), r"return\s+err\b"),
        ],
        "ast_scope": "full",
        # `return $ERR` 会连 `return fmt.Errorf(...)` / `errors.New(...)` 一起命中，
        # 用 `not: regex` 排除包装错误的构造（实测 ue.go 只留 return err 与 nil, err）
        "ast_patterns": [
            (("go",), "return $ERR", None, {"not_any_regex": ("Errorf", r"errors\.New")}),
        ],
    },
    "decorated-fn": {
        "aliases": ("decorated function", "装饰函数", "装饰器函数", "decorator"),
        "patterns": [
            (("py",), r"^\s*@\w[\w.]*\s*\n\s*(async\s+)?def\s+"),
        ],
        "ast_scope": "partial",
        "ast_patterns": [
            (("py",), "@$DECORATOR\nasync def $NAME($$$PARAMS):\n  $$$BODY"),
            (("py",), "@$DECORATOR\ndef $NAME($$$PARAMS):\n  $$$BODY"),
        ],
        "why_rg": "AST 要求装饰器紧贴函数定义；rg 补单装饰器写法与 async 变体",
    },
    "function-def": {
        "aliases": ("function definition", "函数定义", "找函数"),
        "patterns": [
            (("py", "pyi"), r"^\s*(async\s+)?def\s+\w+"),
            (("js", "jsx", "ts", "tsx"), r"^\s*(export\s+)?(async\s+)?function\s+\w+"),
            (("go",), r"^\s*func\s+\w+"),
            (("rs",), r"^\s*(pub\s+)?fn\s+\w+"),
        ],
        "ast_scope": "partial",
        "ast_patterns": [
            (("py", "pyi"), "def $NAME($$$PARAMS):\n  $$$BODY"),
            (("py", "pyi"), "async def $NAME($$$PARAMS):\n  $$$BODY"),
            (("js", "jsx", "ts", "tsx"), "function $NAME($$$PARAMS) { $$$BODY }"),
            (("js", "jsx", "ts", "tsx"), "async function $NAME($$$PARAMS) { $$$BODY }"),
            (("go",), "func $NAME($$$PARAMS) $RET { $$$BODY }"),
            (("rs",), "fn $NAME($$$PARAMS) $RET { $$$BODY }"),
        ],
        "why_rg": "AST 要求显式返回类型（rs）与标准形参；rg 补 pub/无返回类型/箭头函数",
    },
    "class-def": {
        "aliases": ("class definition", "类定义", "找类"),
        "patterns": [
            (("py", "pyi"), r"^\s*class\s+\w+"),
            (("js", "jsx", "ts", "tsx"), r"^\s*(export\s+)?class\s+\w+"),
            (("go",), r"^\s*type\s+\w+\s+struct\b"),
            (("java",), r"^\s*(public|private|protected)?\s*class\s+\w+"),
        ],
        "ast_scope": "partial",
        "ast_patterns": [
            (("py", "pyi"), "class $NAME($$$BASE):\n  $$$BODY"),
            (("js", "jsx", "ts", "tsx"), "class $NAME extends $BASE { $$$BODY }"),
            (("js", "jsx", "ts", "tsx"), "class $NAME { $$$BODY }"),
            (("go",), "type $NAME struct { $$$FIELDS }"),
            (("java",), "class $NAME { $$$BODY }"),
        ],
        "why_rg": "AST 不吞访问修饰符（java public class）与部分继承写法",
    },
}

_STRUCT_ALIAS: dict[str, str] = {}
for _key, _rule in STRUCTURAL_RULES.items():
    _STRUCT_ALIAS[_key] = _key
    for _a in _rule["aliases"]:
        _STRUCT_ALIAS[_a] = _key


def resolve_rule(rule_name: str) -> str | None:
    """语义名/中文别名 → 规范规则键；未知返回 None。"""
    return _STRUCT_ALIAS.get(rule_name.strip().lower())


# ── 子进程出口 ─────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: float | None = 30.0):
    """薄封装 subprocess.run：超时/不可执行返回 None（与 seek.run 同口径）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError):
        return None


# ── rg 回落路径（缺 ast-grep 时；结构与升级前等价）─────────────────────
def _parse_rg_lines(stdout: str, max_results: int) -> list[tuple[str, int, str]]:
    """解析 rg -U 输出为 (path, line, snippet)。

    实测（rg 15.2）：`--line-number -U` 对多行匹配的**每一行都带
    `文件:行号:` 前缀，且各自给本行的行号**（不是整段首行）。这些续行是同一处
    结构的组成部分，若当成独立命中会让结果膨胀（实测 `\\n` 空续行、函数体
    各行都被报一遍）。

    口径：仅当「本行行号 == 上一条同文件命中的行号 + 1」时判为续行并丢弃；
    真正的下一处结构起始行与上一条不相邻（或跨文件），照常成条。
    """
    out: list[tuple[str, int, str]] = []
    for line in stdout.splitlines():
        m = re.match(r"^(.*?):(\d+):(.*)$", line)
        if not m:
            continue
        fp, ln, txt = m.group(1), int(m.group(2)), truncate(m.group(3))
        if out and out[-1][0] == fp and ln == out[-1][1] + 1:
            continue  # 上一处结构的续行，不新增条目
        if not txt:
            continue  # 空片段无检索价值
        out.append((fp, ln, txt))
        if len(out) >= max_results:
            break
    return out


def structural_search_rg(rule: dict[str, Any], path, excludes, max_results,
                         dots=False, timeout=None, runner=None):
    """rg -U 多行正则路径：与 ast-grep 升级前逐字等价。"""
    run_impl = runner or _run
    results: list[tuple[str, int, str]] = []
    for exts, pat in rule["patterns"]:
        cmd = ["rg", "--line-number", "--no-heading", "-U", "-i", "--color", "never"]
        if dots:
            cmd += ["--hidden", "--follow"]
        for ex in excludes:
            cmd += ["-g", f"!{ex}"]
        for e in exts:
            cmd += ["-g", f"*.{e}"]
        cmd += ["-e", pat, str(path)]
        proc = run_impl(cmd, timeout=timeout)
        if proc is None:
            continue
        if proc.returncode == 1:
            continue  # 该语言无命中
        # 返回码 2 = 有结果但过程报错（软链指向不存在文件），不当作无命中
        if proc.returncode not in (0, 2):
            continue
        results.extend(_parse_rg_lines(proc.stdout, max_results))
        if len(results) >= max_results:
            return results[:max_results]
    return results


# ── ast-grep 路径 ──────────────────────────────────────────────────────
def _invoke_sg(patterns: list[tuple[tuple[str, ...], str]], root, excludes,
               dots=False, timeout=None, runner=None) -> list[tuple[str, int, str]]:
    """跑 `ast-grep run -p ... --json=stream --lang ...`。

    按 (exts, pattern) 逐条调用：`--lang` 必须显式给，否则 ast-grep 按扩展名
    推断，tsx 会被当成 ts 而零命中（实测）。每个 exts 集合一次扫描，扫描根传目录。
    """
    sg = _SG_PATH
    if not sg:
        return []
    run_impl = runner or _run
    rows: list[tuple[str, int, str]] = []
    for spec in patterns:
        exts, pat = spec[0], spec[1]
        strictness = spec[2] if len(spec) > 2 else None
        lang = _EXT_TO_LANG.get(exts[0])
        if not lang:
            continue
        cmd = [sg, "run", "-p", pat, "--json=stream", "--lang", lang]
        if strictness:
            cmd += ["--strictness", strictness]
        if dots:
            cmd += ["--no-ignore", "hidden", "--follow"]
        for ex in excludes:
            cmd += ["--globs", f"!{ex}"]
        for e in exts:
            cmd += ["--globs", f"*.{e}"]
        cmd.append(str(root))
        proc = run_impl(cmd, timeout=timeout)
        if proc is None:
            continue
        # 退出码不可用于判命中：零命中也是 0；非 0 一律当该语言失败跳过
        if proc.returncode != 0:
            continue
        rows.extend(_parse_stream(proc.stdout, exclude=spec[3] if len(spec) > 3 else None))
    return _dedup_rows(rows)


def _dedup_rows(rows: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    """跨 pattern 去重：同一文件、同一首标识符的命中只留第一条。

    一条规则的多个 AST pattern 会同时命中同一节点（实测 `async def g():`
    被 `def $NAME...` 与 `async def $NAME...` 各命中一次），且 AST 的首行文本
    不含前导修饰符，与 rg 的表述不同——故按 (文件, 首标识符) 去重。
    """
    seen: set = set()
    out: list[tuple[str, int, str]] = []
    for fp, ln, txt in rows:
        token = _first_token(txt)
        key = (os.path.abspath(os.path.expanduser(fp)), token if token else ln)
        if key in seen or not txt.strip():
            continue
        seen.add(key)
        out.append((fp, ln, txt))
    return out


def _parse_stream(stdout: str, exclude: dict | None = None) -> list[tuple[str, int, str]]:
    """解析 `--json=stream`：每行一个 match，取 (path, start_line, first_line_text)。

    行号从 0 起，对外 +1 转成 rg/编辑器的 1 基。snippet 只取首行：输出契约是
    「路径:行号:片段」，多行会让 format_output 的逐行渲染错位（实测缺陷）。

    `exclude` 支持 `{"not_any_regex": (re, ...)}`：命中文本命中任一正则即丢弃。
    用于 `return $ERR` 这类会把 `fmt.Errorf(...)` / `errors.New(...)` 一并匹配的
    宽 pattern——按规则表声明排除，而不是让调用方各自过滤。
    """
    pats = tuple(re.compile(r) for r in (exclude or {}).get("not_any_regex", ()))
    rows: list[tuple[str, int, str]] = []
    for chunk in stdout.splitlines():
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        rng = obj.get("range")
        st = rng.get("start") if isinstance(rng, dict) else None
        if not isinstance(st, dict):
            continue
        text = obj.get("text")
        if not isinstance(text, str):
            continue
        fp = obj.get("file")
        if not isinstance(fp, str) or not fp:
            continue
        if any(p.search(text) for p in pats):
            continue
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        rows.append((fp, int(st.get("line", 0)) + 1, truncate(first_line)))
    return rows


def _norm_sig(txt: str) -> str:
    """把一处结构的片段归一成可跨实现比对的签名。

    两条路径的表述差异全在**前导修饰符**上：ast-grep 报 `function k(){}`，
    rg 的正则按行首锚定报 `export function k(){}`。剥掉 as/export/async/pub/
    default 这类前缀后两者一致，续行与空行因不含定义关键字而被丢弃。

    取不到定义关键字时返回 ""，调用方据此丢弃该条——那不是一处结构定义。
    """
    body = (txt or "").strip()
    if not body:
        return ""
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[^\sA-Za-z0-9_]+", body)
    if not toks:
        return ""
    i = 0
    while i < len(toks) and toks[i].lower() in _DEF_MODIFIERS:
        i += 1
    rest = toks[i:]
    if not rest:
        return ""
    head = rest[0]
    if head.lower() not in _DEF_KEYWORDS:
        # 装饰器片段（@deco）不含定义关键字，但确实指向一处带装饰的定义；
        # 按首标识符保留，否则 rg 侧的 decorated-fn 会整条丢失。
        if body.startswith("@") and len(rest) >= 2:
            return f"@{rest[1].lower()}"
        return ""
    tail = " ".join(rest[1:])[:80]
    return f"{head.lower()} {tail}"


def _dedup_key(fp: str, ln: int, txt: str):
    """跨路径统一去重键 = (绝对路径, 规范化签名)。"""
    sig = _norm_sig(txt)
    if not sig:
        return None
    return (os.path.abspath(os.path.expanduser(fp)), sig)


def _first_token(txt: str) -> str:
    """片段里的第一个标识符词（小写），供 AST 侧跨 pattern 去重使用。"""
    m = re.search(r"[A-Za-z_][A-Za-z0-9_]*", txt or "")
    return m.group(0).lower() if m else ""


def structural_search_ast(rule: dict[str, Any], path, excludes, max_results,
                          dots=False, timeout=None, runner=None):
    """ast-grep 路径：AST 精确匹配 + rg 兜底去重合并。

    返回 (results, scope)。scope 为规则声明的 ast_scope。

    去重顺序即输出顺序：AST 命中先入（更精确），再补 rg 独有的形态
    （`why_rg` 声明的缺口）。rg 侧的重复命中（同一 pattern 的多行、多个
    pattern 的同一点）在并入时被同一个键挡掉。
    """
    scope = rule.get("ast_scope", "none")
    if scope == "none":
        return [], scope

    ast_hits = [h for h in _invoke_sg(rule.get("ast_patterns", []), path, excludes,
                                      dots, timeout, runner=runner) if h[2]]
    if scope == "full":
        seen: set = set()
        out: list[tuple[str, int, str]] = []
        for fp, ln, txt in ast_hits:
            key = _dedup_key(fp, ln, txt)
            if key and key not in seen:
                seen.add(key)
                out.append((fp, ln, txt))
            if len(out) >= max_results:
                break
        return out, scope

    rg_hits = structural_search_rg(rule, path, excludes, max_results, dots,
                                   timeout, runner=runner)
    seen = {_dedup_key(fp, ln, txt) for fp, ln, txt in ast_hits}
    seen.discard(None)
    merged = list(ast_hits)
    for fp, ln, txt in rg_hits:
        key = _dedup_key(fp, ln, txt)
        if key is None:
            continue
        if key not in seen:
            seen.add(key)
            merged.append((fp, ln, txt))
        if len(merged) >= max_results:
            break
    return merged[:max_results], scope


def structural_search(rule_name, path, excludes, max_results, dots=False,
                      timeout=None, runner=None):
    """结构搜索统一入口（供 seek.py re-export，签名与升级前一致）。

    返回 (results, err)。results 为 [(path, line, snippet)]。
    ast-grep 可用则走 AST 路径（partial 时与 rg 去重合并）；不可用则整条回落
    rg -U，行为与升级前等价。

    `runner` 是子进程注入点：缺省用本模块 `_run`，调用方（单测）可换成打桩实现
    ——保留此前「mock seek.run 拦超时」的探测口径，超时预算必须下传。
    """
    key = resolve_rule(rule_name)
    if not key:
        avail = "、".join(sorted(STRUCTURAL_RULES))
        return [], f"未知结构查询「{rule_name}」，可用：{avail}"
    rule = STRUCTURAL_RULES[key]
    run_impl = runner or _run
    if not sg_available():
        return structural_search_rg(rule, path, excludes, max_results, dots,
                                    timeout, runner=run_impl), None
    hits, _scope = structural_search_ast(rule, path, excludes, max_results, dots,
                                         timeout, runner=run_impl)
    return hits, None


def describe_backend() -> dict[str, Any]:
    """可观测性：当前走哪条路、哪些规则是 AST 全量覆盖。"""
    return {
        "ast_grep": _SG_PATH,
        "backend": "ast-grep" if sg_available() else "rg-fallback",
        "rules": {
            k: {"ast_scope": v.get("ast_scope", "none"),
                "rg_patterns": len(v.get("patterns", [])),
                "ast_patterns": len(v.get("ast_patterns", []))}
            for k, v in STRUCTURAL_RULES.items()
        },
    }
