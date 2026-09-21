#!/usr/bin/env python3
"""iplant 引擎 builder：三级判据与边界（mock _http_get_raw，无网络）。

覆盖 2026-09-21 收录。这个源只有一件事能做对——中文名/俗名 → 学名 + 分类，
而它成不成立全压在三级判据上，所以每一级都单独锁一条用例：

  一级 spno 为空 → 诚实空。**且必须锁死「latin2 回填查询词」这个陷阱**：
       未收录时站点把查询词原样填进 latin2，只看 latin2 会把它当命中。
  二级 spno 非空 + latin2 非空 → 直接命中。
  三级 spno 非空 + latin2 为空 → 走「您是否要找」取接受名。这是俗名的主路径
       （玉米页 5 个俗名全部走这级），不是边缘情况。

另锁：systype 非植物过滤、查询去噪、分类链取不到时不拖累结果、URL 转义。

注意 `_run` 把引擎调用放在 patch 块**内**：放到块外会打真实网络，用例既锁不住
判据又依赖上游可用（本文件初版就踩了这个，断言 fixture 专有内容时才暴露）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import engines_builders_cn  # noqa: E402
from engines_builders_cn import _build_iplant_engine  # noqa: E402

# ── 夹具：形状照抄真实页面（服务端渲染的 var 段 + infomore 块）──────────────

_CORN = """
<script>
var spno = "36846";
var spnomd5 = "";
var spcname = "玉米";
var latin2 = "Zea mays";
var systype = "1";
</script>
<div id="sptitlel" class="infolatin">Zea mays</div>
<div class="infomore"  ><div>俗名：<a href='/info/苞米'>苞米</a>、<a href='/info/苞芦'>苞芦</a>、
<a href='/info/包谷'>包谷</a></div><div class='synt'>异名：</div>
<div class='sync'><a href='/info/Zea mays var. everta' class='synl'>Zea mays var. everta</a></div></div>
"""

# 别名页：spno 有值、latin2 空，「您是否要找」给出接受名（href 带内部 id）
_BAOGU = """
<script>
var spno = "278137";
var spcname = "包谷";
var latin2 = "";
var systype = "1";
</script>
<div class="infomore" style='margin-top:10px'>您是否要找：<span class='spantxt'>
<a href='/info/Zea mays?id=A1DEFA51BA91148A'>玉米 Zea mays</a>、</span></div>
"""

# 未收录：spno 空，但 latin2 把查询词回填了——陷阱所在
_NOT_FOUND = """
<script>
var spno = "";
var spcname = "";
var latin2 = "zzzznotexist";
var systype = "";
</script>
"""

# 非植物：恐龙条目，systype=2
_TREX = """
<script>
var spno = "972565";
var spcname = "霸王龙";
var latin2 = "Tyrannosaurus rex";
var systype = "2";
</script>
"""

_CLASSSYS = (
    '{"classsys": ["<a href=\'//www.iplant.cn/info/Zea mays\' style=\'color:#000\'>玉米 Zea mays</a>'
    "*<a href='//www.iplant.cn/info/Zea' style='color:#000'>玉米属 Zea</a>"
    "*<a href='//www.iplant.cn/info/Poaceae' style='color:#000'>禾本科 Poaceae</a>"
    "*<a href='//www.iplant.cn/info/Poales' style='color:#000'>禾本目 Poales</a>"
    "*<a href='//www.iplant.cn/info/Magnoliopsida' style='color:#000'>木兰纲 Magnoliopsida</a>"
    "*<a href='//www.iplant.cn/info/Angiospermae' style='color:#000'>被子 Angiospermae</a>\"]}"
)


def _run(routes: dict[str, str | None], query: str, n: int = 3,
         spec: dict[str, Any] | None = None):
    """在 patch 生效期内跑一次查询。routes 按 URL 子串匹配，未命中返回 None。"""
    calls: list[str] = []

    def _fake(url: str, headers: dict, timeout: float, engine: str = "?") -> str | None:
        calls.append(url)
        for key, val in routes.items():
            if key in url:
                return val
        return None

    with patch.object(engines_builders_cn, "_http_get_raw", side_effect=_fake):
        eng = _build_iplant_engine({"timeout": 5, "_name": "iplant", **(spec or {})})
        return eng(query, n=n), calls


class TestIplantLevels(unittest.TestCase):
    def test_level2_direct_hit_with_classification(self):
        rs, _ = _run({"/info/": _CORN, "classsys": _CLASSSYS}, "玉米")
        self.assertEqual(len(rs), 1)
        r = rs[0]
        self.assertEqual(r["title"], "玉米 Zea mays")
        self.assertIn("学名 Zea mays", r["snippet"])
        # 分类链首段是种本身，其后三段才是属/科/目
        self.assertIn("属 玉米属 Zea", r["snippet"])
        self.assertIn("科 禾本科 Poaceae", r["snippet"])
        self.assertIn("目 禾本目 Poales", r["snippet"])
        self.assertNotIn("木兰纲", r["snippet"])
        self.assertEqual(r["source"], "iplant")

    def test_level1_empty_spno_is_honest_empty(self):
        """未收录：spno 空 → 空。latin2 回填了查询词也不得当命中。"""
        rs, calls = _run({"/info/": _NOT_FOUND}, "zzzznotexist")
        self.assertEqual(rs, [])
        self.assertTrue(calls, "应发出过请求（判据在响应内容上，不在有没有请求）")

    def test_level3_alias_follows_suggestion(self):
        """别名页：走「您是否要找」取接受名，并补一次分类。"""
        rs, calls = _run({
            "/info/%E5%8C%85%E8%B0%B7": _BAOGU,   # 包谷
            "/info/Zea%20mays": _CORN,            # 接受名页
            "classsys": _CLASSSYS,
        }, "包谷")
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["title"], "玉米 Zea mays")
        self.assertIn("科 禾本科 Poaceae", rs[0]["snippet"])
        # 三级路径必须真的取了接受名页，不能只信提示文本
        self.assertTrue(any("Zea%20mays" in c for c in calls), calls)

    def test_non_plant_filtered(self):
        """霸王龙（systype=2）是「名称校对」的产物，对植物源是噪声。"""
        rs, _ = _run({"/info/": _TREX}, "霸王龙")
        self.assertEqual(rs, [])

    def test_systype_missing_does_not_block(self):
        """类群字段缺失时不拦——判不出就不拦截，宁放过不误杀。"""
        rs, _ = _run({"/info/": _CORN.replace('var systype = "1";', "")}, "玉米")
        self.assertEqual(len(rs), 1)


class TestIplantRobustness(unittest.TestCase):
    def test_classsys_failure_keeps_result(self):
        """分类是增益不是前提：classsys 取不到仍要交出学名。"""
        rs, _ = _run({"/info/": _CORN}, "玉米")   # 无 classsys 路由
        self.assertEqual(len(rs), 1)
        self.assertIn("学名 Zea mays", rs[0]["snippet"])
        self.assertNotIn("科 ", rs[0]["snippet"])

    def test_query_noise_stripped(self):
        rs, calls = _run({"/info/": _CORN, "classsys": _CLASSSYS}, "玉米 学名")
        self.assertEqual(len(rs), 1)
        self.assertTrue(any("%E7%8E%89%E7%B1%B3" in c for c in calls), calls)

    def test_url_percent_encoded(self):
        """中文名进 URL 必须转义（未转义会抛 UnicodeEncodeError）。"""
        rs, _ = _run({"/info/": _CORN, "classsys": _CLASSSYS}, "玉米")
        self.assertIn("%E7%8E%89%E7%B1%B3", rs[0]["url"])

    def test_empty_and_overlong_query(self):
        for bad in ("", "  ", "玉" * 41):
            rs, calls = _run({"/info/": _CORN}, bad)
            self.assertEqual(rs, [])
            self.assertEqual(calls, [], "空/超长查询不该发出请求")

    def test_vernacular_extracted(self):
        rs, _ = _run({"/info/": _CORN, "classsys": _CLASSSYS}, "玉米")
        self.assertIn("俗名 苞米、苞芦、包谷", rs[0]["snippet"])

    def test_http_failure_is_empty(self):
        """取不到页面 → 诚实空，不抛异常。"""
        rs, _ = _run({}, "玉米")
        self.assertEqual(rs, [])


class TestIplantRegistration(unittest.TestCase):
    def test_builder_registered(self):
        """spec.type=iplant 必须能路由到 builder（防 import 笔误）。"""
        from engines import _BUILDERS
        self.assertIn("iplant", _BUILDERS)

    def test_spec_declares_chinese_coverage(self):
        """coverage 必须含 chinese：否则中文查询的相关性判据会被跳过。"""
        from config import load_config, get_engines
        specs = get_engines(load_config(), routable_only=False)
        self.assertIn("iplant", specs)
        spec = specs["iplant"]
        self.assertEqual(spec.get("type"), "iplant")
        self.assertIn("chinese", spec.get("coverage") or [])
        self.assertTrue(spec.get("canary_query"))
        self.assertTrue(spec.get("quality_queries"))

    def test_family_is_science_bio(self):
        from engine_families import family_of
        self.assertEqual(family_of("iplant"), "science_bio")


if __name__ == "__main__":
    unittest.main()
