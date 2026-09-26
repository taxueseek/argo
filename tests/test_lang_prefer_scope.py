"""C1 回归护栏：结果语言偏好必须对所有被追踪语言生效，而不是只对 ja/ko。

原始 bug（可复现）：`argo search "记忆宫殿"` 返回 5 条结果，其中
「記憶の宮殿」（日文）、「Paměťový palác」（捷克文）、「Method of loci」
（英文）三条与查询语言不符，只有前 2 条是中文。

三道闸门同时把中文排除在语言处理之外：

1. `search_rank._lang_prefer_rerank` 的入口条件写死 `primary_lang not in
   ("ja", "ko")` → 对 `zh` 是恒等空操作（return results 原样返回）。
2. `search_pipeline` 的调用点同样只对 ja/ko 触发。
3. `search_pipeline` 的 result_lang 噪声门显式跳过 `zh/en/mixed/other`
   （注释理由：中英是主战场，过早过滤会误伤）。

于是中文路径零语言感知。真正的错不在「该不该过滤 zh/en」——那是产品
判断——而在**这个判断被复制成了三份硬编码字面量**：任何新增的受追踪语言
都要改三处，漏一处就静默失效，而唯一能发现的方式是肉眼看出结果语种不对。

本测试锁定的是「数据驱动」这个属性，而不是某个具体语言的输出：
只要某语言被 `prefer_langs` 追踪，软排序就不得对它 no-op。
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import search_rank  # noqa: E402


def _mk(title, snippet=""):
    return {"title": title, "snippet": snippet, "url": "https://e.com/%d" % abs(hash(title))}


class TestLangPreferRerankIsDataDriven(unittest.TestCase):
    def test_zh_is_not_a_noop(self):
        """中文查询时，含中文的结果必须排到纯外文结果之前。"""
        results = [
            _mk("Paměťový palác", "Czech entry about memory"),
            _mk("Method of loci", "English entry"),
            _mk("记忆宫殿在日常学习中的应用", "中文条目"),
        ]
        out = search_rank._lang_prefer_rerank(results, "zh")
        self.assertEqual(
            out[0]["title"], "记忆宫殿在日常学习中的应用",
            "中文软排序对 zh 仍是 no-op：外文结果未被降权（原 bug）",
        )

    def test_zh_demotes_japanese_sharing_kanji(self):
        """中日共享汉字：日文标题必须被排除在「中文前置」之外。

        真实场景：原 bug 的结果里有「記憶の宮殿」——「記憶」是汉字，落在
        zh 的 CJK 区间内。若只判命中不判排除，它会被当成中文顶到第一位，
        比原 bug 更糟（语种过滤本该管这个，却主动把日文排到了最前）。
        """
        results = [
            _mk("記憶の宮殿", "記憶の宮殿（きおくのきゅうでん）"),
            _mk("记忆宫殿在日常学习中的应用", "中文条目"),
        ]
        out = search_rank._lang_prefer_rerank(results, "zh")
        self.assertEqual(
            out[0]["title"], "记忆宫殿在日常学习中的应用",
            "含假名的日文标题被当成中文了——zh 判定缺少「排除日/韩」这一侧",
        )

    def test_en_is_identity_by_design(self):
        """en 刻意不进表：拉丁字母是 web 通用语种，判定无区分度。

        这是设计决定而非遗漏：若把拉丁字母算作 en 的特征，几乎所有 web
        结果都会命中，该信号等于没写。故 en 走恒等，由 RRF 相关度排序负责。
        """
        results = [_mk("記憶の宮殿", "日本語"), _mk("Method of loci", "English")]
        out = search_rank._lang_prefer_rerank(results, "en")
        self.assertIs(out, results, "en 不应触发基于书写系统的重排")

    def test_ja_ko_still_work(self):
        """既有行为不能回退。"""
        ja = [_mk("パレス", "日本語"), _mk("Palace", "English")]
        self.assertEqual(search_rank._lang_prefer_rerank(ja, "ja")[0]["title"], "パレス")
        ko = [_mk("기억", "한국어"), _mk("Palace", "English")]
        self.assertEqual(search_rank._lang_prefer_rerank(ko, "ko")[0]["title"], "기억")

    def test_untracked_lang_is_identity(self):
        """未追踪语言仍应原样返回（不做无意义重排）。"""
        results = [_mk("a", "x"), _mk("b", "y")]
        for lang in ("th-ai", "vi", "xx", None, ""):
            out = search_rank._lang_prefer_rerank(results, lang)
            self.assertIs(
                out, results,
                "未追踪语言 %r 应保持恒等，不要引入无谓排序" % (lang,),
            )

    def test_empty_results_is_safe(self):
        self.assertEqual(search_rank._lang_prefer_rerank([], "zh"), [])


class TestNoHardcodedLangGateRemains(unittest.TestCase):
    """防「把三处硬编码抄成第四处」——这是本 bug 的类别本身。"""

    FILES = ("search_rank.py", "search_pipeline.py")

    def _src(self, name):
        p = Path(__file__).resolve().parent.parent / "scripts" / name
        # 去掉注释：解释「原先这里是硬编码 ja/ko」的那段话本身必然含有该字面量，
        # 断言要盯的是**可执行代码**里还有没有这个闸门。
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            out.append(line.split("  #", 1)[0] if "  #" in line else line)
        return "\n".join(out)

    def test_lang_prefer_rerank_has_no_ja_ko_literal_gate(self):
        src = self._src("search_rank.py")
        # 允许 _LANG_SCRIPT 表内出现 "ja"/"ko"，但不允许出现把它当准入闸门的写法
        self.assertNotRegex(
            src, r'primary_lang\s+not\s+in\s+\(\s*"ja"\s*,\s*"ko"\s*\)',
            "软排序入口仍是硬编码 ja/ko 闸门——新语言会静默失效",
        )

    def test_pipeline_call_site_has_no_ja_ko_literal_gate(self):
        src = self._src("search_pipeline.py")
        gate = re.search(r'_p_lang\s+in\s+\(\s*"ja"\s*,\s*"ko"\s*\)', src)
        self.assertIsNone(
            gate, "调用点仍是硬编码 ja/ko 闸门（search_pipeline.py）",
        )

    def test_no_baseline_lang_whitelist_literal(self):
        """可执行代码里不得出现中英白名单字面量（第三处，2026-09-27 收口）。

        本 bug 类别是「三处硬编码抄成第四处」：软排序、调用点两处早已被上面
        两条断言锁住，而噪声门的排除表 ("zh", "en", "mixed", "other", "")
        一直在暗处——新增受追踪语言时改了前两处、忘了它，噪声门就对新语言
        静默失效。现在三处都从 lang_pref 的 BASELINE_LANGS / WEAK_QUERY_LANGS
        派生，这条断言锁住「不要再出现字面量」这个类别本身。
        """
        for name in self.FILES:
            src = self._src(name)
            hit = re.search(
                r'not\s+in\s+\(\s*"zh"\s*,\s*"en"\s*,\s*"mixed"', src)
            self.assertIsNone(
                hit,
                f"{name} 又出现中英白名单字面量闸门——从 lang_pref 派生，"
                f"别抄第四处",
            )


if __name__ == "__main__":
    unittest.main()
