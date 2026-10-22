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
# camelCase 细分：小写/数字段、大写段（后随小写时取「大写+小写尾巴」）
_CAMEL_RE = re.compile(r"[a-z0-9]+|[A-Z](?=[a-z])[a-z]*|[A-Z]+(?![a-z])")
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


def _split_camel(run: str) -> list[str]:
    """驼峰细分：BM25Okapi → bm / 25 / okapi（小写输出；无驼峰则空）。"""
    if run.islower() or run.isdigit():
        return []
    return [m.group(0).lower() for m in _CAMEL_RE.finditer(run)]


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
    name, _, dirpart = fp.rpartition("/")
    fields = {
        "name": Counter(_tokens(name, camel=True)),
        "dir": Counter(_tokens(dirpart, camel=True)),
        "line": Counter(_tokens(text or "")),
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
    for i, f_tf in enumerate(row_fields):
        total = 0.0
        for f, w, b in _FIELDS:
            tf_map = f_tf[f]
            if not tf_map:
                continue
            dl = sum(tf_map.values())
            norm = _K1 * (1.0 - b + b * dl / avgdl[f])
            f_df = df[f]
            for qt, qtf in q_tf.items():
                tf = tf_map.get(qt)
                if not tf:
                    continue
                idf = math.log(1.0 + (n_rows - f_df[qt] + 0.5) / (f_df[qt] + 0.5))
                # BM25+：饱和项加下界 δ（只对命中的词，未命中不送底分）
                sat = (tf * (_K1 + 1.0)) / (tf + norm)
                total += w * qtf * (sat + _DELTA) * idf
        total += _TOPIC_PRIOR_WEIGHT * math.log1p(file_hits[rows[i][0]])
        if phrases:
            low_line = (rows[i][2] or "").lower()
            fp_low = (rows[i][0] or "").lower()
            name_low = fp_low.rpartition("/")[2] or fp_low
            name_hits = sum(1 for ph in phrases if ph in name_low)
            line_hits = sum(1 for ph in phrases if ph in low_line)
            total += min(name_hits, 2) * _NAME_PHRASE_BONUS + min(line_hits, 2) * _PHRASE_BONUS
        if boost_fn is not None:
            try:
                total += boost_fn(rows[i][0])
            except Exception:
                pass  # 注入的加成失败不拖累打分本体
        scores.append(total)

    order = sorted(range(n_rows),
                   key=lambda i: (-scores[i], rows[i][0], rows[i][1]))
    return [rows[i] for i in order]
