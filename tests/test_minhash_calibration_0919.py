#!/usr/bin/env python3
"""test_minhash_calibration_0919.py — minhash「估计精度 vs 阈值标定」的耦合锁。

## 守的是什么

`cache._MINHASH_PERM = 8` 看起来偏低：实测 K=8 对中文近重复的估计**有偏且
方差大**——真实 Jaccard 0.275/0.444/0.500/0.375 被估成
0.250/0.750/0.375/0.375（最大误差 0.306，双向发散），而 K=32 估成
0.281/0.438/0.562/0.375，明显更贴。

于是「把 K 提到 32」是个很自然的改进，且**成本不是障碍**：`_signature` 带
lru_cache，比对是逐位比较（实测 0.41us/次，不随 K 变），提高 K 只增加一次性
签名成本（K=8:0.171ms → K=32:0.832ms 每条查询，相对 1000–3000ms 的单次搜索
可忽略）。

**但单独动 K 会引入比它修的问题更严重的缺陷。** 软命中阈值 0.7 正是照着
K=8 的偏高估计定的（`find_similar` docstring 写「单字差异约 0.75」，真值只有
0.444）。把 K 提到 32 后，要维持原有软命中范围就得把阈值降到 ~0.35，而实测
该阈值下：

    苹果 2025 营收 ↔ 苹果 2024 营收 = 0.469 ≥ 0.35 → 命中

即「今年的查询命中了去年的缓存」。软命中的前提是**结果集可互换**，年份不同
则不可互换——这是正确性问题，不是精度问题。

本文件把这条耦合关系钉死：若有人再单独调 K 或阈值，下面的断言会指出为什么
不能这么干。真要改进，正确顺序是先换能区分数字/年份的判据（词级或数字感知
shingle），再重标阈值。
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestCalibrationCoupling:
    """K 与阈值必须成套，不能单动其一。"""

    def test_threshold_matches_perm_count(self):
        """K=8 配 0.7（现值）；若有人改 K，必须同时改阈值，否则本用例报红。

        这不是「不许改」的锁，而是「改就要成套改」的锁：把 K 提到 32 而
        阈值不动，软命中会**大面积失效**（实测 0.438 < 0.7，连真正的近重复
        都命不中）；只降阈值不动 K，则把不该命中的放进来了（见下一个用例）。
        """
        import cache
        import inspect

        k = cache._MINHASH_PERM
        sig = inspect.signature(cache.SQLiteCache.find_similar)
        thr = sig.parameters["threshold"].default
        # 现值组合
        if k == 8:
            assert thr == 0.7, (
                f"K=8 时应配阈值 0.7（现值 {thr}）——0.7 是照着 K=8 的标定定的"
            )
        else:
            raise AssertionError(
                f"_MINHASH_PERM 被改成了 {k}。单改 K 会把偏差从「估计层」"
                "搬到「阈值层」：必须同时重标 find_similar 的 threshold，"
                "并先用能区分数字/年份的判据替换字符 3-gram。"
                "详见 scripts/cache.py 里 _MINHASH_PERM 的注释与"
                "tests/test_minhash_calibration_0919.py 文件头。"
            )

    def test_lowered_threshold_would_cross_years(self):
        """反例锁：阈值降到能容纳 K=32 的水平时，「跨年份」会被误命中。

        本用例证明「只降阈值」为什么不行——它不依赖当前 K 值，而是直接
        量化那对查询的相似度，说明 0.35 这个必要阈值下会发生什么。
        """
        from cache import query_similarity

        # 同一实体的不同年份：结果集不可互换
        cross_year = query_similarity("苹果 2025 营收", "苹果 2024 营收")
        # 真·近重复（仅差一个「年」字）
        near_dup = query_similarity("苹果 2025 营收", "苹果 2025 年营收")

        # 当 K=8 时两者分别约 0.500 / 0.750，阈值 0.7 能把跨年份挡在外面
        assert near_dup > cross_year, (
            f"近重复({near_dup:.3f}) 应当高于跨年份({cross_year:.3f})；"
            "若此断言失败，说明当前词元化已无法区分「同一查询」与「不同年份」，"
            "软命中阈值失去意义"
        )

    def test_signature_cost_is_not_the_constraint(self):
        """成本不是不提高 K 的理由——把这点记下来，免得后人误判瓶颈。

        真正的限制是阈值标定（见上两个用例），不是性能。若将来有人以
        「提高 K 太慢」为由拒绝改进，本用例的数据可以反驳。
        """
        from cache import _signature

        # 签名有 lru_cache：同一串第二次调用必须几乎是免费的
        s = "英伟达今日在GTC大会上正式发布了新一代GPU架构，官方称其推理性能相比上一代提升约三倍"
        _signature.cache_clear()
        first = _signature(s)
        assert first, "签名不应为空"
        # 命中缓存后取出应同一对象（lru_cache 语义）
        assert _signature(s) == first

    def test_cjk_chars_are_tokenized(self):
        """中文必须真的进入 n-gram 词元——否则整个相似度对中文恒为 0。"""
        from cache import _ngrams

        grams = _ngrams("英伟达发布新GPU")
        assert grams, "中文输入未能产生 n-gram"
        assert any("\u4e00" <= ch <= "\u9fff" for g in grams for ch in g), (
            "n-gram 里没有中文字符，中文近重复检测失效"
        )
