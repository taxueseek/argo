#!/usr/bin/env python3
"""min_version_scan.py — 扫出「低于声明最低版本就解析不了」的语法。

## 为什么需要它

argo 声明支持 Python 3.9（见 bin/argo 的 MIN_PYTHON），但开发机与 CI 都跑在
更新的解释器上。于是新语法会在**开发机全绿、用户机上 SyntaxError**：

* `job.py` 里两处 PEP 701 写法（f-string 内嵌同类引号，3.12+ 才有）就是这么
  潜进去的。它还不只是「一条命令坏了」——`bin/argo` 会把选中的解释器写进
  缓存，于是一次探测失误被固化，此后每次调用都失败；而同一缓存下别的子命令
  照常运行，用户完全不知道自己在跑一个跑不动的解释器。
* 静态检查当时全部通过，因为 `ast.parse` 与 ruff 的 E9 都只用**当前**解析器，
  判的是「语法合法」，从不判「在声明支持的最低版本上合法」。

## 判定策略

不用低版本解释器（要求 CI 备齐 3.8~3.12 不现实），也不用逐行正则（`X | Y`
的文本形态与位运算 / 正则交替 / 文档示例完全同形，实测一口气误报 14 处）。
改为基于 **AST** 判结构，只在语法结构真的越界时才报：

| 特征 | 版本 | 判据 |
|------|------|------|
| PEP 701 f-string 外层同类引号嵌套 | 3.12+ | tokenizer：从 FSTRING_START 取外层引号，看字段内是否出现同类引号 |
| PEP 604 `X \\| Y` 用在类型上 | 3.10+ | AST：BinOp(BitOr) 且两侧形如类型名，且文件无 `from __future__ import annotations` |
| `match` 语句 | 3.10+ | AST：存在 Match 节点 |

**刻意偏保守**：漏报只是少一层提示，误报会让检查被豁免掉，那检查就废了。
所以 `0x80 | length`、`{a} | {b}`、`os.O_CREAT | os.O_RDWR` 一律不报。

## 用法

    python3 scripts/min_version_scan.py scripts/ tests/ bin/argo
    python3 -c "from min_version_scan import scan_text; print(scan_text(src, (3,9)))"

零依赖、不联网。作为库使用时返回结构化结果，供检查断言。
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import Iterable

# 形如类型名的标识符白名单（小写但确是类型）
_TYPE_NAMES = frozenset({
    "None", "int", "str", "bool", "float", "bytes", "dict", "list", "set",
    "tuple", "Any", "Path", "Text", "IO", "Optional", "Union", "Callable",
    "Sequence", "Mapping", "Iterable", "Iterator", "datetime", "date",
})

DEFAULT_TARGETS = ("scripts", "tests", "bin/argo")


def read_min_python(bin_argo: Path | str) -> tuple[int, int]:
    """从 bin/argo 读 MIN_PYTHON（版本下限的唯一来源）。

    不在各处另写常量：两处各写一遍版本号、改一处漏一处，正是「3.9 被选中并
    固化进缓存」那次事故的成因。读不到返回 (0, 0)，由调用方判为失败。
    """
    try:
        text = Path(bin_argo).read_text(encoding="utf-8")
    except OSError:
        return (0, 0)
    m = re.search(r"^MIN_PYTHON\s*=\s*\((\d+)\s*,\s*(\d+)\)", text, re.M)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def scan_text(source: str, minver: tuple[int, int]) -> list[tuple[int, str]]:
    """扫描一段源码，返回 [(行号, 原因)]。

    顺序很关键：PEP 701 的检查**先跑**，而且不依赖 `ast.parse`。因为这种写法在
    低版本解释器上根本 parse 不过——若先 parse 再判别，跑在 3.9 上时只会得到
    一句笼统的「invalid syntax」，拿不到「这是 3.12+ 语法、请改写」这个可行动
    的结论。检查的价值恰恰在于给出可行动的诊断。
    """
    findings: list[tuple[int, str]] = []
    if minver < (3, 12):
        low = source.lower()
        if 'f"' in low or "f'" in low:
            findings.extend(_pep701_findings(source))

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        # 已由上面的专项检查给出更具体的原因时，不再补一条笼统的语法错误。
        if findings:
            return sorted(set(findings))
        # 当前解释器都解析不了，交给别的检查（ruff E9）报，这里不重复
        return [(e.lineno or 0, f"语法错误：{e.msg}")]

    if minver < (3, 10):
        if "|" in source:
            findings.extend(_pep604_findings(tree, _has_future_annotations(tree)))
        if "match " in source or "match\t" in source:
            findings.extend(_match_findings(tree))
    return sorted(set(findings))


def scan_paths(paths: Iterable[Path | str],
               minver: tuple[int, int]) -> list[str]:
    """扫描文件/目录，返回可读的问题行（相对路径:行号 原因）。"""
    problems: list[str] = []
    for raw in paths:
        p = Path(raw)
        files = (sorted(p.rglob("*.py")) if p.is_dir() else [p])
        for f in files:
            if "__pycache__" in f.parts or not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, why in scan_text(text, minver):
                problems.append(f"{_rel(f)}:{lineno} {why}")
    return problems


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _has_future_annotations(tree: ast.AST) -> bool:
    """是否有 `from __future__ import annotations`（注解不求值，3.9 也能过）。

    用 AST 找真正的 ImportFrom，不扫文本：模块 docstring 里常出现形似 import
    的示例行，按行文本判断会提前终止，把有 future import 的文件误判成没有。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            return any(a.name == "annotations" for a in node.names)
    return False


# ── PEP 604：类型联合 ────────────────────────────────────────────────────────

def _pep604_findings(tree: ast.AST, has_future: bool) -> list[tuple[int, str]]:
    """`X | Y` 用在**类型**上、且注解会被求值时才算越界。

    `|` 在 3.9 上只有位或与集合并两种含义，都是合法语法；崩的是「类型对象之间
    用 |」（3.10 才定义 type.__or__）。所以按「操作数像不像类型」判，而不是按
    语法形状排除。
    """
    if has_future:
        return []
    if not any(isinstance(n, ast.AnnAssign) for n in ast.walk(tree)):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if _looks_like_type_union(node):
                out.append((node.lineno,
                            "类型注解里使用了 `X | Y`（PEP 604，3.10+）"
                            "——3.9 上是 TypeError；加 `from __future__ import annotations` "
                            "或改用 typing.Optional"))
    return out


def _looks_like_type_union(node: ast.BinOp) -> bool:
    """两侧是否都形如类型名。

    类型名的形态：首字母大写**且非全大写**（`Path`/`PathLike` 是类型，
    `O_CREAT`/`READ` 这类全大写常量是位标志），或落在已知类型名白名单里。
    """
    def _type_like(name: str) -> bool:
        if not name:
            return False
        if name in _TYPE_NAMES:
            return True
        return name[:1].isupper() and not name.isupper()

    def _is_type(n: ast.AST) -> bool:
        if isinstance(n, ast.Constant) and n.value is None:
            return True                      # `X | None` 是最常见形态
        if isinstance(n, ast.Name):
            return _type_like(n.id)
        if isinstance(n, ast.Attribute):
            return _type_like(n.attr)
        if isinstance(n, ast.Subscript):
            return _is_type(n.value)
        return False

    return _is_type(node.left) and _is_type(node.right)


# ── match / case ─────────────────────────────────────────────────────────────

def _match_findings(tree: ast.AST) -> list[tuple[int, str]]:
    # ast.Match 在 3.9 上不存在，而本模块自己必须能在 3.9 跑；用 getattr 取。
    match_cls = getattr(ast, "Match", None)
    if match_cls is None:
        return []
    for node in ast.walk(tree):
        if isinstance(node, match_cls):
            return [(node.lineno, "使用了 `match` 语句（3.10+）")]
    return []


# ── PEP 701：f-string 外层同类引号嵌套 ───────────────────────────────────────

def _pep701_findings(source: str) -> list[tuple[int, str]]:
    """找 3.12+ 的 PEP 701 写法。

    为什么必须自己判：`ast.parse(feature_version=...)` **不管**这件事——PEP 701
    是 3.12 放宽了限制，不是新增语法，实测 feature_version=(3,9) 对坏样本照样
    返回 OK。没有现成的低版本 oracle。

    为什么从 FSTRING_START 出发：3.12+ 的 tokenizer 把 f-string 拆成
    FSTRING_START / FSTRING_MIDDLE / FSTRING_END，内层字符串才是普通 STRING。
    只盯 STRING token 会一个都抓不到——那正是本品首版在 3.14 上静默放行
    `job.py` 的原因。
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 3.9~3.11 的 tokenizer 无法切分这种 f-string（它本身就是待检的坏代码），
        # 或源码片段不完整。退回按字符串 token 的文本判据。
        return _pep701_by_string_token(_safe_tokens(source))

    fstart = getattr(tokenize, "FSTRING_START", None)
    if fstart is None:
        return _pep701_by_string_token(tokens)

    findings: list[tuple[int, str]] = []
    outer: str | None = None
    fend = getattr(tokenize, "FSTRING_END", None)
    for tok in tokens:
        if tok.type == fstart:
            outer = _quote_of(tok.string)
            continue
        if outer is None:
            continue
        if fend is not None and tok.type == fend:
            outer = None
            continue
        if tok.type == tokenize.STRING and tok.string[:1] == outer[0]:
            findings.append((tok.start[0], _PEP701_MSG))
    return findings


_PEP701_MSG = ("f-string 替换字段内嵌了同类引号（PEP 701，3.12+）"
               "——3.9~3.11 上是 SyntaxError；请先取变量再拼接")


def _safe_tokens(source: str) -> list:
    """尽力拿 token；拿不到返回空列表（调用方按「无发现」处理）。"""
    try:
        return list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []


def _quote_of(text: str) -> str:
    """从 `f"` / `f'` / `f\"\"\"` 取引号部分。"""
    if len(text) >= 4 and text[1:4] in ('"""', "'''"):
        return text[1:4]
    return text[1:2] if len(text) >= 2 else '"'


def _pep701_by_string_token(tokens: list) -> list[tuple[int, str]]:
    """3.11 及更早的退路：f-string 是单个 STRING token，按文本推断。

    3.12+ 上不会走到这里（由 FSTRING_START 分支处理），保留是为了本模块在
    老解释器上跑出同样结论。

    不变量（2026-10-17 重写，此前一版「剥收尾引号」的修法拔掉了真獠牙）：
      - `{{` / `}}` 是**字段外**的字面花括号转义，不改变字段深度——按普通
        括号计数会把合法 f-string（chrome_cdp.py:735 的 `f"{{method:...},"`）
        误判成 PEP 701，连累 pre-commit 第一道门禁全红；
      - 字段内每个 `}` 都关闭字段（`}}` 字面转义只在字段外出现）；
      - 扫完整个 body 后 depth>0 ⇒ 字段未闭合。低版本 tokenizer 在第一个
        同类引号处截断 token（`f"...{get_env(["`），片段必然带着未闭合的
        `{`——这正是 PEP 701 写法在低版本 token 流里的形态；合法 f-string
        的所有字段必然在串内闭合。
    """
    findings: list[tuple[int, str]] = []
    for tok in tokens:
        if tok.type != tokenize.STRING:
            continue
        text = tok.string
        if len(text) < 3 or text[0] not in "fF":
            continue
        quote = _quote_of(text)
        body = text[1 + len(quote):]
        if body.endswith(quote):
            body = body[:-len(quote)]
        depth, i, n = 0, 0, len(body)
        flagged = False
        while i < n:
            ch = body[i]
            if ch == "{" or ch == "}":
                if depth == 0 and body.startswith(ch * 2, i):
                    i += 2  # 字面花括号转义（仅字段外合法）
                    continue
                depth += 1 if ch == "{" else -1
                depth = max(0, depth)
            elif depth > 0 and body.startswith(quote, i):
                flagged = True  # 字段内出现同类引号：PEP 701 本体
                break
            i += 1
        if not flagged and depth > 0:
            flagged = True  # 串尾字段未闭合：同类引号截断片段的形态
        if flagged:
            findings.append((tok.start[0], _PEP701_MSG))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent.parent
    bin_argo = root / "bin" / "argo"

    minver = read_min_python(bin_argo)
    if minver < (3, 8):
        print(f"无法从 {bin_argo} 读取 MIN_PYTHON", file=sys.stderr)
        return 2

    targets = args or [str(root / t) for t in DEFAULT_TARGETS]
    problems = scan_paths(targets, minver)
    if not problems:
        print(f"✓ 全部源码可被 Python {minver[0]}.{minver[1]} 解析")
        return 0
    print(f"✗ 存在 Python {minver[0]}.{minver[1]} 解析不了的语法：")
    for line in problems:
        print("  " + line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
