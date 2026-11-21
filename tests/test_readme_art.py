#!/usr/bin/env python3
"""test_readme_art.py — 对外证据图与真源对拍（离线、确定性）。

## 守的是什么

`assets/readme/*.svg` 曾全部手写，于是可以长期漂成理想态而无人发现。
2026-11-21 逐条复现实证：`proof-routes.svg` 自称「PROOF · REAL ROUTES」，
4 张卡里 1 张虚构——「アニメ おすすめ → ja / local_bing·ddg」实测零命中；
`hero.svg` 写 v2.14.1 / 300+ sources，在 v2.16.0 已漂两版。

本类把图接上生成器（`scripts/gen_readme_art.py`）：路由结论走
`route_domains.match_domains`，数字走 package.json / engine_registry /
mcp_tools / route_lang。图被手改或过期即红。

## 口径边界（重要）

README 的**历史表**里「v2.16.0 …… 312 sources」是**当时的事实**，不是错——
多出来的 `helve` 源在 v2.16.0 之后才入库，属于未发布的本地改动。所以：
历史条目不改，当前口径由生成器与 counts.json 维护。把这两件事混起来
去改历史，等于篡改发布记录。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen_readme_art.py"


class TestReadmeArt:
    def test_generator_check_passes(self):
        """图与真源一致——手改 SVG 或真源变更后未重生成，这里就红。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        )
        assert r.returncode == 0, (
            f"对外证据图与真源不符：\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")

    def test_svg_contains_no_literal_route_claims(self):
        """图里不得再出现手抄的路由结论（必须由生成器写）。"""
        svg = (ROOT / "assets" / "readme" / "proof-routes.svg").read_text(encoding="utf-8")
        assert "gen_readme_art.py" in svg, "图未标注生成器来源，无法追溯"

    def test_hero_numbers_come_from_generator(self):
        """hero 的数字必须是真源值，不是手抄常量。"""
        hero = (ROOT / "assets" / "readme" / "hero.svg").read_text(encoding="utf-8")
        import json
        ver = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
        assert f">{ver}<" in hero, f"hero 版本号不是当前 {ver}（手抄漂移）"
        assert "{{" not in hero, "hero 仍有未替换的占位符"

    def test_generator_script_is_registered(self):
        """生成器入 pre-commit 清单——防「新增生成器靠人记得跑」。"""
        man = (ROOT / "tests" / "gate_manifest.txt").read_text(encoding="utf-8")
        assert "tests/test_readme_art.py" in man, (
            "test_readme_art.py 未入册——门禁命名类漏册")
