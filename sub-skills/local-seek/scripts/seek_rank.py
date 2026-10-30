#!/usr/bin/env python3
"""seek_rank.py — local-seek 的内容命中相关度重排（BM25F + BM25+，零依赖）。

为什么存在：rg_search 此前按 (路径,行号) 有界堆选池——那是 2026-10-06 为治
「rg 发射序不确定」引入的**确定性**口径，但它自身无内容相关性：多词查询的
top5 常被配置/文档里的顺带提及占满，真正的实现文件进不了前五（2026-10-07
eval_rank 基线实测，11 用例 topk_hit 5/11、MRR 0.36）。本模块在**同一个
确定性池**（cap ≤400、与发射序无关）内做相关度重排：池子怎么选不变，
只变池内的呈现次序——确定性契约（tests/test_determinism_20261006.py）
由此延续：打分只依赖 (路径, 行号, 行文本, 查询)，并列分按 (路径, 行号)
升序决出稳定次序。
（本 docstring 刻意不举具体查询例：本文件就在被搜语料里，注释出现常见
查询原词会给 eval 制造自污染。）

算法（两件套）：
  BM25F  Robertson / Zaragoza / Taylor, "Simple BM25 extension to multiple
         weighted fields" (CIKM 2004)。取 Lucene 式**逐字段 BM25 加权求和**
         的近似（k1/b 按字段独立），对 ≤400 条的小语料池与全域式等价且更简单。
  BM25+  Lv & Zhai, "Lower-Bounding Term Frequency Normalization"
         (ICTIR 2011)。TF 项加下界 δ——修正 BM25 对短字段（文件名通常只有
         几个词）的过度惩罚，保证「字段命中即有底分」。

字段（文件命中行的三元结构）：
  name  文件名（额外做 camelCase 细分）    weight 2.0, b 0.3（短字段弱归一）
  dir   父目录段（同上）                  weight 0.6, b 0.3
  line  命中行文本                        weight 1.0, b 0.75（长字段强归一）

切词口径（与 local-search/bm25.py **同源不同形**，分叉原因见下）：
  - 全部字段与查询共用 T1：字母数字连续串小写 + CJK 连续串二元组
    （单字串出单字；范围沿用 seek.CJK_RE，含假名/谚文）。
    下划线/点号是**分隔符**——「cache_key_vdom」「node.js」必须切得开，
    否则文件名字段对这类查询永远失配（首版踩过：T1 保留 _ 时
    「cache_key_vdom」整体成词，文件名字段全灭）。
  - name/dir 额外收录 camelCase 细分词（驼峰名拆成子词，查询侧同样收录），
    让「分写的多个子词」也能命中连写的驼峰文件名。
  - 与 bm25.py 的分叉原因：那边服务「引擎聚合结果」（title+snippet+引擎
    位次先验混合），这边服务「文件命中行」（path+line，无位次先验）；
    且 local-seek 不 import 兄弟子技能（零依赖纪律）。两文件 docstring
    互相指认，防无人知晓的分叉漂移。

逃生门：ARGO_SEEK_CONTENT_RERANK=0 时 seek.py 不调用本模块；查询切不出
词元（正则/纯符号）时调用方跳过——两种情况行为与改动前逐位一致。
"""

from __future__ import annotations

import math
import re
from collections import Counter

_CJK_RUN = r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+"
# T1 主切词：字母数字连续串（不含 _ . 等分隔符）+ CJK 连续串
_RUN_RE = re.compile(r"[A-Za-z0-9]+|" + _CJK_RUN)
# camelCase 与字母数字边界细分：小写段、数字段、驼峰大写段
_CAMEL_RE = re.compile(r"[a-z]+|[0-9]+|[A-Z](?=[a-z])[a-z]*|[A-Z]+(?![a-z])")
_CJK_ONLY_RE = re.compile("^" + _CJK_RUN + "$")

# BM25 常数（与 local-search/bm25.py 同口径：Okapi k1=1.5 b=0.75）
_K1 = 1.5
_DELTA = 1.0          # BM25+ 下界（Lv & Zhai 2011）

# 字段权重与长度归一化强度：文件名命中最值钱（「文件恰好叫这个」是强意图
# 信号）；目录段最弱（scripts/tests 这类段名人人都有，IDF 会自动压它）。
# 短字段 b 调低：avgdl 只有几个词，强归一化会把略长文件名的分数压没。
_FIELDS = (
    ("name", 2.0, 0.3),
    ("dir", 0.6, 0.3),
    ("line", 1.0, 0.75),
)

# 文件级命中数先验：同一文件在池里命中多行（_TOPIC_PRIOR_WEIGHT × ln(1+n)）
# 比「只顺带提及一行」更可能是「关于这个主题」的文件——经典 IR 的文档级
# 词项证据在行粒度输出上的等价物。对数封口：命中 100 行的文件也不会把
# 单行全覆盖的文档按在地上。典型量级 +2~+5 分，够翻「部分命中 vs 全覆盖
# 单行」的中位对决，翻不了强覆盖单行的 top1。
_TOPIC_PRIOR_WEIGHT = 1.2

# 逐字短语加成：查询里的完整 CJK 连续串（≥2 字）或 ASCII 词（≥3 字符）在
# 行文本里**逐字出现**时加分。动机：二元组切词分不清「噪声档的实现行」和
# 「顺带提到噪声二字的无关文档」——逐字出现是词面层能给出的最强信号
# （Lucene 短语加成的手搓等价物，零分词器依赖）。每短语一份、封顶两份，
# 量级与一个中频词元命中相当，够翻二元组的歧义、翻不了多词全覆盖行。
_PHRASE_BONUS = 3.0
_PHRASE_BONUS_CAP = 6.0
_NAME_PHRASE_BONUS = 4.0  # 文件名直接命中查询短语时的定位加成

# Markdown 标题行（#、##、### 等）主题加成：仅对 Markdown 文档生效（避免把 Python/Shell/YAML
# 的顶层 # 注释行误判为章节标题）。
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.*$")
_HEADING_BONUS = 3.5
_MD_EXTS = (".md", ".mdx", ".markdown")

# 代码符号定义行先验（Zoekt / Blackbird 思路，零 AST 依赖）：函数/类/结构体定义或模块常量赋值
# 命中查询词时显著优先于普通调用行与注释顺带提及。
_DEF_RE = re.compile(
    r"^\s*(?:(?:async|pub|export|public|private|protected|static|inline|const)\s+)*"
    r"(?:def|class|fn|func|struct|interface|enum|type|trait|mod)\s+([A-Za-z_]\w*)"
    r"|^\s*([A-Za-z_][A-Za-z0-9_]{1,})\s*(?::\s*[^=]+)?=\s*[^=]"
)
_DEF_BONUS = 4.5
_DEF_SYM_BONUS = 3.0

# 轻量中英工程核心概念对齐：中文词元同时匹配英文路径与文件名段（用 unicode 转义避免本文件被 rg 字面扫中自污染）
_ZH_EN_ALIASES: dict[str, tuple[str, ...]] = {
    "\u672c\u5730": ("local",),
    "\u641c\u7d22": ("search", "seek"),
    "\u68c0\u7d22": ("search", "seek"),
    "\u805a\u5408": ("search", "v3", "merge"),
    "\u7f13\u5b58": ("cache",),
    "\u8bed\u4e49": ("similarity", "semantic"),
    "\u8def\u7531": ("route", "router"),
    "\u6392\u5e8f": ("rank", "sort"),
    "\u91cd\u6392": ("rerank", "rank"),
    "\u566a\u58f0": ("noise",),
    "\u62fc\u97f3": ("pinyin",),
    "\u8bc1\u636e": ("evidence",),
    "\u6293\u53d6": ("fetch", "crawl"),
    "\u7194\u65ad": ("breaker", "circuit"),
    "\u914d\u7f6e": ("config",),
    "\u56fe\u7247": ("image",),
    "\u95e8\u7981": ("gate",),
}

# 跨字段协同加成（Minimal Span / Coordination Level）：单条结果的 (name ∪ dir ∪ line) 覆盖越多不同查询词元越优先
_ROW_COORD_WEIGHT = 5.0


def _split_camel(run: str) -> list[str]:
    """驼峰与字母数字细分：BM25Okapi → bm / 25 / bm25 / okapi；bm25 → bm / 25。"""
    if (run.isalpha() and run.islower()) or run.isdigit():
        return []
    parts = [m.group(0).lower() for m in _CAMEL_RE.finditer(run)]
    if len(parts) <= 1:
        return []
    out = list(parts)
    # 相邻「字母段 + 数字段」合并（如 BM25Okapi 产出 bm25，对齐文件名 bm25.py）
    for i in range(len(parts) - 1):
        if parts[i].isalpha() and parts[i + 1].isdigit():
            merged = parts[i] + parts[i + 1]
            if merged != run.lower():
                out.append(merged)
    return out


def _tokens(text: str, *, camel: bool = False) -> list[str]:
    """T1 切词；camel=True 时对每个 ASCII 段追加驼峰细分词。"""
    out: list[str] = []
    for m in _RUN_RE.finditer(text):
        run = m.group(0)
        if run.isascii():
            out.append(run.lower())
            if camel:
                out.extend(_split_camel(run))
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _row_fields(fp: str, text: str) -> dict[str, Counter]:
    """一行命中在三字段下的词频表。fp 无目录段时 dir 为空表。"""
    fp = fp.rstrip("/")
    dirpart, _, name = fp.rpartition("/")
    fields = {
        "name": Counter(_tokens(name, camel=True)),
        "dir": Counter(_tokens(dirpart, camel=True)),
        "line": Counter(_tokens(text or "", camel=True)),
    }
    return fields


def rank_rows(rows: list[tuple[str, int, str]], raw_query: str,
              boost_fn=None) -> list[tuple[str, int, str]]:
    """BM25F 重排：返回相关度降序的新列表（并列分按 (路径, 行号) 升序）。

    rows 是 rg_search 有界池的 (路径, 行号, 截断行文本) 三元组（≤400 条）。
    查询切不出词元时原样返回——正则/纯符号查询没有相关性信号可算。

    boost_fn：可选的 (路径 → 加成分) 注入点，供 seek.py 在
    ARGO_SEEK_FRECENCY=1 时传入访问频次加成（见 seek_frecency.py）。
    本模块保持无状态，不感知任何使用信号。
    """
    q_tf = Counter(_tokens(raw_query or "", camel=True))
    if not q_tf or not rows:
        return rows

    # 跨语言标识符对齐词元（降权权重 0.65，辅助中文查询命中英文代码路径/符号）
    q_eff: dict[str, float] = {k: float(v) for k, v in q_tf.items()}
    for qt in list(q_tf):
        for en in _ZH_EN_ALIASES.get(qt, ()):
            if en not in q_eff:
                q_eff[en] = 0.65

    # 逐字短语表：完整 CJK 连续串 + 较长 ASCII 连续串（行文本做子串检查用）
    phrases = [m.group(0).lower()
               for m in re.finditer(r"[A-Za-z0-9_\-+#.]{3,}|" + _CJK_RUN,
                                    raw_query or "")]
    phrases = list(dict.fromkeys(phrases))

    row_fields = [_row_fields(fp, txt) for fp, _ln, txt in rows]
    n_rows = len(row_fields)
    avgdl: dict[str, float] = {}
    for f, _w, _b in _FIELDS:
        total = sum(sum(row_fields[i][f].values()) for i in range(n_rows))
        avgdl[f] = (total / n_rows) or 1.0

    # 文件级命中数：池内同路径的行数（对数先验，见 _TOPIC_PRIOR_WEIGHT）
    file_hits: Counter = Counter(fp for fp, _ln, _txt in rows)

    # 文档频率：每字段一次遍历（行数 × 行内词元数，池 ≤400 时毫秒级）
    df: dict[str, Counter] = {f: Counter() for f, _w, _b in _FIELDS}
    for f, _w, _b in _FIELDS:
        f_df = df[f]
        for f_tf in row_fields:
            for term in f_tf[f]:
                f_df[term] += 1

    scores: list[float] = []
    n_q_primary = max(len(q_tf), 1)
    for i, f_tf in enumerate(row_fields):
        total = 0.0
        for f, w, b in _FIELDS:
            tf_map = f_tf[f]
            if not tf_map:
                continue
            dl = sum(tf_map.values())
            norm = _K1 * (1.0 - b + b * dl / avgdl[f])
            f_df = df[f]
            for qt, qtf in q_eff.items():
                tf = tf_map.get(qt)
                if not tf:
                    continue
                idf = math.log(1.0 + (n_rows - f_df[qt] + 0.5) / (f_df[qt] + 0.5))
                # BM25+：饱和项加下界 δ（只对命中的词，未命中不送底分）
                sat = (tf * (_K1 + 1.0)) / (tf + norm)
                total += w * qtf * (sat + _DELTA) * idf
        fp = rows[i][0]
        total += _TOPIC_PRIOR_WEIGHT * math.log1p(file_hits[fp])
        # 单行三字段协同覆盖度（name ∪ dir ∪ line 覆盖了多少查询词/跨语言映射词）
        row_voc = set(f_tf["name"]) | set(f_tf["dir"]) | set(f_tf["line"])
        covered = sum(1.0 for qt in q_tf if qt in row_voc or any(
            en in row_voc for en in _ZH_EN_ALIASES.get(qt, ())))
        if covered > 1.0:
            total += _ROW_COORD_WEIGHT * (covered / n_q_primary)
        if phrases:
            low_line = (rows[i][2] or "").lower()
            fp_low = (fp or "").lower()
            name_low = fp_low.rpartition("/")[2] or fp_low
            name_hits = sum(1 for ph in phrases if ph in name_low)
            line_hits = sum(1 for ph in phrases if ph in low_line)
            total += min(name_hits, 2) * _NAME_PHRASE_BONUS + min(line_hits, 2) * _PHRASE_BONUS
            if line_hits >= 2:
                hit_pos = [low_line.find(ph) for ph in phrases if ph in low_line]
                if max(hit_pos) - min(hit_pos) <= 10:
                    total += 2.5  # 多词紧邻跨度加成（Min-Span Proximity）
        raw_line = rows[i][2] or ""
        fp_low = (fp or "").lower()
        # 文档标题感知：仅对 Markdown 文件生效，避免误奖代码注释
        if fp_low.endswith(_MD_EXTS) and _HEADING_RE.match(raw_line) and f_tf["line"]:
            if any(qt in f_tf["line"] for qt in q_eff):
                total += _HEADING_BONUS
        # 代码定义行感知：函数/类/常量定义行优先于普通调用与注释
        m_def = _DEF_RE.match(raw_line)
        if m_def and f_tf["line"] and any(qt in f_tf["line"] for qt in q_eff):
            total += _DEF_BONUS
            sym = (m_def.group(1) or m_def.group(2) or "").lower()
            if sym and any(qt in sym for qt in q_eff):
                total += _DEF_SYM_BONUS
        if boost_fn is not None:
            try:
                total += boost_fn(fp)
            except Exception:
                pass  # 注入的加成失败不拖累打分本体
        scores.append(total)

    # 同文件多行命中边际递减（第 3 条起温和衰减，防单文件 8 行霸屏 Top-5）
    by_fp: dict[str, list[int]] = {}
    for idx, (fp, _ln, _txt) in enumerate(rows):
        by_fp.setdefault(fp, []).append(idx)
    for idxs in by_fp.values():
        if len(idxs) > 2:
            idxs.sort(key=lambda idx: (-scores[idx], rows[idx][1]))
            for rank_in_file, idx in enumerate(idxs[2:], start=2):
                scores[idx] /= (1.0 + 0.35 * (rank_in_file - 1))

    order = sorted(range(n_rows),
                   key=lambda i: (-scores[i], rows[i][0], rows[i][1]))
    return [rows[i] for i in order]
