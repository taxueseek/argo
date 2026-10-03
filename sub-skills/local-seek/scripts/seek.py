#!/usr/bin/env python3
"""local-seek: 本地高效搜索统一入口。零第三方依赖，仅用 Python 标准库。

路由规则（Argo 式确定性路由，模型不必自己拼参数）：
  rg       正文/正则搜索（默认，最快，尊重 .gitignore）
  fd       按文件名查找（--filename）
  mdfind   macOS Spotlight 全盘保底（--spotlight 或 --scope all）

输出原则：默认精简文本（路径:行号:截断片段），--json 供 Agent 消费。
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
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
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
    "__tests__", "__test__", "testdata", "test_data", "fixtures", "fixture",
    "__mocks__", "__snapshots__", "e2e", "benchmark", "benchmarks",
    "tmp", "temp", ".tmp", "tmpdir", "archive", "archives", ".trash",
    "2026-*",   # 本工作区的日期归档目录：研究产物，非源码
]
_NOISE_SET = frozenset(n for n in NOISE_TIER if not n.endswith("*"))
DOC_EXTS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "md", "txt",
            "rtf", "html", "htm", "epub", "csv", "json", "yaml", "yml", "log"}
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


def _is_noise_path(fp: str, root: str) -> bool:
    """该命中是否落在噪声档目录（相对**搜索根**判定）。

    为什么必须相对搜索根：直接对整条路径做分段匹配是错的——搜索根自己叫
    `tests/` 或含 `2026-` 时（本工作区正是如此），整棵树都会被判成噪声。
    rg 的 -g glob 犯的正是这个错（`**/tests/**` 会匹配路径里任意一段），
    所以降权放到 Python 层做，不交给 glob。
    """
    try:
        rel = Path(fp).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        rel = Path(fp)          # 不在根之下（软链/越界）时退回整条判定
    parts = rel.parts
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
    """
    clean = [r for r in rows if not _is_noise_path(r[0], root)]
    noisy = [r for r in rows if _is_noise_path(r[0], root)]
    return (clean + noisy)[:max_results]


def rg_search(patterns, path, excludes, exts, context, count, max_results,
              fixed, raw_query="", dots=False, drop_noise=True, timeout=None,
              since_ts=None, until_ts=None):
    timeout = _RUN_DEFAULT_TIMEOUT_S if timeout is None else timeout

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
        # regex parse error 已回退，其他错误需要报告（权限不足、路径不存在等）
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
        counts.sort(key=lambda t: t[1], reverse=True)
        return _apply_noise_floor(counts, path, max_results) if drop_noise \
            else counts[:max_results], None
    out = []
    # 提前截断：rg 的输出是「文件内按行序」，直接 break 会砍掉后面文件里的
    # 命中，而这些文件可能才是真源（噪声档过滤要看到全量才能排序）。故先
    # 收满一个**上界**再交给 _apply_noise_floor 排序截断。上界取 max_results
    # 的 4 倍并设下限，保证「真源排在前面」这个目标有素材可用。
    cap = max(max_results, min(max_results * 4, 400))
    for line in proc.stdout.splitlines():
        m = re.match(r"^(.*?):(\d+):(.*)$", line)
        if m:
            fp, ln, txt = m.group(1), int(m.group(2)), m.group(3)
            # 时间窗在入池前过滤：窗外行不占上界名额（先截断后过滤会漏报）
            if _in_time_window(fp, since_ts, until_ts):
                out.append((fp, ln, truncate(txt)))
        if len(out) >= cap:
            break
    if drop_noise:
        out = _apply_noise_floor(out, path, max_results)
    else:
        out = out[:max_results]
    return out, None


# ── 文件名相关性评分 + 拼音首字母（fzf 式；pypinyin 优先，GB2312 表保底）──────────

# GB2312 首字母区间表（常用汉字全覆盖；生僻字回退原字）
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


def pinyin_initials(text: str) -> str:
    """文本的拼音首字母（中文→首字母，非中文保留原字符），用于「xjj→新建夹」。
    pypinyin 可用则用；否则 GB2312 区间表保底；两者都不可用时返回原文本。"""
    if not text:
        return text
    try:
        from pypinyin import lazy_pinyin, Style  # type: ignore
        parts = lazy_pinyin(text, style=Style.FIRST_LETTER, errors="default")
        return "".join(parts)
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


def _fzf_score(name: str, pattern: str) -> int:
    """fzf 式文件名匹配评分：smart case + 连续匹配/段边界加分。0 表示不匹配。
    借鉴 fzf 的常见场景优化：连续命中、路径段/词边界、全等大加分。"""
    if not pattern or not name:
        return 0
    p, n = pattern, name
    if not any(c.isupper() for c in p):  # smart case：无大写则忽略大小写
        p, n = p.lower(), n.lower()
    score, prev, i = 0, -2, 0
    for ch in p:
        idx = n.find(ch, i)
        if idx < 0:
            return 0
        if prev >= 0 and idx == prev + 1:
            score += 3        # 连续命中
        elif idx == 0 or n[idx - 1] in "/._- " or not n[idx - 1].isalnum():
            score += 4        # 路径段/词边界命中
        else:
            score += 1
        prev, i = idx, idx + 1
    if len(p) == len(n):
        score += 5            # 全等
    return score


def _looks_like_pinyin_abbrev(q: str) -> bool:
    """疑似中文拼音缩写（如 xjj）：纯 ASCII 字母、2-4 位。"""
    q = (q or "").strip()
    return bool(q and q.isascii() and q.isalpha() and 2 <= len(q) <= 4)


def _file_pinyin_bonus(path: str, query: str) -> int:
    """文件名拼音首字母与查询呼应 → 加分（「新建夹」↔ xjj）。
    正向（中文查询 → 拼音缩写）50；反向（拼音缩写查询 → 中文文件名）8，
    反向低于字面命中，避免把真名 xjj.docx 挤下去。"""
    q = query.strip().lower()
    if not q:
        return 0
    base = Path(path).name
    initials = pinyin_initials(base).lower()
    if not initials:
        return 0
    has_cjk_query = any("\u4e00" <= c <= "\u9fff" for c in query)
    if has_cjk_query:
        qs = pinyin_initials(query).strip().lower()
        return 50 if qs and qs in initials else 0
    if (q.isascii() and q.isalpha() and 2 <= len(q) <= 4
            and any("\u4e00" <= c <= "\u9fff" for c in base)):
        # 拼音缩写查询（xjj）→ 中文文件名（新建夹.pdf）
        return 8 if q in initials else 0
    return 0


def _rank_path_results(results, query: str):
    """按文件名相关性排序：fzf 评分 + 拼音加分 + mtime 新优先。
    仅用于「按文件定位」场景（--filename / --spotlight）；内容搜索保持原序。"""
    def key(it):
        fp = it[0] if isinstance(it, (list, tuple)) else str(it)
        sc = _fzf_score(Path(fp).name, query)
        sc += _file_pinyin_bonus(fp, query)
        sc += max(0, _fzf_score(fp, query.lower())) // 4  # 全路径段匹配也加分
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
    if proc is None or proc.returncode not in (0, 1):
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
    out = []
    for line in proc.stdout.splitlines():
        if count:
            if ":" in line:
                fp, _, n = line.rpartition(":")
                # grep -c 会把没有命中的文件也列出来，写成 file:0。这种不算命中，
                # 丢掉；这样和 rg --count-matches 只列命中的文件保持一致。
                if n.isdigit() and int(n) > 0 and _in_time_window(fp, since_ts, until_ts):
                    out.append((fp, int(n), ""))
            if len(out) >= max_results:
                break
        else:
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if m:
                fp, ln, txt = m.group(1), int(m.group(2)), m.group(3)
                if _in_time_window(fp, since_ts, until_ts):
                    out.append((fp, ln, truncate(txt)))
            if len(out) >= max_results:
                break
    return out, None


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


def to_json(results, engine, mode, scope, path, elapsed_ms, query, since=None, until=None):
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
            {"path": fp, "line": ln, "snippet": txt, "mtime": _file_mtime(fp)}
            for fp, ln, txt in results
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
    """rg 缺失时的 grep 保底（目录内/全盘共用）。

    返回 (results, err, mode)；rg 与 grep 都不可用时 results=None 且 err
    说明缺什么——调用方必须把「工具缺失」与「未找到匹配」区分开。
    """
    grep_exe = resolve_grep()
    if not grep_exe:
        return None, "本机无 rg 与 grep 可用，请安装 ripgrep", ""
    dots = getattr(args, "dot", False)
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
    ap.add_argument("--scope", choices=["code", "doc", "all"], default="code",
                    help="code=正文+代码；doc=文档类；all=Spotlight 兜底")
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
    # doc 场景补充文档扩展名
    if scope == "doc" and not exts:
        # 文本档（md/txt/html…）rg 直接搜全文；pdf/docx 等二进制 rg 搜不出
        # 内容，那是 --spotlight（Spotlight 内容索引）的职责。此前把 md/txt
        # 从 exts 里减掉，恰好漏掉最常搜的笔记（2026-10-03 实测：--scope doc
        # 搜 md 零命中，doc 场景等于虚设）。
        exts = sorted(DOC_EXTS)
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
    elif not tool_exists("rg"):
        # 目录内搜索缺 rg 必须落 grep，不能落 mdfind：Spotlight 不索引
        # 源码内容，会把「工具缺失」伪装成「搜索结论」（实测搜 argo 自身
        # 符号返回 24 条外部陈旧副本、漏掉正主，还报「未找到匹配」）。
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
            err = "fd 未安装：按文件名搜索需要 fd（内容搜索走 rg/grep）"
    else:
        mode = "deep" if (args.context > 0 or args.count) else "fast"
        if not args.exact and len(patterns) > 1:
            # 中文扩展遵循「先窄后宽」：先精确匹配，命中不足才放宽到扩展词
            _dn = not args.include_noise
            results, err = rg_search([args.query], path, excludes, exts,
                                     args.context, args.count, max_results,
                                     is_literal(args.query), args.query, args.dot,
                                     drop_noise=_dn, timeout=time_budget,
                                     since_ts=since_ts, until_ts=until_ts)
            if not results and not err:
                results, err = rg_search(patterns, path, excludes, exts,
                                         args.context, args.count, max_results,
                                         fixed, args.query, args.dot, drop_noise=_dn,
                                         timeout=time_budget,
                                         since_ts=since_ts, until_ts=until_ts)
                if results:
                    mode += "+扩展"
        else:
            results, err = rg_search(patterns, path, excludes, exts,
                                     args.context, args.count, max_results,
                                     fixed, args.query, args.dot,
                                     drop_noise=not args.include_noise,
                                     timeout=time_budget,
                                     since_ts=since_ts, until_ts=until_ts)

    # 注：噪声档**只降权不排除**（见 _apply_noise_floor），所以这里没有
    # 「搜不到就回落重搜」的分支——噪声内容始终在结果池里，只是排在真源之后。
    # 早先版本用 rg 的 -g glob 排除，那会连搜索根自己叫 tests/ 的情况一起打死
    # （实测整棵树被判空、且回落也不触发），故改成 Python 层排序。

    elapsed = int((time.time() - start) * 1000)
    # 时间窗：统一出口按文件 mtime 过滤（rg/fd/mdfind 共用）
    results = filter_by_mtime(results, args.since, args.until)
    # 按文件定位场景（fd/spotlight）：fzf 相关性评分 + 拼音加分 + mtime 新优先排序
    if (args.filename or args.spotlight) and results:
        results = _rank_path_results(results, args.query)

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
