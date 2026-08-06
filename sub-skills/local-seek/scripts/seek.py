#!/usr/bin/env python3
"""local-seek: 本地高效搜索统一入口。零第三方依赖，仅用 Python 标准库。

路由规则（Argo 式确定性路由，模型不必自己拼参数）：
  rg       正文/正则搜索（默认，最快，尊重 .gitignore）
  fd       按文件名查找（--filename）
  mdfind   macOS Spotlight 全盘兜底（--spotlight 或 --scope all）

输出原则：默认精简文本（路径:行号:截断片段），--json 供 Agent 消费。
全链路零 token 消耗：工具输出本身就是压缩后的结果。

用法：
  seek.py "查询词"                     # 当前目录 rg 搜索（精简输出）
  seek.py "查询词" --path ~/notes      # 指定目录
  seek.py "查询词" --scope doc         # 文档类（含 pdf/docx 等文件名提示）
  seek.py "查询词" --filename          # 按文件名查找（fd）
  seek.py "查询词" --spotlight         # Spotlight 全盘兜底（含 PDF/邮件/笔记）
  seek.py "查询词" --count             # 先看每文件命中数，不输出内容
  seek.py "查询词" --context 3         # 带上下文行
  seek.py "查询词" --type py,ts        # 限定扩展名
  seek.py "查询词" --json              # JSON 输出
  seek.py "查询词" --max 10            # 限制结果数
  seek.py "查询词" --exact             # 关闭中文扩展（精确匹配）
  seek.py "查询词" --exclude 某文件     # 额外排除 glob（可重复，如排除评估脚本自身）
  seek.py --outline 文件路径            # 输出文件结构（def/class/标题/顶层key）
  seek.py --lines 10-50 文件路径        # 按行读取文件（替代 read_file 全文）
  seek.py "裸except" --structural       # 结构搜索（空catch/裸except/装饰函数/函数定义）
  seek.py --git-log 文件路径            # 文件的最近提交历史
  seek.py --git-blame 12 文件路径       # 第 12 行的提交归属
  seek.py --domains                    # 列出知识域配置
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from itertools import islice
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DOMAINS_FILE = CONFIG_DIR / "domains.yaml"

DEFAULT_EXCLUDES = [
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv",
    "dist", "build", "target", ".next", ".cache", "Pods", "vendor",
    ".DS_Store", "*.min.js", "*.map", "*.lock", ".terraform", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "coverage", "htmlcov", ".idea", ".vscode",
    ".venv", "venv", "env", ".env", "site-packages", "node_modules/.cache",
]
DOC_EXTS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "md", "txt",
            "rtf", "html", "htm", "epub", "csv", "json", "yaml", "yml", "log"}
CODE_EXTS = {"py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "c", "h",
             "cpp", "hpp", "cs", "rb", "php", "swift", "kt", "sh", "bash",
             "zsh", "sql", "vue", "svelte", "lua", "r", "scala", "dart", "ex"}

REGEX_META = re.compile(r'[.*+?\[\](){}^$|\\]')
LOOKAROUND = re.compile(r'\(\?[=!<]|\\[1-9]')
CJK_RE = re.compile(r'[\u4e00-\u9fff]+')

_pcre2_ok = None


def is_literal(query: str) -> bool:
    """查询是否纯字面量（无 regex 元字符），决定是否用 rg -F 固定字符串模式。"""
    return not REGEX_META.search(query)


def needs_pcre2(query: str) -> bool:
    """查询是否需要 PCRE2（look-around / 反向引用），默认引擎不支持。"""
    return bool(LOOKAROUND.search(query))


def pcre2_supported() -> bool:
    """本机 rg 是否编译了 pcre2 特性（模块级缓存，只测一次）。"""
    global _pcre2_ok
    if _pcre2_ok is None:
        proc = run(["rg", "--pcre2", "-e", "x", "/dev/null"])
        _pcre2_ok = proc is not None and proc.returncode == 0
    return _pcre2_ok


def build_patterns(query: str, exact: bool = False):
    """构建搜索 pattern 列表。零依赖中文扩展：对长度>=3 的中文段补滑动二元组，
    提升召回（如「封面图」扩展出「封面」），代价是少量噪音，--exact 可关闭。
    返回 (patterns, fixed)：fixed 表示全部 pattern 可作固定字符串。"""
    if exact:
        return [query], is_literal(query)
    tokens, pos = [], 0
    for m in CJK_RE.finditer(query):
        if m.start() > pos:
            tokens.append(query[pos:m.start()])
        tokens.append(m.group(0))
        pos = m.end()
    if pos < len(query):
        tokens.append(query[pos:])
    if not any(CJK_RE.fullmatch(t) for t in tokens):
        return [query], is_literal(query)
    parts = []
    for t in tokens:
        if CJK_RE.fullmatch(t) and len(t) >= 3:
            parts.append(t)
            for i in range(len(t) - 1):
                parts.append(t[i:i + 2])
        elif t.strip():
            # 剥掉非中文段首尾空白，跳过纯空白 token（否则空格会成噪音 pattern）
            parts.append(t.strip())
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out, all(is_literal(p) for p in out)


def load_excludes() -> list:
    """解析 domains.yaml 的排除规则（极简段感知解析，不引入 YAML 依赖）。"""
    excludes = list(DEFAULT_EXCLUDES)
    if not DOMAINS_FILE.exists():
        return excludes
    section = None
    for raw in DOMAINS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("exclude") and ":" in line:
            section = "exclude"
            continue
        if line.startswith("knowledge_domains") and ":" in line:
            section = "knowledge"
            continue
        if line.startswith(("max_results", "snippet", "doc_exts")) and ":" in line:
            section = None
            continue
        m = re.match(r"^-\s+(.+?)\s*$", line)
        if m and section == "exclude":
            item = m.group(1).strip().strip("\"'")
            if item and item not in excludes:
                excludes.append(item)
    return excludes


def run(cmd, cwd=None, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return None


def tool_exists(name: str) -> bool:
    proc = run(["command", "-v", name])
    return proc is not None and proc.returncode == 0


def truncate(text: str, n: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def rg_search(patterns, path, excludes, exts, context, count, max_results,
              fixed, raw_query=""):
    def build(fixed):
        cmd = ["rg", "--line-number", "--no-heading", "-i", "--color", "never"]
        if fixed:
            cmd.append("-F")
        elif needs_pcre2(raw_query):
            if pcre2_supported():
                cmd.append("--pcre2")
            else:
                return None, ("本机 rg 未编译 PCRE2，不支持 look-around/反向引用语法，"
                              "请简化查询（如去掉 (?=、(?! 等结构）")
        if count:
            cmd.append("--count-matches")
        elif context > 0:
            cmd += ["-C", str(context)]
        for ex in excludes:
            cmd += ["-g", f"!{ex}"]
        if exts:
            for e in exts:
                cmd += ["-g", f"*.{e}"]
        for p in patterns:
            cmd += ["-e", p]
        cmd.append(str(path))
        return cmd, None

    cmd, err = build(fixed)
    if err:
        return [], err
    proc = run(cmd)
    if (proc is not None and proc.returncode == 2 and not fixed
            and "regex parse error" in (proc.stderr or "")):
        # regex 解析失败（如按字面意图输入 interface{}、foo.bar），回退固定字符串
        cmd2, _ = build(True)
        proc = run(cmd2)
    if proc is None or proc.returncode not in (0, 1, 2):
        return [], "rg 执行失败"
    if proc.returncode == 1:
        return [], None  # 无匹配，正常
    if count:
        out = []
        for line in proc.stdout.splitlines():
            if ":" in line:
                fp, _, n = line.rpartition(":")
                out.append((fp, int(n) if n.isdigit() else 0, ""))
            if len(out) >= max_results:
                break
        return out, None
    out = []
    for line in proc.stdout.splitlines():
        m = re.match(r"^(.*?):(\d+):(.*)$", line)
        if m:
            fp, ln, txt = m.group(1), int(m.group(2)), m.group(3)
            out.append((fp, ln, truncate(txt)))
        if len(out) >= max_results:
            break
    return out, None


def fd_search(query, path, exts, max_results):
    cmd = ["fd", "-i", "-t", "f", "--color", "never"]
    for ex in DEFAULT_EXCLUDES:
        cmd += ["-E", ex]
    if exts:
        for e in exts:
            cmd += ["-e", e]
    cmd += [query, str(path)]
    proc = run(cmd)
    if proc is None or proc.returncode not in (0, 1):
        return [], "fd 执行失败"
    out = [(p, 0, "") for p in proc.stdout.splitlines()[:max_results]]
    return out, None


def mdfind_search(query, path, max_results):
    cmd = ["mdfind"]
    if path and str(path) != ".":
        cmd += ["-onlyin", str(Path(path).expanduser())]
    cmd += [query]
    proc = run(cmd)
    if proc is None:
        return [], "mdfind 执行失败"
    out = [(p, 0, "") for p in proc.stdout.splitlines()[:max_results]]
    return out, None


def format_output(results, engine, mode, path, elapsed_ms, query, total):
    lines = [f"local-seek: {total} 处命中（{engine} · {path} · {elapsed_ms}ms · 模式 {mode}）"]
    base = os.path.abspath(os.path.expanduser(path))
    for fp, ln, txt in results:
        afp = os.path.abspath(fp)
        try:
            rel = os.path.relpath(afp, base)
        except ValueError:
            rel = afp  # 跨挂载点无法计算相对路径
        shown = rel if not rel.startswith("..") else afp
        if ln:
            lines.append(f"{shown}:{ln}: {txt}" if txt else f"{shown}:{ln}")
        else:
            lines.append(f"{shown}")
    return "\n".join(lines)


def to_json(results, engine, mode, scope, path, elapsed_ms, query):
    return json.dumps({
        "query": query,
        "engine": engine,
        "mode": mode,
        "scope": scope,
        "path": str(path),
        "elapsed_ms": elapsed_ms,
        "count": len(results),
        "results": [
            {"path": fp, "line": ln, "snippet": txt}
            for fp, ln, txt in results
        ],
    }, ensure_ascii=False, indent=2)


_OUTLINE_RULES = {
    ".py": [re.compile(r'^\s*(async\s+)?(def|class)\s+\w+')],
    ".pyi": [re.compile(r'^\s*(async\s+)?(def|class)\s+\w+')],
    ".js": [re.compile(r'^\s*(export\s+)?(async\s+)?(function|class)\s+\w+'),
            re.compile(r'^\s*(export\s+)?(const|let|var)\s+\w+')],
    ".jsx": [re.compile(r'^\s*(export\s+)?(async\s+)?(function|class)\s+\w+'),
             re.compile(r'^\s*(export\s+)?(const|let|var)\s+\w+')],
    ".ts": [re.compile(r'^\s*(export\s+)?(async\s+)?(function|class|interface|type|enum)\s+\w+'),
            re.compile(r'^\s*(export\s+)?(const|let|var)\s+\w+')],
    ".tsx": [re.compile(r'^\s*(export\s+)?(async\s+)?(function|class|interface|type|enum)\s+\w+'),
             re.compile(r'^\s*(export\s+)?(const|let|var)\s+\w+')],
    ".go": [re.compile(r'^\s*func\s+\w+'),
            re.compile(r'^\s*type\s+\w+\s+(struct|interface)\b')],
    ".rs": [re.compile(r'^\s*(pub\s+)?(fn|struct|enum|impl|trait|mod|type)\s+\w+')],
    ".java": [re.compile(r'^\s*(public|private|protected|static|\s)*(class|interface|enum)\s+\w+'),
              re.compile(r'^\s*(public|private|protected|static|\s)*[\w<>,\[\] ]+\s+\w+\s*\(')],
    ".sh": [re.compile(r'^\s*function\s+\w+'),
            re.compile(r'^\s*[a-zA-Z_]\w*\s*\(\)\s*\{?')],
    ".bash": [re.compile(r'^\s*function\s+\w+'),
              re.compile(r'^\s*[a-zA-Z_]\w*\s*\(\)\s*\{?')],
    ".zsh": [re.compile(r'^\s*function\s+\w+'),
             re.compile(r'^\s*[a-zA-Z_]\w*\s*\(\)\s*\{?')],
    ".md": [re.compile(r'^#{1,6}\s+')],
    ".mdx": [re.compile(r'^#{1,6}\s+')],
    ".json": [re.compile(r'^\s*"[^"]+"\s*:')],
    ".yaml": [re.compile(r'^[a-zA-Z_][\w.-]*\s*:')],
    ".yml": [re.compile(r'^[a-zA-Z_][\w.-]*\s*:')],
}


def outline_file(path):
    """输出代码/文档文件结构，替代整文件读取。返回 (输出文本, 退出码)。"""
    p = Path(path)
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"local-seek: 读取失败 {e}", 1
    rules = _OUTLINE_RULES.get(p.suffix.lower())
    if not rules:
        return (f"local-seek: {p.name} 共 {len(lines)} 行"
                f"（{p.suffix or '无扩展名'} 暂无结构规则）"), 0
    hits = []
    for i, ln in enumerate(lines, 1):
        if any(r.match(ln) for r in rules):
            hits.append(f"{i}: {ln.strip()[:100]}")
    head = f"local-seek: {p.name} 结构 {len(hits)} 处 / 共 {len(lines)} 行"
    return "\n".join([head] + hits), 0


def read_lines(path, spec):
    """按行读取文件（惰性，不加载全文）。返回 (输出文本, 退出码)。"""
    m = re.fullmatch(r"(\d+)-(\d+)", spec)
    if not m:
        return "local-seek: --lines 格式应为 N-M（如 10-20）", 1
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        return f"local-seek: 行范围无效 {spec}", 1
    p = Path(path)
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    try:
        f = open(p, encoding="utf-8", errors="replace")
    except OSError as e:
        return f"local-seek: 读取失败 {e}", 1
    with f:
        total = sum(1 for _ in f)
    if a > total:
        return f"local-seek: {p.name} 只有 {total} 行，请求从 {a} 行开始", 1
    end = min(b, total)
    out = [f"local-seek: {p.name} 第 {a}-{end} 行 / 共 {total} 行"]
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, ln in enumerate(islice(f, a - 1, end), start=a):
            out.append(f"{i}: {ln.rstrip()}")
    return "\n".join(out), 0


STRUCTURAL_RULES = {
    "empty-catch": {
        "aliases": ("empty catch", "空catch", "空catch块", "空捕获", "空异常处理"),
        "patterns": [
            (("js", "jsx", "ts", "tsx"), r"catch\s*\([^)]*\)\s*\{\s*\}"),
        ],
    },
    "bare-except": {
        "aliases": ("bare except", "裸except", "裸异常", "无类型except"),
        "patterns": [
            (("py",), r"except\s*:"),
        ],
    },
    "unwrapped-error": {
        "aliases": ("unwrapped error", "错误未包装", "裸错误返回", "裸return err"),
        "patterns": [
            (("go",), r"return\s+err\b"),
        ],
    },
    "decorated-fn": {
        "aliases": ("decorated function", "装饰函数", "装饰器函数", "decorator"),
        "patterns": [
            (("py",), r"^\s*@\w[\w.]*\s*\n\s*(async\s+)?def\s+"),
        ],
    },
    "function-def": {
        "aliases": ("function definition", "函数定义", "找函数"),
        "patterns": [
            (("py", "pyi"), r"^\s*(async\s+)?def\s+\w+"),
            (("js", "jsx", "ts", "tsx"), r"^\s*(export\s+)?(async\s+)?function\s+\w+"),
            (("go",), r"^\s*func\s+\w+"),
            (("rs",), r"^\s*(pub\s+)?fn\s+\w+"),
        ],
    },
    "class-def": {
        "aliases": ("class definition", "类定义", "找类"),
        "patterns": [
            (("py", "pyi"), r"^\s*class\s+\w+"),
            (("js", "jsx", "ts", "tsx"), r"^\s*(export\s+)?class\s+\w+"),
            (("go",), r"^\s*type\s+\w+\s+struct\b"),
            (("java",), r"^\s*(public|private|protected)?\s*class\s+\w+"),
        ],
    },
}

_STRUCT_ALIAS = {}
for _key, _rule in STRUCTURAL_RULES.items():
    _STRUCT_ALIAS[_key] = _key
    for _a in _rule["aliases"]:
        _STRUCT_ALIAS[_a] = _key


def structural_search(rule_name, path, excludes, max_results):
    """结构搜索：按语义规则（空 catch/裸 except/未包装错误/装饰函数等）检索。
    零安装实现：rg -U 多行 + 语言感知 pattern；本机装 ast-grep 后可升级为 AST 精确匹配。"""
    key = _STRUCT_ALIAS.get(rule_name.strip().lower())
    if not key:
        avail = "、".join(STRUCTURAL_RULES)
        return [], f"未知结构查询「{rule_name}」，可用：{avail}"
    results = []
    for exts, pat in STRUCTURAL_RULES[key]["patterns"]:
        cmd = ["rg", "--line-number", "--no-heading", "-U", "-i", "--color", "never"]
        for ex in excludes:
            cmd += ["-g", f"!{ex}"]
        for e in exts:
            cmd += ["-g", f"*.{e}"]
        cmd += ["-e", pat, str(path)]
        proc = run(cmd)
        if proc is None:
            continue
        if proc.returncode == 1:
            continue  # 该语言无命中
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if m:
                results.append((m.group(1), int(m.group(2)), truncate(m.group(3))))
            if len(results) >= max_results:
                return results, None
    return results, None


def git_log(path, n=10):
    """输出文件的最近提交历史（git log --oneline）。"""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    proc = run(["git", "-C", str(p.parent), "log", "--oneline", f"-{n}", "--", p.name])
    if proc is None:
        return "local-seek: git 执行失败", 1
    if proc.returncode == 128 or (proc.returncode == 0 and not proc.stdout.strip()):
        inside = run(["git", "-C", str(p.parent), "rev-parse", "--is-inside-work-tree"])
        if inside is None or inside.returncode != 0:
            return f"local-seek: {p.name} 不在 git 仓库中", 1
        return f"local-seek: {p.name} 无提交历史", 0
    lines = proc.stdout.splitlines()
    return "\n".join([f"local-seek: {p.name} 最近 {len(lines)} 条提交"] + lines), 0


def git_blame(path, line):
    """输出文件第 N 行的 blame 信息（谁在哪个提交改的）。"""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    proc = run(["git", "-C", str(p.parent), "blame", "-L", f"{line},{line}", "--", p.name])
    if proc is None or proc.returncode != 0:
        return f"local-seek: git blame 失败（{p.name} 可能在 git 仓库外或行号无效）", 1
    return f"local-seek: {p.name} 第 {line} 行\n{proc.stdout.strip()}", 0


def main():
    ap = argparse.ArgumentParser(prog="seek", description="本地高效搜索统一入口")
    ap.add_argument("query", nargs="?", help="搜索查询词")
    ap.add_argument("--path", default=".", help="搜索目录（默认当前目录）")
    ap.add_argument("--scope", choices=["code", "doc", "all"], default="code",
                    help="code=正文+代码；doc=文档类；all=Spotlight 兜底")
    ap.add_argument("--filename", action="store_true", help="按文件名查找（fd）")
    ap.add_argument("--spotlight", action="store_true", help="Spotlight 全盘兜底")
    ap.add_argument("--type", default="", help="限定扩展名，逗号分隔（py,ts,md）")
    ap.add_argument("--count", action="store_true", help="只输出每文件命中数")
    ap.add_argument("--context", type=int, default=0, help="上下文行数（默认 0）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--max", type=int, default=0, help="最大结果数（默认读配置）")
    ap.add_argument("--exact", action="store_true", help="关闭中文扩展（精确匹配）")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="额外排除 glob（可重复指定，如 --exclude eval_seek.py）")
    ap.add_argument("--outline", action="store_true", help="输出文件结构（文件路径为位置参数）")
    ap.add_argument("--lines", default="", metavar="N-M",
                    help="按行读取文件（文件路径为位置参数）")
    ap.add_argument("--structural", action="store_true",
                    help="结构搜索：按语义检索（裸except/空catch/装饰函数/函数定义等）")
    ap.add_argument("--git-log", action="store_true",
                    help="输出文件的最近提交历史（文件路径为位置参数）")
    ap.add_argument("--git-blame", default="", metavar="N",
                    help="输出文件第 N 行的 blame 信息（文件路径为位置参数）")
    ap.add_argument("--domains", action="store_true", help="列出知识域与排除规则")
    args = ap.parse_args()

    # 知识域展示模式
    if args.domains:
        if DOMAINS_FILE.exists():
            print(DOMAINS_FILE.read_text(encoding="utf-8"))
        else:
            print("未找到 config/domains.yaml，使用内置默认规则")
        return

    # 文件结构 / 按行读取模式（文件路径优先取位置参数，否则取 --path）
    if args.outline or args.lines:
        target = args.query or str(args.path)
        if args.lines:
            msg, rc = read_lines(target, args.lines)
        else:
            msg, rc = outline_file(target)
        print(msg)
        return rc

    # git 联动模式：查文件提交历史 / 单行归属
    if args.git_log or args.git_blame:
        target = args.query or str(args.path)
        if args.git_blame:
            msg, rc = git_blame(target, args.git_blame)
        else:
            msg, rc = git_log(target)
        print(msg)
        return rc

    # 结构搜索模式：按语义规则检索（裸except/空catch/装饰函数等）
    if args.structural:
        if not args.query:
            ap.print_help()
            return
        start = time.time()
        results, err = structural_search(args.query, Path(args.path).expanduser(),
                                         load_excludes() + args.exclude,
                                         args.max or 30)
        elapsed = int((time.time() - start) * 1000)
        if err:
            print(f"local-seek: {err}")
            return 1
        if not results:
            print(f"local-seek: 未找到匹配（rg-structural · {args.path} · {elapsed}ms）")
            return 1
        if args.json:
            print(to_json(results, "rg-structural", "structural", args.scope,
                          args.path, elapsed, args.query))
        else:
            print(format_output(results, "rg-structural", "structural",
                                args.path, elapsed, args.query, len(results)))
        return 0

    if not args.query:
        ap.print_help()
        return

    max_results = args.max or 30
    excludes = load_excludes() + args.exclude
    exts = [e.strip().lstrip(".") for e in args.type.split(",") if e.strip()]

    scope = args.scope
    if args.spotlight:
        scope = "all"
    # doc 场景补充文档扩展名
    if scope == "doc" and not exts:
        exts = sorted(DOC_EXTS - {"txt", "md", "json", "yaml", "yml", "log"})
        # 文档全文主要走 rg；pdf 等二进制走 Spotlight 文件名/内容索引
        scope = "code"
        if not tool_exists("rg"):
            scope = "all"

    path = Path(args.path).expanduser()
    if not path.exists():
        print(f"local-seek: 目录不存在 {path}")
        return 1

    engine = "rg"
    mode = "fast"
    start = time.time()
    results, err = [], None
    patterns, fixed = build_patterns(args.query, args.exact)

    if scope == "all" or not tool_exists("rg"):
        engine, mode = "mdfind", "deep"
        results, err = mdfind_search(args.query, None if args.spotlight else path,
                                     max_results)
    elif args.filename:
        engine, mode = "fd", "fast"
        results, err = fd_search(args.query, path, exts, max_results)
    else:
        mode = "deep" if (args.context > 0 or args.count) else "fast"
        if not args.exact and len(patterns) > 1:
            # 中文扩展遵循「先窄后宽」：先精确匹配，命中不足才放宽到扩展词
            results, err = rg_search([args.query], path, excludes, exts,
                                     args.context, args.count, max_results,
                                     is_literal(args.query), args.query)
            if not results and not err:
                results, err = rg_search(patterns, path, excludes, exts,
                                         args.context, args.count, max_results,
                                         fixed, args.query)
                if results:
                    mode += "+扩展"
        else:
            results, err = rg_search(patterns, path, excludes, exts,
                                     args.context, args.count, max_results,
                                     fixed, args.query)

    elapsed = int((time.time() - start) * 1000)

    if err:
        print(f"local-seek: {err}")
        return 1
    if not results:
        print(f"local-seek: 未找到匹配（{engine} · {path} · {elapsed}ms）")
        return 1

    if args.json:
        print(to_json(results, engine, mode, scope, path, elapsed, args.query))
    else:
        print(format_output(results, engine, mode, path, elapsed, args.query,
                            len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
