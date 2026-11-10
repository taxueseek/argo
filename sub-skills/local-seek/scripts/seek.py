#!/usr/bin/env python3
"""local-seek: 本地高效搜索统一入口。零第三方依赖，仅用 Python 标准库。

路由规则（Argo 式确定性路由，模型不必自己拼参数）：
  rg       正文/正则搜索（默认，最快，尊重 .gitignore）
  fd       按文件名查找（--filename）
  mdfind   macOS Spotlight 全盘保底（--spotlight 或 --scope all）

输出原则：默认精简文本（路径:行号:截断片段），--json 供 Agent 消费。
排序原则：内容命中按 BM25F 相关度重排（池子选择仍是确定性的 (路径,行号) 口径，
变的只是池内次序；ARGO_SEEK_CONTENT_RERANK=0 关闭，正则/纯符号查询与
--count/--context 模式自动跳过）。文件名场景用 fzf V2 评分 + 拼音加分。
全链路零 token 消耗：工具输出本身就是压缩后的结果。

用法：
  seek.py "查询词"                     # 当前目录 rg 搜索（精简输出）
  seek.py "查询词" --path ~/notes      # 指定目录
  seek.py "查询词" --scope doc         # 文档类（含 pdf/docx 等文件名提示）
  seek.py "查询词" --filename          # 按文件名查找（fd）
  seek.py "查询词" --spotlight         # Spotlight 全盘保底（含 PDF/邮件/笔记）
  seek.py "查询词" --count             # 先看每文件命中数，不输出内容
  seek.py "查询词" --context 3         # 带上下文行
  seek.py "查询词" --type py,ts        # 限定扩展名
  seek.py "查询词" --json              # JSON 输出
  seek.py "查询词" --max 10            # 限制结果数
  seek.py "查询词" --exact             # 关闭中文扩展（精确匹配）
  seek.py "查询词" --since 7d          # 只看最近 7 天修改过的文件命中
  seek.py "查询词" --until 2026-07-01  # 只看 2026-07-01 之前修改的命中
  seek.py "查询词" --json --since 7d   # JSON 输出带 mtime 字段
  seek.py "查询词" --exclude 某文件     # 额外排除 glob（可重复，如排除评估脚本自身）
  seek.py --outline 文件路径            # 输出文件结构（def/class/标题/顶层key）
  seek.py --lines 10-50 文件路径        # 按行读取文件（替代 read_file 全文）
  seek.py "裸except" --structural       # 结构搜索（空catch/裸except/装饰函数/函数定义）
  seek.py --git-log 文件路径            # 文件的最近提交历史
  seek.py --git-blame 12 文件路径       # 第 12 行的提交归属
  seek.py --domains                    # 列出知识域配置
"""

# PEP 604 联合注解（str | None）在 3.10+ 才可运行时求值；此处开启延迟注解，
# 使脚本在 Python 3.9（本机默认 python3）下也能正常导入/执行，而不只在 3.10+ 可用。
from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows 控制台与多语言环境编码保护（避免 UTF-8 输出在 GBK 等控制台下抛 UnicodeEncodeError）
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from seek_rank import _ZH_EN_ALIASES  # 中英工程词对齐（词源单点在 seek_rank）

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DOMAINS_FILE = CONFIG_DIR / "domains.yaml"

DEFAULT_EXCLUDES = [
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv",
    "dist", "build", "target", ".next", ".cache", "Pods", "vendor",
    ".DS_Store", "*.min.js", "*.map", "*.lock", ".terraform", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "coverage", "htmlcov", ".idea", ".vscode",
    ".venv", "venv", "env", ".env", "site-packages", "node_modules/.cache",
]

# ── 噪声档（2026-09-27 新增）───────────────────────────────────────────────
# 实测（Documents/GPT 搜 "import"，rg 61899 处命中）：
#     vendor/生成物/归档  7%    测试/fixture/benchmark  27%    tmp/  7%
# 合计 41% 的命中是「搜代码时不想看到的东西」。DEFAULT_EXCLUDES 已经挡掉
# 一部分生成物，但漏了三类最大的：repos/（克隆的第三方仓）、__tests__|tests|
# fixtures|benchmark（测试与固件）、tmp/（临时检出）。
#
# 为什么是**降权**而不是排除：
#   排除 = 这些内容永远搜不到。搜「某个第三方仓里怎么写的」「我的测试怎么
#   写的」是真实且常见的用法，排除会让工具在这些查询上直接给错答案。
#   降权 = 真实源优先，源不够时再回落到噪声档。既改善日常体验，又不制造
#   「明明有却搜不到」这种更糟的失败模式。
NOISE_TIER = [
    "repos", "repo", "third_party", "thirdparty", "vendors",
    "__tests__", "__test__", "tests", "test", "testdata", "test_data",
    "fixtures", "fixture", "__mocks__", "__snapshots__", "e2e",
    "benchmark", "benchmarks",
    "tmp", "temp", ".tmp", "tmpdir", "archive", "archives", ".trash",
    "2026-*",   # 本工作区的日期归档目录：研究产物，非源码
]
_NOISE_SET = frozenset(n for n in NOISE_TIER if not n.endswith("*"))
DOC_EXTS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "md", "txt",
            "rtf", "html", "htm", "epub", "csv", "json", "yaml", "yml", "log"}
OFFICE_ZIP_EXTS = {"docx", "pptx", "xlsx"}
CONFIG_EXTS = {"yaml", "yml", "json", "toml", "ini", "env", "conf", "xml", "properties"}
CODE_EXTS = {"py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "c", "h",
             "cpp", "hpp", "cs", "rb", "php", "swift", "kt", "sh", "bash",
             "zsh", "sql", "vue", "svelte", "lua", "r", "scala", "dart", "ex"}

REGEX_META = re.compile(r'[.*+?\[\](){}^$|\\]')
LOOKAROUND = re.compile(r'\(\?[=!<]|\\[1-9]')
CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+')  # 汉字扩展A/汉字/日假名/韩谚文（多语言 2-gram 扩展）

_pcre2_ok = None


def is_literal(query: str) -> bool:
    """查询是否纯字面量（无 regex 元字符），决定是否用 rg -F 固定字符串模式。"""
    return not REGEX_META.search(query)


def needs_pcre2(query: str) -> bool:
    """查询是否需要 PCRE2（look-around / 反向引用），默认引擎不支持。"""
    return bool(LOOKAROUND.search(query))


def pcre2_supported() -> bool:
    """本机 rg 是否编译了 pcre2 特性（模块级缓存，只测一次）。

    判据读 `rg --version` 的 features 行（如 `features:+pcre2`），
    **不再用「跑一次匹配看返回码」**。

    历史 bug（2026-09-27 实测定位）：原判据是

        proc = run(["rg", "--pcre2", "-e", "x", os.devnull])
        _pcre2_ok = proc is not None and proc.returncode == 0

    `/dev/null` 永远没有匹配，rg 无匹配时返回 **1**，于是 `returncode == 0`
    恒为 False——本机 rg 15.0.0 明明带 `+pcre2`（PCRE2 10.45 带 JIT），
    却永远被判为「未编译 PCRE2」。后果是所有 look-around / 反向引用查询
    被拒绝：

        $ seek.py 'foo(?=\\d)' --path /tmp/pcretest
        local-seek: 本机 rg 未编译 PCRE2，不支持 look-around 语法，请简化查询
        $ rg --pcre2 -e 'foo(?=\\d)' /tmp/pcretest     # 同一个 rg，正常工作
        /tmp/pcretest/a.txt:foo123

    新判据与 rg 自己声明的能力一致，不依赖「某个文件恰好有匹配」这种
    与探测目标无关的巧合；`rg --version` 不输出 ANSI 颜色（实测
    CLICOLOR_FORCE=1 下 features 行仍是纯文本），无需额外 strip。
    也不看 returncode——`rg --version` 正常时返回 0，但把它纳入判据等于
    给「返回码语义」留后门（原缺陷正是踩在这里），只看 features 行更纯。
    rg 不存在时 run 返回 None，同样落到 False（走 grep 回退路径）。
    """
    global _pcre2_ok
    if _pcre2_ok is None:
        proc = run(["rg", "--version"])
        _pcre2_ok = bool(proc is not None
                         and "+pcre2" in (proc.stdout or ""))
    return _pcre2_ok


def build_patterns(query: str, exact: bool = False):
    """构建搜索 pattern 列表。零依赖中文扩展：对长度>=3 的中文段补滑动二元组，
    能多搜到一些（如「封面图」扩展出「封面」），代价是少量噪音，--exact 可关闭。
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
    # 统一构建（中英混排同一条路径）：CJK 段 ≥3 字补 2-gram 放宽；非 CJK 段
    # 按空格拆词。此前混排查询（如「性能 asyncio tutorial」）走中文分支时英文
    # 段整段化，退回「固定短语匹配」，单词条目全部漏掉——eb0ed38 只修了纯英文
    # 分支，2026-10-03 两条分支合并后混排随之修复。
    parts = []
    for t in tokens:
        if CJK_RE.fullmatch(t) and len(t) >= 3:
            parts.append(t)
            for i in range(len(t) - 1):
                parts.append(t[i:i + 2])
        elif t.strip():
            for word in t.strip().split():
                if word:
                    parts.append(word)
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out, all(is_literal(p) for p in out)


_EXCLUDES_MEMO: tuple[int, list[str]] | None = None


def load_excludes() -> list:
    """解析 domains.yaml 的排除规则（极简段感知解析，不引入 YAML 依赖；mtime 记忆化）。"""
    global _EXCLUDES_MEMO
    try:
        mtime_ns = DOMAINS_FILE.stat().st_mtime_ns
    except OSError:
        return list(DEFAULT_EXCLUDES)
    if _EXCLUDES_MEMO is not None and _EXCLUDES_MEMO[0] == mtime_ns:
        return list(_EXCLUDES_MEMO[1])
    excludes = list(DEFAULT_EXCLUDES)
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
    _EXCLUDES_MEMO = (mtime_ns, excludes)
    return list(excludes)


def run(cmd, cwd=None, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except (FileNotFoundError, PermissionError, OSError):
        return None


def tool_exists(name: str) -> bool:
    # shutil.which 跨平台（Windows 下 command -v 不存在）
    return shutil.which(name) is not None


# 搜索器未显式传超时时的默认上限（秒，与 run() 的历史默认一致）。
# H2 之后进程内调用方会显式传更紧的 time_budget（防挂死占死单线程
# executor——子进程可硬杀，线程不可杀）。
_RUN_DEFAULT_TIMEOUT_S = 30.0


def truncate(text: str, n: int = 120) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _norm_root(root, cwd: str) -> str:
    """搜索根归一化成绝对、无冗余分隔符的字符串（纯字符串运算，零 syscall）。"""
    return os.path.normpath(root if os.path.isabs(root) else os.path.join(cwd, root))


def _is_noise_path(fp: str, root: str, root_abs: str | None = None,
                   cwd: str | None = None) -> bool:
    """该命中是否落在噪声档目录（相对**搜索根**判定）。

    为什么必须相对搜索根：直接对整条路径做分段匹配是错的——搜索根自己叫
    `tests/` 或含 `2026-` 时（本工作区正是如此），整棵树都会被判成噪声。
    rg 的 -g glob 犯的正是这个错（`**/tests/**` 会匹配路径里任意一段），
    所以降权放到 Python 层做，不交给 glob。

    性能（2026-10-06）：原实现每行都做
    `Path(fp).resolve().relative_to(Path(root).resolve())`。`resolve()` 对每个
    路径段 lstat、还反复 getcwd；实测 `--count` 扫 8930 个文件时它独占
    1.52s / 1.91s（80%），内含 3.5 万次 getcwd、10 万次 lstat。改为纯字符串
    归一化：根只归一化一次，命中路径用 normpath 做前缀比较，全程零 syscall。
    不在根之下（软链/越界）时退回整条路径判定，与原 `Path(fp).parts` 一致。
    """
    if cwd is None:
        cwd = os.getcwd()
    if root_abs is None:
        root_abs = _norm_root(root, cwd)
    afp = os.path.normpath(fp if os.path.isabs(fp) else os.path.join(cwd, fp))
    if afp == root_abs:
        return False
    prefix = root_abs + os.sep
    if afp.startswith(prefix):
        parts = afp[len(prefix):].split(os.sep)
    elif os.path.isabs(fp):
        parts = afp.split(os.sep)   # 绝对但不在根下：退回整条绝对路径
    else:
        parts = fp.split(os.sep)    # 相对但不在根下：退回原始相对路径
    if not parts:
        return False
    for p in parts[:-1]:         # 最后一段是文件名，不参与目录判定
        if p in _NOISE_SET:
            return True
        # 日期归档目录（2026-09-26_xxx）
        if p[:4].isdigit() and p[4:5] in "-_" and len(p) >= 5:
            return True
    return False


def _apply_noise_floor(rows, root, max_results):
    """噪声档结果排到真源之后；真源够数时直接不返回噪声档。

    不是排除：真源不足 max_results 时用噪声档补满，保证「有结果总比没结果好」，
    也保证搜第三方仓里的内容仍然可达。

    根与 cwd 只解析一次、每行只判一次（原实现对每行判两遍，还把根重复
    resolve 两遍——见 _is_noise_path 的性能注记）。
    """
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "."
    root_abs = _norm_root(root, cwd)
    clean, noisy = [], []
    for r in rows:
        (noisy if _is_noise_path(r[0], root, root_abs, cwd) else clean).append(r)
    return (clean + noisy)[:max_results]


_ENV_FALSY = ("0", "off", "no", "false", "disable", "disabled")

# 重排开启时的池子上限：评分几百行在毫秒级（seek_rank 单遍计数），而浅池
# 会让「每文件只轮到 1 行」、文件内最相关的深行进不了评分视野。
_POOL_CAP_MAX = 800


def _rerank_wanted(raw_query: str, context: int, count: bool,
                   fixed: bool) -> bool:
    """内容重排是否应该启用（只做**廉价判定**，不导入打分模块）。

    供 rg_search 在选池**之前**决定池子上限：重排开启时池子放宽到
    _POOL_CAP_MAX（评分几百行只多几毫秒，而浅池会把每个文件只轮到 1 行、
    文件内最相关的深行永远进不了评分视野）；关闭时维持与旧版逐位一致的
    小池（此时输出次序 = (路径,行号) 基准序，行为零变化）。
    """
    if (os.environ.get("ARGO_SEEK_CONTENT_RERANK", "").strip().lower()
            in _ENV_FALSY):
        return False
    return bool(fixed) and not count and context <= 0 and bool(raw_query)


def _rerank_content(rows, raw_query: str, context: int, count: bool,
                    fixed: bool = True):
    """BM25F 相关度重排的**闸门**：条件全过才真正打分，否则原样返回。

    1. ARGO_SEEK_CONTENT_RERANK 关闭 → 整体逃生门（恢复 (路径,行号) 原序）
    2. --count / --context 模式跳过：计数模式的输出语义是「每文件命中数」，
       上下文模式的行序承载分组结构，都不适用相关度次序
    3. 非字面量查询（正则/PCRE2，fixed=False）跳过：词元切分会把 `\\d{4}`
       切成 d/4/2 这类碎片，重排只引入噪声（字面量才进重排）
    4. seek_rank 任何异常 → 原样返回（重排是增益项，绝不阻塞主流程）

    打分只依赖 (路径, 行号, 行文本, 查询)，与 rg 发射序无关，并列分按
    (路径,行号) 升序——test_determinism_20261006 的确定性契约由此延续。
    """
    if not _rerank_wanted(raw_query, context, count, fixed):
        return rows
    try:
        import seek_rank
        import seek_frecency
        boost_fn = seek_frecency.boost if seek_frecency.enabled() else None
        return seek_rank.rank_rows(rows, raw_query, boost_fn=boost_fn)
    except Exception:
        return rows


_POOL_ZH_EN: dict[str, str] = {
    "\u672c\u5730": "local", "\u641c\u7d22": "search", "\u68c0\u7d22": "seek", "\u7f13\u5b58": "cache",
    "\u8def\u7531": "route", "\u6392\u5e8f": "rank", "\u91cd\u6392": "rerank", "\u566a\u58f0": "noise",
    "\u62fc\u97f3": "pinyin", "\u8bc1\u636e": "evidence", "\u6293\u53d6": "fetch", "\u7194\u65ad": "breaker",
    "\u805a\u5408": "search_engines", "\u5e76\u53d1": "concurrency", "\u9650\u6d41": "ratelimit",
    "\u8d85\u65f6": "timeout", "\u91cd\u8bd5": "retry", "\u5f02\u6b65": "async", "\u961f\u5217": "queue",
    "\u7d22\u5f15": "index", "\u5411\u91cf": "vector", "\u6743\u91cd": "weight", "\u9608\u503c": "threshold",
    "\u5ef6\u8fdf": "latency", "\u541e\u5410": "throughput", "\u6743\u9650": "permission", "\u8ba4\u8bc1": "auth",
    "\u52a0\u5bc6": "crypto", "\u89e3\u6790": "parse", "\u5e8f\u5217": "serialize", "\u8fdb\u7a0b": "process",
    "\u7ebf\u7a0b": "thread", "\u9501": "lock", "\u8c03\u5ea6": "dispatch", "\u964d\u7ea7": "fallback",
    "\u6062\u590d": "recovery", "\u53bb\u91cd": "dedup", "\u65f6\u6548": "freshness", "\u6743\u5a01": "authority",
    "\u914d\u989d": "quota", "\u95e8\u7981": "gate", "\u6570\u636e": "data", "\u5b9a\u65f6": "schedule",
    "\u7b56\u7565": "policy", "\u6307\u6807": "metrics", "\u7ec8\u7aef": "cli", "\u63a5\u53e3": "api",
    "\u6e32\u67d3": "render", "\u89c6\u9891": "video", "\u5b57\u5e55": "subtitle", "\u7ec4\u4ef6": "component",
    "\u5185\u5b58": "memory", "\u65e5\u5fd7": "log", "\u90e8\u7f72": "deploy", "\u7f51\u7edc": "network",
    "\u4ee3\u7406": "proxy", "\u6570\u636e\u5e93": "db",
}

_DEF_LINE_RE = re.compile(
    r"^\s*(?:(?:async|pub|export|public|private|protected|static|inline|const)\s+)*"
    r"(?:def|class|fn|func|struct|interface|enum|type|trait|mod)\s+[A-Za-z_]"
)


def _line_buf_key(item: tuple[int, str]) -> tuple[int, int, str]:
    ln, txt = item
    if _DEF_LINE_RE.match(txt):
        pri = 0
    elif txt.lstrip().startswith(("#", "//", "/*", "*")):
        pri = 2
    else:
        pri = 1
    return pri, ln, txt


def _select_pool(candidates, cap: int, per_file: int = 8,
                 raw_query: str = "") -> list:
    """有界池的**按文件轮转配额 + 词覆盖度优先**选择（2026-10-07 方案甲，2026-10-30 升级）。

    流式阶段按文件收集候选（单文件有界缓冲 32 行，零字符串转换开销）；
    仅当总候选超 cap 或单文件超 per_file 时，按「行内词元覆盖数 + 路径互补词元数」
    精选每文件代表行并按文件覆盖度轮转入池。
    """
    raw_by_file: dict[str, list[tuple[int, str]]] = {}
    buf_limit = max(per_file * 4, 32)
    total_cands = 0
    for fp, ln, txt in candidates:
        total_cands += 1
        rows = raw_by_file.setdefault(fp, [])
        if len(rows) < buf_limit:
            rows.append((ln, txt))
        elif buf_limit > 1:
            worst = max(rows, key=_line_buf_key)
            if _line_buf_key((ln, txt)) < _line_buf_key(worst):
                rows[rows.index(worst)] = (ln, txt)

    if not raw_by_file:
        return []

    # 快路：总候选未超池上限且每文件均未超配额时，直接按基准序返回（零分词开销）
    if total_cands <= cap and all(len(v) <= per_file for v in raw_by_file.values()):
        return [(fp, ln, txt) for fp in sorted(raw_by_file) for ln, txt in sorted(raw_by_file[fp])]

    q_terms: list[str] = []
    if raw_query:
        pats, _ = build_patterns(raw_query, exact=False)
        for p in pats:
            pl = p.lower()
            if len(pl) >= 2 and pl not in q_terms:
                q_terms.append(pl)
            en = _POOL_ZH_EN.get(pl)
            if en and en not in q_terms:
                q_terms.append(en)
        q_terms = q_terms[:8]

    by_file: dict[str, list[tuple[int, str]]] = {}
    file_cov: dict[str, int] = {}
    for fp, rows in raw_by_file.items():
        if not q_terms:
            rows.sort()
            by_file[fp] = rows[:per_file]
            file_cov[fp] = 0
            continue
        fp_low = fp.lower()
        p_mask = 0
        for idx, qt in enumerate(q_terms):
            if qt in fp_low:
                p_mask |= 1 << idx
        f_mask = p_mask
        scored: list[tuple[int, int, int, str]] = []
        for ln, txt in rows:
            txt_low = (txt or "").lower()
            l_mask = 0
            for idx, qt in enumerate(q_terms):
                if qt in txt_low:
                    l_mask |= 1 << idx
            f_mask |= l_mask
            scored.append((-(p_mask | l_mask).bit_count(), -(l_mask & ~p_mask).bit_count(), ln, txt))
        scored.sort()
        by_file[fp] = [(ln, txt) for _, _, ln, txt in scored[:per_file]]
        file_cov[fp] = f_mask.bit_count()

    ordered_files = sorted(by_file, key=lambda fp: (-file_cov[fp], fp))
    out: list = []
    consumed: dict[str, int] = {fp: 0 for fp in by_file}
    while len(out) < cap:
        progressed = False
        for fp in ordered_files:
            i = consumed[fp]
            if i < len(by_file[fp]):
                ln, txt = by_file[fp][i]
                out.append((fp, ln, txt))
                consumed[fp] = i + 1
                progressed = True
                if len(out) >= cap:
                    break
        if not progressed:
            break
    return out


def _finish_content(candidates, raw_query, context, count, fixed,
                    path, max_results, drop_noise):
    """内容搜索的共用收尾：有界池 → 确定性排序 → BM25F 重排 → 噪声地板。

    rg 路径与 FTS 快路（seek_fts）都必须走这里——两条来源、一套池化与
    排序口径，输出 schema 才能逐位一致。
    """
    cap = max(max_results, min(max_results * 4, 400))
    want_rerank = _rerank_wanted(raw_query, context, count, fixed)
    if want_rerank:
        cap = max(cap, _POOL_CAP_MAX)  # 重排开启：池子放宽，深行进得了评分视野
    out = _select_pool(candidates, cap, raw_query=raw_query if want_rerank else "")
    out.sort()  # 基准序 = (路径,行号,内容)：重排关闭时与 2026-10-06 口径逐位一致
    # BM25F 相关度重排（2026-10-07，方案甲）：池子的**选择**口径不变（与发射序
    # 无关），变的只是池内**呈现次序**——基线实测多词查询的 top5 常被配置/
    # 文档里的顺带提及占满，实现文件进不了前五。闸门与降级见 _rerank_content。
    # 注意：这里刻意不写具体查询例——本文件就在被搜语料里，注释里出现
    # 常见查询原词会给 eval 制造自污染（首轮实测踩过）。
    out = _rerank_content(out, raw_query, context, count, fixed)
    if drop_noise:
        return _apply_noise_floor(out, path, max_results)
    return out[:max_results]


def rg_search(patterns, path, excludes, exts, context, count, max_results,
              fixed, raw_query="", dots=False, drop_noise=True, timeout=None,
              since_ts=None, until_ts=None, fts_ok=True, symbol=False):
    timeout = _RUN_DEFAULT_TIMEOUT_S if timeout is None else timeout

    # FTS5 持久索引快路（opt-in，ARGO_LOCAL_FTS=1，2026-10-10 方案乙）：只在
    # 「单模式 + 非 count/context」时尝试，正则元字符由 query_suitable 拦下
    # （字面量标志 fixed 不参与判断——普通无元字符查询就是 fixed=True 进来的）。
    # 索引不可用/未建成时返回 None，自然落到下面的 rg 主路；契约见
    # seek_fts 模块 docstring（零命中回退、TTL 新鲜度、覆盖面对齐）。
    #
    # fts_ok（2026-10-12 修复）：调用方带 --type/--exclude 时必须为 False。
    # 快路只按内容命中，不会应用 rg 侧的 `-g !glob` / `-g *.ext` 过滤——
    # 此前带过滤条件仍走快路，静默返回范围外的结果（--type py 会混入非 .py，
    # --exclude 被忽略还会绕掉 eval 自污染的排除守卫）。
    if fts_ok and not count and context == 0 and len(patterns) == 1:
        try:
            import seek_fts
        except ImportError:
            seek_fts = None
        if seek_fts is not None and seek_fts.query_suitable(patterns[0]):
            rows = seek_fts.content_candidates(
                patterns[0], path, dots=dots,
                since_ts=since_ts, until_ts=until_ts)
            if rows is not None:
                return _finish_content(iter(rows), raw_query, context, count,
                                       fixed, path, max_results,
                                       drop_noise), None

    def build(fixed, drop_noise=True):
        cmd = ["rg", "--line-number", "--no-heading", "-i", "--color", "never"]
        if dots:
            # rg 默认既不进以 . 开头的目录，也不跟软链。本机 skill 就放在
            # ~/.agents、~/.zcode 这类目录里，很多条目还是软链，所以两个都要打开。
            # 排除规则里的 !.git 仍然生效，不会把 .git 扫进来。
            cmd += ["--hidden", "--follow"]
        if fixed:
            cmd.append("-F")
        elif needs_pcre2(raw_query):
            if pcre2_supported():
                cmd.append("--pcre2")
            else:
                return None, ("本机 rg 未编译 PCRE2，不支持 look-around/反向引用语法，"
                              "请简化查询（如去掉 (?=、(?! 等结构）")
        if symbol:
            cmd.append("-w")
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

    cmd, err = build(fixed, drop_noise=drop_noise)
    if err:
        return [], err
    proc = run(cmd, timeout=timeout)
    if (proc is not None and proc.returncode == 2 and not fixed
            and "regex parse error" in (proc.stderr or "")):
        # regex 解析失败（如按字面意图输入 interface{}、foo.bar），回退固定字符串
        cmd2, _ = build(True, drop_noise=drop_noise)
        proc = run(cmd2, timeout=timeout)
    if proc is None or proc.returncode not in (0, 1, 2):
        return [], "rg 执行失败"
    if proc.returncode == 1:
        return [], None  # 无匹配，正常
    if proc.returncode == 2:
        # 返回码 2 =「有结果，但过程中报了错」（悬空软链 os error 2、权限
        # 不足等）：rg 跳过出错路径继续搜，stdout 里其余文件的匹配照常有效
        # ——与 _structural_search 同口径，有结果就放行下方正常解析，不再
        # 整个放弃（2026-10-31 实测：--dot 搜含悬空软链的 ~/.agents/skills，
        # 一条「No such file or directory」让整个查询空手而归）。stderr 的
        # 路径级警告由 rg 自行打印，不进 err——上层 err 非 None 即判失败。
        if not (proc.stdout or "").strip():
            err = (proc.stderr or "").strip()
            return [], f"rg 错误: {err[:200]}" if err else "rg 执行错误"
    if count:
        # 全量收集后按命中数降序取前 N：rg --count-matches 的输出顺序是
        # 目录遍历序，直接截断会让「哪些文件命中最多」系统性答错——
        # 命中最多的文件可能恰好排在遍历序后面。行数=文件数，全量收集有界。
        counts = []
        for line in proc.stdout.splitlines():
            if ":" in line:
                fp, _, n = line.rpartition(":")
                if _in_time_window(fp, since_ts, until_ts):
                    counts.append((fp, int(n) if n.isdigit() else 0, ""))
        # 并列计数按路径升序决出稳定次序：sort 稳定 + rg 发射序不定的组合，
        # 会让并列文件在两次运行里顺序不同
        counts.sort(key=lambda t: (-t[1], t[0]))
        return _apply_noise_floor(counts, path, max_results) if drop_noise \
            else counts[:max_results], None
    out = []
    # 上界池 + **确定性选择**：rg 并行遍历的发射序不稳定，「先到先得」截断
    # 拿到的是任意的 cap 条——同一查询两次跑、命中集与排序都不同（v1.4.1
    # 审计遗留，2026-10-06 定性修复）。池子选择与发射序无关、内存有界、
    # 单遍流式（2026-10-07 升级为按文件轮转配额，见 _select_pool：修复
    # 「单文件高频命中挤占池位、路径序靠后文件整体出局」的多样性缺陷）。
    # 收尾（池化→排序→重排→噪声地板）在 _finish_content，与 FTS 快路共用。
    candidates = (
        (m.group(1), int(m.group(2)), truncate(m.group(3)))
        for line in proc.stdout.splitlines()
        if (m := re.match(r"^(.*?):(\d+):(.*)$", line))
        # 时间窗在入池前过滤：窗外行不占上界名额（先截断后过滤会漏报）
        and _in_time_window(m.group(1), since_ts, until_ts)
    )
    return _finish_content(candidates, raw_query, context, count, fixed,
                           path, max_results, drop_noise), None


# ── Office 办公文档解包透视（纯 Python 标准库，零第三方依赖）──────────────

def _extract_office_items(file_path: Path) -> list[tuple[int, str]]:
    """从 docx/pptx/xlsx 等 Office zip 容器中免依赖流式提取 (序号, 文本)。"""
    ext = file_path.suffix.lower().lstrip(".")
    if ext not in OFFICE_ZIP_EXTS:
        return []
    import zipfile
    import xml.etree.ElementTree as ET
    items: list[tuple[int, str]] = []
    try:
        with zipfile.ZipFile(file_path, "r") as z:
            namelist = set(z.namelist())
            if ext == "docx" and "word/document.xml" in namelist:
                tree = ET.fromstring(z.read("word/document.xml"))
                p_count = 0
                for p in tree.iter():
                    if p.tag.endswith("}p"):
                        texts = [node.text for node in p.iter() if node.tag.endswith("}t") and node.text]
                        if texts:
                            p_text = "".join(texts).strip()
                            if p_text:
                                p_count += 1
                                items.append((p_count, f"[段落{p_count}] {p_text}"))
            elif ext == "pptx":
                slide_names = sorted(
                    [n for n in namelist if n.startswith("ppt/slides/slide") and n.endswith(".xml")],
                    key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', x)]
                )
                for s_idx, s_name in enumerate(slide_names):
                    tree = ET.fromstring(z.read(s_name))
                    texts = [node.text for node in tree.iter() if node.tag.endswith("}t") and node.text]
                    if texts:
                        s_text = " ".join(texts).strip()
                        if s_text:
                            items.append((s_idx + 1, f"[幻灯片{s_idx + 1}] {s_text}"))
            elif ext == "xlsx":
                shared_strings: list[str] = []
                if "xl/sharedStrings.xml" in namelist:
                    tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
                    for si in tree.iter():
                        if si.tag.endswith("}si"):
                            texts = [node.text for node in si.iter() if node.tag.endswith("}t") and node.text]
                            shared_strings.append("".join(texts))
                sheet_names = sorted(
                    [n for n in namelist if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")],
                    key=lambda x: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', x)]
                )
                item_idx = 0
                for s_idx, s_name in enumerate(sheet_names):
                    tree = ET.fromstring(z.read(s_name))
                    for c in tree.iter():
                        if c.tag.endswith("}c"):
                            cell_ref = c.attrib.get("r", "")
                            cell_type = c.attrib.get("t", "")
                            v_node = [node.text for node in c.iter() if node.tag.endswith("}v") and node.text]
                            val = ""
                            if v_node:
                                v_str = v_node[0]
                                if cell_type == "s" and v_str.isdigit() and int(v_str) < len(shared_strings):
                                    val = shared_strings[int(v_str)]
                                else:
                                    val = v_str
                            if val and val.strip():
                                item_idx += 1
                                r_match = re.search(r'\d+', cell_ref)
                                row_num = int(r_match.group(0)) if r_match else item_idx
                                items.append((row_num, f"[Sheet{s_idx + 1}:{cell_ref}] {val.strip()}"))
    except Exception:
        return []
    return items


def office_search(patterns: list[str], path: Path, excludes: list[str],
                  exts: list[str], count: bool, max_results: int,
                  fixed: bool, raw_query: str = "", dots: bool = False,
                  drop_noise: bool = True, since_ts: float | None = None,
                  until_ts: float | None = None) -> list[tuple[str, int, str]]:
    """扫描目标路径下的 Office 容器文件并提取匹配文本。"""
    active_exts = (set(exts) & OFFICE_ZIP_EXTS) if exts else OFFICE_ZIP_EXTS
    if not active_exts:
        return []

    compiled = []
    for p in patterns:
        try:
            pattern_str = re.escape(p) if fixed else p
            compiled.append(re.compile(pattern_str, re.IGNORECASE))
        except re.error:
            pass
    if not compiled:
        return []

    candidates = []
    counts = []

    if path.is_file():
        files_to_scan = [path] if path.suffix.lower().lstrip(".") in active_exts else []
    else:
        files_to_scan = []
        for root, dirs, files in os.walk(path):
            if not dots:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
            if any(exc in root for exc in excludes if exc):
                continue
            for f in files:
                ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                if ext in active_exts:
                    fp = Path(root) / f
                    if _in_time_window(str(fp), since_ts, until_ts):
                        files_to_scan.append(fp)

    for fp in files_to_scan:
        fp_str = str(fp)
        items = _extract_office_items(fp)
        matched_in_file = 0
        for line_num, txt in items:
            if any(c.search(txt) for c in compiled):
                matched_in_file += 1
                if not count:
                    candidates.append((fp_str, line_num, truncate(txt)))
        if count and matched_in_file > 0:
            counts.append((fp_str, matched_in_file, ""))

    if count:
        counts.sort(key=lambda t: (-t[1], t[0]))
        return counts[:max_results]

    if not candidates:
        return []

    return _finish_content(iter(candidates), raw_query, 0, count, fixed,
                           path, max_results, drop_noise)


def _merge_content_dedup(a: list, b: list, limit: int) -> list:
    """按 (文件路径, 行号) 去重合并两组正文搜索结果。"""
    seen, out = set(), []
    for it in list(a) + list(b):
        key = (it[0], it[1]) if isinstance(it, (list, tuple)) and len(it) >= 2 else str(it)
        if key not in seen:
            seen.add(key)
            out.append(it)
        if len(out) >= limit:
            break
    return out


# ── fzf FuzzyMatchV2 评分 + 拼音首字母（pypinyin 优先，GB2312 表保底）──────────
# 评分常数与对齐算法按 junegunn/fzf src/algo/algo.go 的 FuzzyMatchV2 口径实现
# （2026-10-07 方案甲：原手搓版只有「边界+4/连续+3/全等+5」三档，无 gap 惩罚、
# 无 camelCase 加成，歧义文件名的 top1 判定比 fzf 真版粗一档）。DP 对齐保证
# 最优匹配位置（xaabc 搜 ab 时「跳过词首 a 取边界 b」的得分高于顺位连打，
# 顺位贪心永远发现不了这类反超）；文件名长度量级下 O(m·n) 开销微秒级。

_SCORE_MATCH = 16
_GAP_START = -3
_GAP_EXT = -1
_BONUS_BOUNDARY = _SCORE_MATCH // 2              # 8：串首/路径段/词边界
_BONUS_NON_WORD = _SCORE_MATCH // 2              # 8：其他非词字符之后
_BONUS_CAMEL = _BONUS_BOUNDARY + _GAP_EXT        # 7：驼峰大写/字母→数字
_BONUS_CONSECUTIVE = -(_GAP_START + _GAP_EXT)    # 4：连续命中
_FIRST_CHAR_MULT = 2                             # 首个模式字符的边界加成翻倍
_EXACT_CASE_BONUS = _SCORE_MATCH                 # 全等（区分大小写）
_EXACT_BONUS = _SCORE_MATCH // 2                 # 全等（仅忽略大小写）


def _match_bonus(prev: str | None, ch: str) -> int:
    """n[j] 作为匹配点时的边界加成（fzf 的 bonusFor 口径）。"""
    if prev is None:
        return _BONUS_BOUNDARY
    if prev in "/-_. ":
        return _BONUS_BOUNDARY
    if prev.islower() and ch.isupper():
        return _BONUS_CAMEL
    if prev.isalpha() and ch.isdigit():
        return _BONUS_CAMEL
    if not prev.isalnum():
        return _BONUS_NON_WORD
    return 0


def _fzf_score(name: str, pattern: str) -> int:
    """fzf FuzzyMatchV2 文件名匹配评分（smart case + DP 最优对齐）。0 表示不匹配。

    M[i][j] = 模式 p[:i+1] 匹配且 p[i] 恰落在 n[j] 的最优分；
    连续命中走 M[i-1][j-1] + 连续加成；有间隙的转移用仿射 gap 惩罚
    （GAP_START + GAP_EXT×(g-1)），经前缀最大值 H[i-1][j'] + (-GAP_EXT)×j'
    折算成 O(m·n) 单遍——文件名长度量级下等价于 fzf 的滑窗实现。
    """
    if not pattern or not name:
        return 0
    p, n = pattern, name
    if not any(c.isupper() for c in p):  # smart case：无大写则忽略大小写
        p, n = p.lower(), n.lower()
    m, ln = len(p), len(n)
    if m > ln:
        return 0
    NEG = float("-inf")
    prev_m: list[float] = [NEG] * ln   # 上一模式字符的 M 值（按 n 下标）
    prev_h: list[float] = [NEG] * ln   # 上一行 M 的前缀最大值
    prev_pm: list[float] = [NEG] * ln  # max_{j'≤k}(prev_h[j'] + j')，仿射 gap 用
    for i in range(m):
        pc = p[i]
        cur_m: list[float] = [NEG] * ln
        cur_h: list[float] = [NEG] * ln
        for j in range(ln):
            if n[j] != pc:
                continue
            if i == 0:
                cur_m[j] = _SCORE_MATCH + \
                    _match_bonus(n[j - 1] if j else None, n[j]) * _FIRST_CHAR_MULT
            else:
                best = NEG
                if j >= 1 and prev_m[j - 1] != NEG:
                    best = prev_m[j - 1] + _SCORE_MATCH + _BONUS_CONSECUTIVE
                if j >= 2 and prev_pm[j - 2] != NEG:
                    best = max(best, prev_pm[j - 2] + _GAP_START + 2 - j)
                if best != NEG:
                    cur_m[j] = best + _SCORE_MATCH + \
                        _match_bonus(n[j - 1] if j else None, n[j])
            if cur_m[j] != NEG:
                cur_h[j] = max(cur_h[j - 1] if j else NEG, cur_m[j])
        run = NEG
        for j in range(ln):
            if cur_h[j] != NEG:
                run = max(run, cur_h[j] + j)
            prev_pm[j] = run
        prev_m, prev_h = cur_m, cur_h
    best = max(prev_h) if ln else NEG
    if best == NEG:
        return 0
    score = int(best)
    if p == n:
        score += _EXACT_CASE_BONUS
    elif p == n.lower():
        score += _EXACT_BONUS
    return score
_GB2312_SECTIONS = (
    (0xB0A1, 0xB0C4, "a"), (0xB0C5, 0xB2C0, "b"), (0xB2C1, 0xB4ED, "c"),
    (0xB4EE, 0xB6E9, "d"), (0xB6EA, 0xB7A1, "e"), (0xB7A2, 0xB8C0, "f"),
    (0xB8C1, 0xB9FD, "g"), (0xB9FE, 0xBBF6, "h"), (0xBBF7, 0xBFA5, "j"),
    (0xBFA6, 0xC0AB, "k"), (0xC0AC, 0xC2E7, "l"), (0xC2E8, 0xC4C2, "m"),
    (0xC4C3, 0xC5B5, "n"), (0xC5B6, 0xC5BD, "o"), (0xC5BE, 0xC6D9, "p"),
    (0xC6DA, 0xC8BA, "q"), (0xC8BB, 0xC8F5, "r"), (0xC8F6, 0xCBF9, "s"),
    (0xCBFA, 0xCDD9, "t"), (0xCDDA, 0xCEF3, "w"), (0xCEF4, 0xD1B8, "x"),
    (0xD1B9, 0xD4D0, "y"), (0xD4D1, 0xD7F9, "z"),
)


def _gb2312_initial(b1: int, b2: int) -> str:
    code = (b1 << 8) | b2
    for lo, hi, letter in _GB2312_SECTIONS:
        if lo <= code <= hi:
            return letter
    return "?"


_PYPINYIN_FN = None
_PYPINYIN_PROBED = False


def pinyin_initials(text: str) -> str:
    """文本的拼音首字母（中文→首字母，非中文保留原字符），用于「xjj→新建夹」。
    pypinyin 可用则用；否则 GB2312 区间表保底；两者都不可用时返回原文本。"""
    global _PYPINYIN_FN, _PYPINYIN_PROBED
    if not text:
        return text
    if not _PYPINYIN_PROBED:
        _PYPINYIN_PROBED = True
        try:
            from pypinyin import lazy_pinyin, Style  # type: ignore
            _PYPINYIN_FN = lambda s: "".join(  # noqa: E731
                lazy_pinyin(s, style=Style.FIRST_LETTER, errors="default")
            )
        except Exception:
            _PYPINYIN_FN = None
    if _PYPINYIN_FN is not None:
        try:
            return _PYPINYIN_FN(text)
        except Exception:
            pass
    out = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            try:
                b = ch.encode("gb2312")
                out.append(_gb2312_initial(b[0], b[1]))
            except (UnicodeEncodeError, IndexError):
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _looks_like_pinyin_abbrev(q: str) -> bool:
    """疑似中文拼音缩写（如 xjj）：纯 ASCII 字母、2-4 位。"""
    q = (q or "").strip()
    return bool(q and q.isascii() and q.isalpha() and 2 <= len(q) <= 4)


def _file_pinyin_bonus(path: str, query: str,
                       query_initials: str | None = None) -> int:
    """文件名拼音首字母与查询呼应 → 加分（「新建夹」↔ xjj）。
    正向（中文查询 → 拼音缩写）50；反向（拼音缩写查询 → 中文文件名）8，
    反向低于字面命中，避免把真名 xjj.docx 挤下去。"""
    q = query.strip().lower()
    if not q:
        return 0
    base = Path(path).name
    has_cjk_query = any("\u4e00" <= c <= "\u9fff" for c in query)
    is_abbrev = (not has_cjk_query and q.isascii() and q.isalpha()
                 and 2 <= len(q) <= 4
                 and any("\u4e00" <= c <= "\u9fff" for c in base))
    if not has_cjk_query and not is_abbrev:
        return 0
    initials = pinyin_initials(base).lower()
    if not initials:
        return 0
    if has_cjk_query:
        qs = (query_initials if query_initials is not None
              else pinyin_initials(query).strip().lower())
        return 50 if qs and qs in initials else 0
    return 8 if q in initials else 0


def _rank_path_results(results, query: str):
    """按文件名相关性排序：fzf V2 评分 + 拼音加分 + frecency（opt-in）+ mtime 新优先。

    仅用于「按文件定位」场景（--filename / --spotlight）；内容命中的相关度
    次序由 _rerank_content（BM25F）负责，不走这里。
    """
    frec_on, boost_fn = False, None
    try:
        import seek_frecency
        frec_on, boost_fn = seek_frecency.enabled(), seek_frecency.boost
    except Exception:
        pass
    now = time.time()
    q_low = query.lower()
    q_init = (pinyin_initials(query).strip().lower()
              if any("\u4e00" <= c <= "\u9fff" for c in query) else "")

    def key(it):
        fp = it[0] if isinstance(it, (list, tuple)) else str(it)
        sc = _fzf_score(Path(fp).name, query)
        sc += _file_pinyin_bonus(fp, query, query_initials=q_init)
        # 全路径段匹配也加分。V2 的分数量级是旧手搓版的 ~4 倍（基分 16 vs 3-4），
        # 除数同步 4→16，保持「路径段是文件名的从属信号」这一原比例——
        # 不调的话全路径匹配会反超文件名匹配 + 拼音加成，排序语义就变了。
        sc += max(0, _fzf_score(fp, q_low)) // 16
        if frec_on and boost_fn is not None:
            try:
                sc += boost_fn(fp, now)
            except Exception:
                pass
        try:
            mt = os.path.getmtime(fp)
        except OSError:
            mt = 0.0
        return (-sc, -mt, fp)
    return sorted(results, key=key)


def _merge_dedup(a: list, b: list, limit: int) -> list:
    """按路径去重合并两组结果（fd 中文 + 拼音双查）。"""
    seen, out = set(), []
    for it in list(a) + list(b):
        fp = it[0] if isinstance(it, (list, tuple)) else str(it)
        if fp not in seen:
            seen.add(fp)
            out.append(it)
        if len(out) >= limit:
            break
    return out


def fd_search(query, path, exts, max_results, dots=False, timeout=None,
              since_ts=None, until_ts=None):
    timeout = _RUN_DEFAULT_TIMEOUT_S if timeout is None else timeout
    cmd = ["fd", "-i", "-t", "f", "--color", "never"]
    if dots:
        cmd += ["-H", "-L"]  # -H 进以 . 开头的目录、-L 跟软链，和 rg 的两个开关对应
    for ex in DEFAULT_EXCLUDES:
        cmd += ["-E", ex]
    if exts:
        for e in exts:
            cmd += ["-e", e]
    cmd += [query, str(path)]
    proc = run(cmd, timeout=timeout)
    if proc is None:
        return [], "fd 执行失败"
    # fd 对个别路径出错（悬空软链等）退出码落到 0/1 之外，但 stdout 里其余
    # 匹配照常有效——有结果就解析，与 rg_search 同口径（2026-10-31）。
    if proc.returncode not in (0, 1) and not (proc.stdout or "").strip():
        return [], "fd 执行失败"
    # 时间窗在截断前过滤（先截后滤会漏报，与 rg 路径同理）
    lines = [p for p in proc.stdout.splitlines()
             if _in_time_window(p, since_ts, until_ts)]
    return [(p, 0, "") for p in lines[:max_results]], None


def resolve_grep():
    p = shutil.which("grep")
    if p:
        return p
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            cand = Path(git).resolve().parent.parent / "usr" / "bin" / "grep.exe"
            if cand.exists():
                return str(cand)
    return None


def grep_search(grep_exe, patterns, path, excludes, exts, context, count,
                max_results, fixed, raw_query="", dots=False, timeout=None,
                since_ts=None, until_ts=None):
    timeout = _RUN_DEFAULT_TIMEOUT_S if timeout is None else timeout
    def build(fixed):
        # grep 本来就会进以 . 开头的目录，差别在软链：-r 遇到软链目录不进，-R 才跟。
        # 但 macOS 自带的 grep 连 -R 也不进软链目录（实测），所以这一路只在没装 rg 时用。
        cmd = [grep_exe, "-R" if dots else "-r", "-n", "-i", "-I", "--color=never"]
        if fixed:
            cmd.append("-F")
        elif needs_pcre2(raw_query):
            return None, ("grep 不支持 look-around/反向引用语法，"
                          "请简化查询（如去掉 (?=、(?! 等结构）或安装 ripgrep")
        if count:
            cmd.append("-c")
        elif context > 0:
            cmd += ["-C", str(context)]
        for ex in excludes:
            if any(c in ex for c in "*?["):
                cmd += ["--exclude", ex]
            else:
                cmd += ["--exclude-dir", ex]
        if exts:
            for e in exts:
                cmd += ["--include", f"*.{e}"]
        for p in patterns:
            cmd += ["-e", p]
        cmd.append(str(path))
        return cmd, None

    cmd, err = build(fixed)
    if err:
        return [], err
    proc = run(cmd, timeout=timeout)
    if (proc is not None and proc.returncode == 2 and not fixed
            and "grep:" in (proc.stderr or "")):
        cmd2, _ = build(True)
        proc = run(cmd2, timeout=timeout)
    if proc is None or proc.returncode not in (0, 1, 2):
        return [], "grep 执行失败"
    # 返回码 1 不等于「没搜到」：macOS 自带的 grep 跟随软链时，会一边输出结果
    # 一边返回 1（实测输出了 3972 行，返回码仍是 1）。输出为空才算真的没搜到。
    if proc.returncode == 1 and not proc.stdout.strip():
        return [], None
    # 确定性选择与 rg 路径同款（grep/fd 同样并行发射序不定）
    if count:
        counts = []
        for line in proc.stdout.splitlines():
            if ":" in line:
                fp, _, n = line.rpartition(":")
                # grep -c 会把没有命中的文件也列出来，写成 file:0。这种不算命中，
                # 丢掉；这样和 rg --count-matches 只列命中的文件保持一致。
                if n.isdigit() and int(n) > 0 and _in_time_window(fp, since_ts, until_ts):
                    counts.append((fp, int(n), ""))
        counts.sort(key=lambda t: (-t[1], t[0]))
        return counts[:max_results], None
    candidates = (
        (m.group(1), int(m.group(2)), truncate(m.group(3)))
        for line in proc.stdout.splitlines()
        if (m := re.match(r"^(.*?):(\d+):(.*)$", line))
        and _in_time_window(m.group(1), since_ts, until_ts)
    )
    return heapq.nsmallest(max_results, candidates,
                           key=lambda t: (t[0], t[1], t[2])), None


def mdfind_search(query, path, max_results, timeout=None,
                  since_ts=None, until_ts=None):
    timeout = _RUN_DEFAULT_TIMEOUT_S if timeout is None else timeout
    cmd = ["mdfind"]
    if path and str(path) != ".":
        cmd += ["-onlyin", str(Path(path).expanduser())]
    cmd += [query]
    proc = run(cmd, timeout=timeout)
    if proc is None:
        return [], "mdfind 执行失败"
    # 时间窗在截断前过滤（先截后滤会漏报，与 rg 路径同理）
    lines = [p for p in proc.stdout.splitlines()
             if _in_time_window(p, since_ts, until_ts)]
    return [(p, 0, "") for p in lines[:max_results]], None


_BINARY_EXTS = frozenset({
    "exe", "dll", "so", "dylib", "bin", "o", "a", "lib",
    "png", "jpg", "jpeg", "gif", "ico", "webp", "bmp", "psd",
    "mp3", "mp4", "mov", "avi", "mkv", "flv", "wav",
    "zip", "tar", "gz", "7z", "bz2", "xz", "rar",
    "pyc", "pyo", "pyd", "class", "db", "sqlite", "sqlite3"
})


def py_walk_filename(query: str, path: Path, exts: list[str], max_results: int,
                     dots: bool = False, timeout: float | None = None,
                     since_ts: float | None = None, until_ts: float | None = None) -> tuple[list, str | None]:
    """纯 Python 标准库文件名检索（无 fd 时的全平台保底，零外部依赖）。"""
    timeout = _RUN_DEFAULT_TIMEOUT_S if timeout is None else timeout
    deadline = (time.time() + timeout) if timeout else None
    q_low = query.lower()
    matches = []
    excludes = set(load_excludes())
    root_str = str(Path(path).expanduser())

    for root, dirs, files in os.walk(root_str):
        if deadline and time.time() > deadline:
            break
        if not dots:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        dirs[:] = [d for d in dirs if d not in excludes and not any(ex in d for ex in excludes if "*" in ex)]

        for f in files:
            if not dots and f.startswith("."):
                continue
            if exts:
                suffix = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                if suffix not in exts:
                    continue
            if not query or q_low in f.lower():
                fp = os.path.join(root, f)
                if _in_time_window(fp, since_ts, until_ts):
                    matches.append((fp, 0, ""))
                    if len(matches) >= max_results * 4:
                        break
        if len(matches) >= max_results * 4:
            break

    ranked = _rank_path_results(matches, query) if matches else []
    return ranked[:max_results], None


def py_walk_search(patterns: list[str], path: Path, excludes: list[str], exts: list[str],
                   context: int, count: bool, max_results: int, fixed: bool,
                   raw_query: str = "", dots: bool = False, timeout: float | None = None,
                   since_ts: float | None = None, until_ts: float | None = None) -> tuple[list, str | None]:
    """纯 Python 标准库文件内容检索（无 rg 与 grep 时的全平台保底，零外部依赖）。"""
    timeout = _RUN_DEFAULT_TIMEOUT_S if timeout is None else timeout
    deadline = (time.time() + timeout) if timeout else None
    ex_set = set(excludes)
    pats = patterns or ([raw_query] if raw_query else [])
    if not pats:
        return [], None

    regexes = None
    if not fixed:
        regexes = []
        for p in pats:
            try:
                regexes.append(re.compile(p, re.IGNORECASE))
            except re.error:
                regexes.append(re.compile(re.escape(p), re.IGNORECASE))
    pats_lower = [p.lower() for p in pats]

    counts = []
    candidates = []
    root_str = str(Path(path).expanduser())

    for root, dirs, files in os.walk(root_str):
        if deadline and time.time() > deadline:
            break
        if not dots:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        dirs[:] = [d for d in dirs if d not in ex_set]

        for f in files:
            if deadline and time.time() > deadline:
                break
            if not dots and f.startswith("."):
                continue
            suffix = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            if exts:
                if suffix not in exts:
                    continue
            elif suffix in _BINARY_EXTS:
                continue

            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
                if st.st_size > 5 * 1024 * 1024 or st.st_size == 0:
                    continue
            except OSError:
                continue

            if not _in_time_window(fp, since_ts, until_ts):
                continue

            file_matched_lines = 0
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    for line_idx, line in enumerate(fh, start=1):
                        matched = False
                        if fixed:
                            line_low = line.lower()
                            matched = any(p in line_low for p in pats_lower)
                        elif regexes:
                            matched = any(rgx.search(line) for rgx in regexes)

                        if matched:
                            file_matched_lines += 1
                            if not count:
                                candidates.append((fp, line_idx, truncate(line)))
                                if len(candidates) >= _POOL_CAP_MAX:
                                    break
            except (OSError, UnicodeError):
                continue

            if count and file_matched_lines > 0:
                counts.append((fp, file_matched_lines, ""))
                if len(counts) >= max_results * 2:
                    break
        if (count and len(counts) >= max_results * 2) or (not count and len(candidates) >= _POOL_CAP_MAX):
            break

    if count:
        counts.sort(key=lambda t: (-t[1], t[0]))
        return counts[:max_results], None

    if not candidates:
        return [], None

    return _finish_content(
        iter(candidates), raw_query, context, count, fixed,
        root_str, max_results, drop_noise=True
    ), None


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


def _cluster_results(results):
    """为同一文件紧邻（行号差 <= 4）的命中分配逻辑聚簇 ID。"""
    if not results:
        return []
    clusters = []
    cid = 1
    for i, (fp, ln, _txt) in enumerate(results):
        assigned = None
        if i > 0 and ln and ln > 0:
            prev_fp, prev_ln, _ = results[i - 1]
            if prev_fp == fp and prev_ln and prev_ln > 0 and abs(ln - prev_ln) <= 4:
                assigned = clusters[i - 1]
        if assigned is None:
            assigned = cid
            cid += 1
        clusters.append(assigned)
    return clusters


def to_json(results, engine, mode, scope, path, elapsed_ms, query, since=None, until=None):
    clusters = _cluster_results(results)
    return json.dumps({
        "query": query,
        "engine": engine,
        "mode": mode,
        "scope": scope,
        "path": str(path),
        "elapsed_ms": elapsed_ms,
        "count": len(results),
        "since": since,
        "until": until,
        "results": [
            {"path": fp, "line": ln, "snippet": txt, "mtime": _file_mtime(fp), "cluster": cid}
            for (fp, ln, txt), cid in zip(results, clusters)
        ],
    }, ensure_ascii=False, indent=2)


# ── 时间窗（文件修改时间过滤；统一出口，不涉公共缓存）────────────────
def parse_time(s: str | None) -> float | None:
    """相对（7d/12h/1w/1m/1y）或绝对（2026-08-01/ISO）→ unix 秒时间戳。"""
    if not s:
        return None
    s = str(s).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d+)([hdwmy])", s.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    now = time.time()
    delta = {"h": 3600, "d": 86400, "w": 7 * 86400,
             "m": 30 * 86400, "y": 365 * 86400}[unit]
    return now - n * delta


def _file_mtime(fp: str) -> str | None:
    try:
        return datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def _in_time_window(fp: str, since_ts: float | None, until_ts: float | None) -> bool:
    """单个文件是否落在 [since_ts, until_ts] 时间窗内（None = 该侧不限）。"""
    if not since_ts and not until_ts:
        return True
    try:
        mt = os.path.getmtime(fp)
    except OSError:
        return False
    if since_ts and mt < since_ts:
        return False
    if until_ts and mt > until_ts:
        return False
    return True


def filter_by_mtime(results, since: str | None, until: str | None):
    """时间窗过滤：仅保留文件 mtime 落在 [since, until] 内的命中。

    这是**兜底出口**（structural 等未下传时间戳的路径用）。主过滤在各搜索器
    **截断之前**完成（since_ts/until_ts 参数）——「先截 max 条再过滤」会系统性
    漏报（2026-10-03 实测：20 个新命中只报出 8 个，旧文件占满了截断池）。
    时间窗查询下文件已消失等 IO 异常条目剔除（与 local-search 无日期剔除同理）。
    """
    since_ts, until_ts = parse_time(since), parse_time(until)
    if not since_ts and not until_ts:
        return results
    return [(fp, ln, txt) for fp, ln, txt in results
            if _in_time_window(fp, since_ts, until_ts)]


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
    """按行读取文件（单遍流式，不加载全文）。返回 (输出文本, 退出码)。"""
    m = re.fullmatch(r"(\d+)-(\d+)", spec)
    if not m:
        return "local-seek: --lines 格式应为 N-M（如 10-20）", 1
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        return f"local-seek: 行范围无效 {spec}", 1
    p = Path(path)
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    # 单遍流式读取：边数总行数边收集目标区间。原实现先 open 数行、再 open
    # 读区间，同一文件开两遍——大文件时 IO 翻倍（2026-10-06 收口）。
    picked, total = [], 0
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f, 1):
                total = i
                if a <= i <= b:
                    picked.append(f"{i}: {ln.rstrip()}")
    except OSError as e:
        return f"local-seek: 读取失败 {e}", 1
    if a > total:
        return f"local-seek: {p.name} 只有 {total} 行，请求从 {a} 行开始", 1
    end = min(b, total)
    out = [f"local-seek: {p.name} 第 {a}-{end} 行 / 共 {total} 行"]
    return "\n".join(out + picked), 0


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


def structural_search(rule_name, path, excludes, max_results, dots=False,
                      timeout=None):
    """结构搜索：按语义规则（空 catch/裸 except/未包装错误/装饰函数等）检索。
    零安装实现：rg -U 多行 + 语言感知 pattern；本机装 ast-grep 后可升级为 AST 精确匹配。"""
    key = _STRUCT_ALIAS.get(rule_name.strip().lower())
    if not key:
        avail = "、".join(STRUCTURAL_RULES)
        return [], f"未知结构查询「{rule_name}」，可用：{avail}"
    results = []
    for exts, pat in STRUCTURAL_RULES[key]["patterns"]:
        cmd = ["rg", "--line-number", "--no-heading", "-U", "-i", "--color", "never"]
        if dots:
            cmd += ["--hidden", "--follow"]
        for ex in excludes:
            cmd += ["-g", f"!{ex}"]
        for e in exts:
            cmd += ["-g", f"*.{e}"]
        cmd += ["-e", pat, str(path)]
        # timeout 与其他搜索器同源：H2 后进程内调用传紧预算，缺省走 30s。
        # 此前漏下传，MCP 进程内调用可被挂满 30s 占死单线程 executor。
        proc = run(cmd, timeout=timeout)
        if proc is None:
            continue
        if proc.returncode == 1:
            continue  # 该语言无命中
        # 返回码 2 表示「有结果，但过程中报了错」（比如软链指向的文件不存在）。
        # 不能一见非零就当没结果，否则加上 --dot 以后，结构搜索会从有结果变成没找到。
        if proc.returncode not in (0, 2):
            continue
        for line in proc.stdout.splitlines():
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if m:
                results.append((m.group(1), int(m.group(2)), truncate(m.group(3))))
            if len(results) >= max_results:
                return results, None
    return results, None


def git_log(path, n=10, timeout=None):
    """输出文件的最近提交历史（git log --oneline）。"""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    proc = run(["git", "-C", str(p.parent), "log", "--oneline", f"-{n}", "--", p.name],
               timeout=timeout)
    if proc is None:
        return "local-seek: git 执行失败", 1
    if proc.returncode == 128 or (proc.returncode == 0 and not proc.stdout.strip()):
        inside = run(["git", "-C", str(p.parent), "rev-parse", "--is-inside-work-tree"])
        if inside is None or inside.returncode != 0:
            return f"local-seek: {p.name} 不在 git 仓库中", 1
        return f"local-seek: {p.name} 无提交历史", 0
    lines = proc.stdout.splitlines()
    return "\n".join([f"local-seek: {p.name} 最近 {len(lines)} 条提交"] + lines), 0


def git_blame(path, line, timeout=None):
    """输出文件第 N 行的 blame 信息（谁在哪个提交改的）。"""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"local-seek: {path} 不是文件", 1
    proc = run(["git", "-C", str(p.parent), "blame", "-L", f"{line},{line}", "--", p.name],
               timeout=timeout)
    if proc is None or proc.returncode != 0:
        return f"local-seek: git blame 失败（{p.name} 可能在 git 仓库外或行号无效）", 1
    return f"local-seek: {p.name} 第 {line} 行\n{proc.stdout.strip()}", 0


def _run_grep_fallback(args, patterns, fixed, path, excludes, exts, max_results,
                       timeout=None, since_ts=None, until_ts=None):
    """rg 缺失时的 FTS5 / grep 保底（目录内/全盘共用）。

    返回 (results, err, mode)；FTS5 索引命中时优先走标准库 sqlite3（免外部工具），
    rg 与 grep 都不可用且无 FTS5 索引时 results=None 且 err 说明缺什么——
    调用方必须把「工具缺失」与「未找到匹配」区分开。
    """
    dots = getattr(args, "dot", False)
    if not exts and not getattr(args, "exclude", None) and not args.count and args.context == 0:
        try:
            import seek_fts
        except ImportError:
            seek_fts = None
        if seek_fts is not None and seek_fts.query_suitable(args.query):
            rows = seek_fts.content_candidates(
                args.query, path, dots=dots,
                since_ts=since_ts, until_ts=until_ts)
            if rows is not None:
                return _finish_content(
                    iter(rows), args.query, args.context, args.count,
                    fixed, path, max_results, not args.include_noise
                ), None, "fts"
    grep_exe = resolve_grep()
    if not grep_exe:
        # 纯 Python 标准库保底（Windows / Linux 裸机环境开箱即用，零外部工具依赖）
        results, err = py_walk_search(
            patterns, path, excludes, exts, args.context, args.count,
            max_results, fixed, raw_query=args.query, dots=dots,
            timeout=timeout, since_ts=since_ts, until_ts=until_ts
        )
        if results:
            return results, None, "py-walk"
        return None, "本机无 rg 与 grep 可用（纯 Python 保底亦无匹配），请安装 ripgrep", ""
    mode = "fast"
    if not args.exact and len(patterns) > 1:
        results, err = grep_search(grep_exe, [args.query], path, excludes, exts,
                                   args.context, args.count, max_results,
                                   is_literal(args.query), dots=dots, timeout=timeout,
                                   since_ts=since_ts, until_ts=until_ts)
        if not results and not err:
            results, err = grep_search(grep_exe, patterns, path, excludes, exts,
                                       args.context, args.count, max_results,
                                       fixed, dots=dots, timeout=timeout,
                                       since_ts=since_ts, until_ts=until_ts)
            if results:
                mode += "+扩展"
    else:
        results, err = grep_search(grep_exe, patterns, path, excludes, exts,
                                   args.context, args.count, max_results, fixed,
                                   dots=dots, timeout=timeout,
                                   since_ts=since_ts, until_ts=until_ts)
    return results, err, mode


def run_query(argv=None, time_budget=None) -> tuple[str, int]:
    """执行一次 seek 查询，返回 (输出文本, 退出码)。

    H2（2026-09-29）：从 main() 重构出的**进程内可调用核心**——CLI 壳与
    include-local / MCP argo_local_search 的进程内调用共用同一实现（单一
    来源），CLI 行为逐字节不变（main 只负责 print + exit code）。
    time_budget：下传给各搜索器内部子进程（rg/fd/grep/mdfind）的超时上限；
    None = 沿用 30s 历史默认。进程内调用必须传紧值——子进程可硬杀，线程
    不可杀，不传会让挂死的 rg 占死单线程 executor。
    """
    ap = argparse.ArgumentParser(prog="seek", description="本地高效搜索统一入口")
    ap.add_argument("query", nargs="?", help="搜索查询词")
    ap.add_argument("--path", default=".", help="搜索目录（默认当前目录）")
    ap.add_argument("--scope", choices=["code", "doc", "office", "config", "all"], default="code",
                    help="code=正文+代码；doc/office=办公与文档；config=配置文件；all=Spotlight 兜底")
    ap.add_argument("--filename", action="store_true", help="按文件名查找（fd）")
    ap.add_argument("--spotlight", action="store_true", help="Spotlight 全盘兜底")
    ap.add_argument("--dot", action="store_true",
                    help="连以 . 开头的目录和软链一起搜（默认关；搜 ~/.agents、~/.zcode 时要加）")
    ap.add_argument("--include-noise", action="store_true",
                    help="不降权：repos/、tests/、tmp/、日期归档目录与真源平权"
                         "（默认排在真源之后，但仍可达，故通常无需显式加）")
    ap.add_argument("--type", default="", help="限定扩展名，逗号分隔（py,ts,md）")
    ap.add_argument("--count", action="store_true", help="只输出每文件命中数")
    ap.add_argument("--context", type=int, default=0, help="上下文行数（默认 0）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--max", type=int, default=0, help="最大结果数（默认读配置）")
    ap.add_argument("--exact", action="store_true", help="关闭中文扩展（精确匹配）")
    ap.add_argument("--symbol", action="store_true",
                    help="符号/词边界精确匹配（rg -w，过滤子串噪音）")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="额外排除 glob（可重复指定，如 --exclude eval_seek.py）")
    ap.add_argument("--since", default=None,
                    help="文件修改时间下限（7d / 2026-08-01），过滤命中文件的 mtime")
    ap.add_argument("--until", default=None,
                    help="文件修改时间上限（7d / 2026-08-01），过滤命中文件的 mtime")
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
    args = ap.parse_args(argv)

    # 知识域展示模式
    if args.domains:
        if DOMAINS_FILE.exists():
            return DOMAINS_FILE.read_text(encoding="utf-8"), 0
        return "未找到 config/domains.yaml，使用内置默认规则", 0

    # 文件结构 / 按行读取模式（文件路径优先取位置参数，否则取 --path）
    if args.outline or args.lines:
        target = args.query or str(args.path)
        if args.lines:
            return read_lines(target, args.lines)
        return outline_file(target)

    # git 联动模式：查文件提交历史 / 单行归属
    if args.git_log or args.git_blame:
        target = args.query or str(args.path)
        if args.git_blame:
            return git_blame(target, args.git_blame, timeout=time_budget)
        return git_log(target, timeout=time_budget)

    # 结构搜索模式：按语义规则检索（裸except/空catch/装饰函数等）
    if args.structural:
        if not args.query:
            return ap.format_help(), 0
        start = time.time()
        results, err = structural_search(args.query, Path(args.path).expanduser(),
                                         load_excludes() + args.exclude,
                                         args.max or 30, args.dot,
                                         timeout=time_budget)
        elapsed = int((time.time() - start) * 1000)
        results = filter_by_mtime(results, args.since, args.until)
        if err:
            return f"local-seek: {err}", 1
        if not results:
            return f"local-seek: 未找到匹配（rg-structural · {args.path} · {elapsed}ms）", 1
        if args.json:
            return to_json(results, "rg-structural", "structural", args.scope,
                           args.path, elapsed, args.query,
                           since=args.since, until=args.until), 0
        return format_output(results, "rg-structural", "structural",
                             args.path, elapsed, args.query, len(results)), 0

    if not args.query:
        return ap.format_help(), 0

    max_results = args.max or 30
    excludes = load_excludes() + args.exclude
    exts = [e.strip().lstrip(".") for e in args.type.split(",") if e.strip()]

    scope = args.scope
    if args.spotlight:
        scope = "all"
    # doc / office 场景补充文档与办公扩展名
    if scope in ("doc", "office") and not exts:
        exts = sorted(DOC_EXTS | OFFICE_ZIP_EXTS)
        scope = "code"
        if not tool_exists("rg"):
            scope = "all"
    elif scope == "config" and not exts:
        exts = sorted(CONFIG_EXTS)
        scope = "code"
        if not tool_exists("rg"):
            scope = "all"

    path = Path(args.path).expanduser()
    if not path.exists():
        # 返回 (text, rc) 而非直接 print：run_query 是进程内可调用核心，
        # main 只负责 print。此前在这里 print + 裸 return 1，违反契约，
        # MCP/include-local 等进程内调用方解包 (text, rc) 直接 TypeError。
        return f"local-seek: 目录不存在 {path}", 1

    engine = "rg"
    mode = "fast"
    start = time.time()
    results, err = [], None
    patterns, fixed = build_patterns(args.query, args.exact)
    if args.symbol:
        patterns, fixed = [args.query], is_literal(args.query)
    # 时间窗时间戳只解析一次，下传各搜索器在**截断前**过滤
    since_ts, until_ts = parse_time(args.since), parse_time(args.until)

    if scope == "all":
        # 全盘搜索：mdfind 优先（rg/grep 扫全盘太慢），缺 mdfind 退 grep
        if tool_exists("mdfind"):
            engine, mode = "mdfind", "deep"
            results, err = mdfind_search(args.query, None if args.spotlight else path,
                                         max_results, timeout=time_budget,
                                         since_ts=since_ts, until_ts=until_ts)
        else:
            results, err, gmode = _run_grep_fallback(
                args, patterns, fixed, path, excludes, exts, max_results,
                timeout=time_budget, since_ts=since_ts, until_ts=until_ts)
            if results is None:
                engine, mode = "none", "fast"
            else:
                engine, mode = "grep", gmode
    elif args.filename:
        engine, mode = "fd", "fast"
        if tool_exists("fd"):
            results, err = fd_search(args.query, path, exts, max_results, args.dot,
                                     timeout=time_budget,
                                     since_ts=since_ts, until_ts=until_ts)
            # 拼音首字母补充：中文查询 → 双查拼音缩写（「新建夹」↔ xjj）。
            # 先窄后宽：仅原结果 <3 且中文 ≥2 字时做（单字「新」→'x' 太宽泛，会引入噪音）
            if not err and len(results or []) < 3:
                _cjk_len = sum(1 for c in args.query if "\u4e00" <= c <= "\u9fff")
                q_py = pinyin_initials(args.query)
                if _cjk_len >= 2 and q_py and q_py.lower() != args.query.strip().lower():
                    py_res, py_err = fd_search(q_py, path, exts, max_results, args.dot,
                                               timeout=time_budget,
                                               since_ts=since_ts, until_ts=until_ts)
                    if not py_err and py_res:
                        results = _merge_dedup(results or [], py_res, max_results)
            # 拼音缩写反推：结果少且疑似缩写（xjj）→ 枚举候选按拼音首字母过滤
            if not err and (not results or len(results) < 3) and _looks_like_pinyin_abbrev(args.query):
                all_res, all_err = fd_search("", path, exts, 3000, args.dot,
                                             timeout=time_budget,
                                             since_ts=since_ts, until_ts=until_ts)
                if not all_err and all_res:
                    py_hits = [it for it in all_res if _file_pinyin_bonus(it[0], args.query) > 0]
                    if py_hits:
                        results = _merge_dedup(results or [], py_hits, max_results)
        else:
            # 纯 Python 标准库保底：系统未安装 fd 时（Windows/Linux 裸机环境），通过 os.walk 查找文件名
            results, err = py_walk_filename(
                args.query, path, exts, max_results, dots=args.dot,
                timeout=time_budget, since_ts=since_ts, until_ts=until_ts
            )
            engine = "py-walk"
            if not err and len(results or []) < 3:
                _cjk_len = sum(1 for c in args.query if "\u4e00" <= c <= "\u9fff")
                q_py = pinyin_initials(args.query)
                if _cjk_len >= 2 and q_py and q_py.lower() != args.query.strip().lower():
                    py_res, py_err = py_walk_filename(
                        q_py, path, exts, max_results, dots=args.dot,
                        timeout=time_budget, since_ts=since_ts, until_ts=until_ts
                    )
                    if not py_err and py_res:
                        results = _merge_dedup(results or [], py_res, max_results)
            if not results and not err:
                err = "fd 未安装（纯 Python 扫描亦无匹配）：按文件名搜索建议安装 fd"
    elif not tool_exists("rg"):
        # 目录内搜索缺 rg 必须落 grep，不能落 mdfind：Spotlight 不索引
        # 源码内容，会把「工具缺失」伪装成「搜索结论」（实测搜 argo 自身
        # 符号返回 24 条外部陈旧副本、漏掉正主，还报「未找到匹配」）。
        results, err, gmode = _run_grep_fallback(
            args, patterns, fixed, path, excludes, exts, max_results,
            timeout=time_budget, since_ts=since_ts, until_ts=until_ts)
        if results is None:
            engine, mode = "none", "fast"
        elif gmode == "fts":
            engine, mode = "fts", gmode
        elif gmode.startswith("py-walk"):
            engine, mode = "py-walk", gmode
        else:
            engine, mode = "grep", gmode
    else:
        mode = "deep" if (args.context > 0 or args.count) else "fast"
        # FTS 快路资格：带扩展名过滤、自定义排除或符号搜索时关闭（快路不做 rg 侧 glob 与 -w 过滤）。
        fts_ok = not exts and not args.exclude and not args.symbol
        if not args.exact and len(patterns) > 1:
            # 中文扩展遵循「先窄后宽」：先精确匹配，命中不足才放宽到扩展词
            _dn = not args.include_noise
            results, err = rg_search([args.query], path, excludes, exts,
                                     args.context, args.count, max_results,
                                     is_literal(args.query), args.query, args.dot,
                                     drop_noise=_dn, timeout=time_budget,
                                     since_ts=since_ts, until_ts=until_ts,
                                     fts_ok=fts_ok, symbol=args.symbol)
            if not results and not err:
                results, err = rg_search(patterns, path, excludes, exts,
                                         args.context, args.count, max_results,
                                         fixed, args.query, args.dot, drop_noise=_dn,
                                         timeout=time_budget,
                                         since_ts=since_ts, until_ts=until_ts,
                                         fts_ok=fts_ok, symbol=args.symbol)
                if results:
                    mode += "+扩展"
        else:
            results, err = rg_search(patterns, path, excludes, exts,
                                     args.context, args.count, max_results,
                                     fixed, args.query, args.dot,
                                     drop_noise=not args.include_noise,
                                     timeout=time_budget,
                                     since_ts=since_ts, until_ts=until_ts,
                                     fts_ok=fts_ok, symbol=args.symbol)
        # 中英对齐回落（2026-10-31）：alias 只进打分层时纯英文语料连候选池
        # 都进不了（rg 字面零命中，打分无从发力）；无条件注入检索层又会用
        # 对齐噪音挤掉既有命中（eval_rank 0.8258→0.7857 实测回退）。折中取
        # 「先窄后宽」同款语义：原 patterns 空手时才带对齐词元重搜一次。
        if not results and not err:
            alias_pats = list(patterns) + [
                en for p in patterns for en in _ZH_EN_ALIASES.get(p, ())
                if en not in patterns]
            if alias_pats != patterns:
                results, err = rg_search(alias_pats, path, excludes, exts,
                                         args.context, args.count, max_results,
                                         fixed, args.query, args.dot,
                                         drop_noise=not args.include_noise,
                                         timeout=time_budget,
                                         since_ts=since_ts, until_ts=until_ts,
                                         fts_ok=fts_ok, symbol=args.symbol)
                if results:
                    mode += "+中英"

    # 注：噪声档**只降权不排除**（见 _apply_noise_floor），所以这里没有
    # 「搜不到就回落重搜」的分支——噪声内容始终在结果池里，只是排在真源之后。
    # 早先版本用 rg 的 -g glob 排除，那会连搜索根自己叫 tests/ 的情况一起打死
    # （实测整棵树被判空、且回落也不触发），故改成 Python 层排序。

    # 融合 Office 办公文档解包透视（DOCX/PPTX/XLSX，纯标准库免依赖）
    if not args.filename and (args.scope in ("doc", "office") or (exts and any(e in OFFICE_ZIP_EXTS for e in exts))):
        office_res = office_search(patterns, path, excludes, exts, args.count,
                                   max_results, fixed, args.query, args.dot,
                                   drop_noise=not args.include_noise,
                                   since_ts=since_ts, until_ts=until_ts)
        if office_res:
            results = _merge_content_dedup(results or [], office_res, max_results)
            if "office" not in engine:
                engine = f"{engine}+office"

    elapsed = int((time.time() - start) * 1000)
    # 时间窗：统一出口按文件 mtime 过滤（rg/fd/mdfind 共用）
    results = filter_by_mtime(results, args.since, args.until)
    # 按文件定位场景（fd/spotlight）：fzf 相关性评分 + 拼音加分 + mtime 新优先排序
    if (args.filename or args.spotlight) and results:
        results = _rank_path_results(results, args.query)

    # frecency 记录（opt-in，ARGO_SEEK_FRECENCY=1）：只记最终交付的 top 路径，
    # 关闭时零 IO、零行为差异（见 seek_frecency.py）
    if results:
        try:
            import seek_frecency
            if seek_frecency.enabled():
                seek_frecency.record([r[0] if isinstance(r, (list, tuple)) else str(r)
                                      for r in results])
        except Exception:
            pass

    if err:
        return f"local-seek: {err}", 1
    if not results:
        return f"local-seek: 未找到匹配（{engine} · {path} · {elapsed}ms）", 1

    if args.json:
        return to_json(results, engine, mode, scope, path, elapsed, args.query,
                       since=args.since, until=args.until), 0
    return format_output(results, engine, mode, path, elapsed, args.query,
                         len(results)), 0


def main(argv=None):
    """CLI 薄壳：print + exit code（进程内调用方直接用 run_query）。"""
    text, rc = run_query(argv)
    if text:
        print(text)
    return rc


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            if _s.encoding and _s.encoding.lower().replace("-", "") != "utf8":
                _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
