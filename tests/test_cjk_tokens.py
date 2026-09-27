#!/usr/bin/env python3
"""test_cjk_tokens.py — 无空格中文查询的判据回归。

## 为什么单独立一道门

`re.findall(r"...|[\\u4e00-\\u9fff]{2,}", q)` 会把**无空格中文整句**吃成一个
token（「年最好看的科幻电影」→ `['年最好看的科幻电影']`）。结果里永远不会出现
整句原样，`token in blob` 于是恒假——真实结果一条都过不了门。实测每查询取通用
引擎 5 条实回：

    查询                 实回  旧判据通过  2-gram
    年最好看的科幻电影      5        0        5
    量子计算最新进展        5        0        5
    怎么学好深度学习        5        0        5
    冬奥会金牌榜            5        1        5
    东京旅游攻略推荐        5        0        5
    新能源汽车电池技术      5        2        5

四处用同一个正则的地方一起静默失效：recovery 兜底、百度百科相关度门、化学
token 交集、V2EX 中文节点反查。所以这里有纯函数断言，也有四个站点各自的
成对用例（真结果要过 + 无关结果要拦）——只测「放宽后能过」会把门测成恒真。
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cjk_tokens import cjk_ngrams, cjk_term_grams, signal_tokens  # noqa: E402

#: 实测过「旧判据通过 0~2/5」的无空格中文查询。
SPACELESS_CJK = [
    "年最好看的科幻电影",
    "量子计算最新进展",
    "怎么学好深度学习",
    "冬奥会金牌榜",
    "东京旅游攻略推荐",
    "新能源汽车电池技术",
]


def test_spaceless_cjk_never_becomes_one_token():
    """整句成 token 是这个缺陷的根，先用纯函数钉死。"""
    for q in SPACELESS_CJK:
        toks = signal_tokens(q)
        assert toks, f"{q} 一个信号词都没切出来"
        assert q not in toks, f"{q} 又被整句当成一个 token"
        assert all(len(t) == 2 for t in toks), f"{q} → {toks}，中文应全是 2-gram"


def test_cjk_terms_case_stays_lowercase():
    """`in blob` 比对要求两边同形，大写必须降下来。"""
    assert signal_tokens("NASA 2025 火星计划") == [
        "nasa", "火星", "星计", "计划",
    ]


def test_single_cjk_char_is_not_a_token():
    """单字不成词（与原 `{2,}` 一致）——「上」「大」这类会噪声命中。"""
    assert signal_tokens("上海") == ["上海"]
    assert signal_tokens("上") == []


def test_ngram_and_term_helpers():
    assert cjk_ngrams("科幻电影") == ["科幻", "幻电", "电影"]
    assert cjk_ngrams("科幻") == ["科幻"]           # 不长于 n 时原样返回
    assert cjk_term_grams("abc 科幻电影 xyz") == ["科幻", "幻电", "电影"]
    assert cjk_term_grams("only ascii") == []


def test_stopwords_filter_bigrams_not_just_whole_tokens():
    """停用词要作用在 2-gram 上，否则「最新」会把门从恒假翻到恒真。"""
    from recovery import _REC_SIGNAL_STOP

    toks = signal_tokens("量子计算最新进展", stop=_REC_SIGNAL_STOP)
    assert "最新" not in toks
    assert "量子" in toks and "计算" in toks and "进展" in toks


def test_uppercase_acronym_bypasses_stopwords():
    """大写缩写不受停用词限制——丢了这条，门会从「恒假」翻到「恒真」。

    「who」本身就在 _REC_SIGNAL_STOP 里。若按停用词一律滤掉，
    「WHO headquarters」两个词会被滤光 → keys 空 → `if not keys: return True`
    把门放成恒真，Chegg 垃圾页也能算「恢复成功」（test_p0_v25 的反例锁的
    正是这一点，本条是它的机理说明）。
    """
    from recovery import _REC_SIGNAL_STOP, _result_has_query_signal

    assert "who" in _REC_SIGNAL_STOP
    assert signal_tokens("WHO headquarters", stop=_REC_SIGNAL_STOP) == ["who"]
    junk = {"title": "Chegg homework", "snippet": "study support",
            "url": "https://chegg.example"}
    assert _result_has_query_signal("WHO headquarters", junk) is False


# ── 站点 1：recovery 空结果恢复的「结果与查询有信号」门 ────────────────────────


def test_recovery_gate_accepts_real_hit():
    from recovery import _result_has_query_signal

    item = {"title": "近10年最好看的十部科幻电影", "snippet": "科幻片单",
            "url": "https://example.com/1"}
    assert _result_has_query_signal("年最好看的科幻电影", item) is True


def test_recovery_gate_rejects_unrelated_result():
    """成对的反面：无关结果必须仍被拦住，否则这道门等于删掉。"""
    from recovery import _result_has_query_signal

    junk = {"title": "Chegg 学习资料下载", "snippet": "免费下载各科习题答案",
            "url": "https://chegg.example/x"}
    assert _result_has_query_signal("年最好看的科幻电影", junk) is False


def test_recovery_gate_rejects_generic_bigram_coincidence():
    """「最新」这类通用 bigram 的巧合命中要被停用词挡掉。"""
    from recovery import _result_has_query_signal

    cat = {"title": "最新的养猫指南", "snippet": "新手养猫全攻略",
           "url": "https://example.com/cat"}
    assert _result_has_query_signal("量子计算最新进展", cat) is False


# ── 站点 2：百度百科词条相关度门 ─────────────────────────────────────────────


def test_baike_gate_spaceless_cn_query_passes():
    """「苹果公司简介」曾返回 0 条，加个空格才 1 条——同一条词条因空格而存亡。"""
    from config import load_config, get_engines
    from engines_builders_data import _build_baidu_baike_engine

    specs = get_engines(load_config(), routable_only=False)
    spec = specs.get("baidu_baike") or {"name": "baidu_baike"}
    # 只验判据不验网络：直接跑 builder 产出的引擎会打网，这里取门用的表达式。
    assert _build_baidu_baike_engine(spec) is not None  # builder 可实例化

    q = "苹果公司简介"
    q_keys = set(signal_tokens(q, stop={
        "the", "and", "for", "year", "founded", "founding", "headquarters",
        "where", "what", "when", "which", "with", "from", "that", "this",
        "年份", "时间", "成立", "创办", "创立", "总部", "职能", "简介",
    }))
    blob = "苹果公司 百度百科 苹果公司（Apple Inc.）是一家美国科技公司".lower()
    assert any(k in blob for k in q_keys), f"{q} → {q_keys} 仍过不了词条门"


def test_baike_gate_rejects_zero_overlap_entry():
    """零重叠词条（英文实体问返回「我们选择登月」）必须仍被丢弃。"""
    q_keys = set(signal_tokens("Apple Inc founding year", stop={
        "the", "and", "for", "year", "founded", "founding", "headquarters",
        "where", "what", "when", "which", "with", "from", "that", "this",
    }))
    blob = "我们选择登月 1969 年阿波罗计划".lower()
    assert not any(k in blob for k in q_keys)


# ── 站点 3：化学结果 token 交集 ──────────────────────────────────────────────


def test_chem_tokens_expand_spaceless_cjk():
    from engines_builders_data import _chem_tokens

    toks = _chem_tokens("阿司匹林的作用")
    assert {"阿司", "司匹", "匹林"} <= toks
    # 旧行为：整句「阿司匹林的作用」是一个 token，与词条「阿司匹林」零交集
    assert "阿司匹林的作用" not in toks


def test_chem_tokens_single_char_still_dropped():
    from engines_builders_data import _chem_tokens

    toks = _chem_tokens("水")
    assert all(len(t) >= 2 for t in toks), toks


# ── 站点 4：V2EX 中文标题反查节点 ───────────────────────────────────────────


def test_v2ex_cn_title_reverse_lookup():
    """「怎么找工作」应反查到 jobs——旧写法整句成词，与「酷工作」永不交集。"""
    from v2ex_nodes import pick_nodes

    nodes = [
        {"name": "jobs", "title": "酷工作", "header": "求职、招聘", "topics": 12345},
        {"name": "python", "title": "Python", "header": "编程语言", "topics": 999},
    ]
    result = pick_nodes("怎么找工作", nodes=nodes)
    assert "jobs" in result["nodes"]
    assert result["layer"] == "cn_title"


def test_v2ex_cn_title_reverse_lookup_still_abstains_on_no_match():
    """词面差异时诚实回落 none，不硬凑（「招聘」实测无命中）。"""
    from v2ex_nodes import pick_nodes

    nodes = [{"name": "python", "title": "Python", "header": "编程语言",
              "topics": 999}]
    result = pick_nodes("招聘", nodes=nodes)
    assert result["layer"] == "none" and result["nodes"] == []
