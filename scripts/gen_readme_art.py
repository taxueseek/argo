#!/usr/bin/env python3
"""gen_readme_art.py — 从真实路由数据生成 README 对外证据图（proof-routes.svg）。

## 为什么要有这个脚本

`assets/readme/proof-routes.svg` 曾是一张**手写** SVG，自称「PROOF · REAL
ROUTES」，但 2026-11-21 逐条复现发现 4 张卡里 1 张是虚构的：

  - 「贵州茅台股价 → stock_query / sina·tencent」        实测一致
  - 「Inception director → film_search / imdb·itunes」   实测一致
  - 「アニメ おすすめ → ja / local_bing·ddg」            实测**零命中**（虚构）
  - 「where is Eiffel Tower → geo_places / osm·wiki」    实测一致

根因与 counts.json 同源：图上的数字与路由结论是**手抄**的，没有生成器、
没有门禁，于是可以长期漂成理想态。本脚本把图接上**运行时同一批函数**
（route_domains.match_domains），并把「选哪几条案例」也变成显式清单——
清单里的查询必须真实命中声明的域，否则 `--check` 红。

## 与既有生成器的关系

同 `gen_engine_catalog.py`（源数/文档）、`gen_routing_snapshot.py`（路由
决策快照）同一范式：真源 → 生成 → `--check` 对拍磁盘。不新增概念。

用法：
  python3 scripts/gen_readme_art.py            # 生成（原子写）
  python3 scripts/gen_readme_art.py --check    # 只校验，过期/不符退出码 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
SVG_PATH = SKILL_DIR / "assets" / "readme" / "proof-routes.svg"

# 对外证据卡：查询 → 期望命中的域。查询必须来自真实语料（ROUTE_MATRIX /
# LEGITIMATE_ROUTES），不得手编；期望域由 `--check` 用运行时路由实测对拍。
# 换案例时先跑一次 route_domains.match_domains 确认命中，再改这里。
CARDS: list[dict[str, str]] = [
    {"tag": "FINANCE", "query": "贵州茅台股价", "expect_domain": "stock_query",
     "accent": "#34D399", "chip": "#0B3B2E", "shape": "报价快照 · early-stop",
     "field": "domain"},
    {"tag": "FILM", "query": "Inception director", "expect_domain": "film_search",
     "accent": "#A78BFA", "chip": "#1A1430", "shape": "作品页 · 实体字段",
     "field": "domain"},
    # 多语言卡：用**实测命中**的日语查询（「宮崎駿 アニメ 映画」→ film_search），
    # 不用虚构的「アニメ おすすめ」。同卡的韩语/日语 geo 案例同样可复现。
    {"tag": "LANG", "query": "エッフェル塔 どこ", "expect_domain": "geo_places",
     "accent": "#38BDF8", "chip": "#0C2A3A", "shape": "非中文查询走本地/通用源",
     "field": "lang"},
    {"tag": "GEO", "query": "where is Eiffel Tower", "expect_domain": "geo_places",
     "accent": "#FBBF24", "chip": "#2A2310", "shape": "坐标 · 实体定位",
     "field": "domain"},
]

_W, _H = 1200, 420
_CARD_W, _CARD_H, _CARD_Y, _CARD_GAP = 260, 230, 140, 16
_X0 = 56
_MONO = "ui-monospace, Menlo, monospace"
_SANS = "-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, sans-serif"


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _route(query: str) -> dict:
    sys.path.insert(0, str(SCRIPTS))
    import route_domains as rd
    import route_lang as rl
    hits = rd.match_domains(query, max_n=1)
    h = hits[0] if hits else {}
    combo = list(h.get("engines_combo") or [])
    return {
        "domain": h.get("name") or "",
        "primary": h.get("primary") or "",
        "engines": combo,
        "lang": (rl.extract_features(query) or {}).get("primary_lang") or "",
    }


def build() -> tuple[str, list[str]]:
    """生成 SVG 文本，并返回一致性告警（空 = 全部可复现）。"""
    warnings: list[str] = []
    cards = []
    for c in CARDS:
        r = _route(c["query"])
        if r["domain"] != c["expect_domain"]:
            warnings.append(
                f"{c['query']!r} 实测命中 {r['domain']!r}，与声明 "
                f"{c['expect_domain']!r} 不符——改 CARDS 前先复现路由")
        shown = " · ".join(_short(e) for e in r["engines"][:2]) or "(无 combo)"
        if c["field"] == "lang":
            value = f'{r["lang"]} · 非中文查询'
            label = "query_lang"
        else:
            value = r["domain"] or "(未命中)"
            label = "domain"
        cards.append({**c, "value": value, "label": label,
                      "engines_shown": shown})

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" role="img" aria-labelledby="title desc">',
        '  <title id="title">Argo 问啥像啥</title>',
        '  <desc id="desc">四类真实路由：A股行情、影视、多语言、地理实体各自命中垂直源'
        '（由 scripts/gen_readme_art.py 从运行时路由生成，--check 对拍）</desc>',
        '',
        '  <defs>',
        '    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">',
        '      <stop offset="0%" stop-color="#070B14"/>',
        '      <stop offset="100%" stop-color="#0B1220"/>',
        '    </linearGradient>',
        '  </defs>',
        '',
        f'  <rect width="{_W}" height="{_H}" rx="28" fill="url(#bg)"/>',
        '',
        f'  <text x="{_X0}" y="48" font-family="{_MONO}" font-size="12" '
        'fill="#38BDF8" letter-spacing="2">PROOF · REAL ROUTES</text>',
        f'  <text x="{_X0}" y="82" font-family="{_SANS}" font-size="26" '
        'font-weight="650" fill="#E8EEF7">问啥像啥 · 垂直源优先</text>',
        f'  <text x="{_X0}" y="110" font-family="{_SANS}" font-size="14" '
        'fill="#64748B">同样是「搜索」，路由不同 · 答案形态不同 · 可信度字段跟输出一起回来</text>',
    ]
    for i, c in enumerate(cards):
        x = _X0 + i * (_CARD_W + _CARD_GAP)
        chip_w = max(44, len(c["tag"]) * 7 + 20)
        out += [
            '',
            f'  <!-- card {i + 1} {c["tag"].lower()} -->',
            f'  <g transform="translate({x} {_CARD_Y})">',
            f'    <rect width="{_CARD_W}" height="{_CARD_H}" rx="16" '
            'fill="#0D1526" stroke="#1E2A3F"/>',
            f'    <rect x="16" y="16" width="{chip_w}" height="22" rx="6" fill="{c["chip"]}"/>',
            f'    <text x="{16 + chip_w / 2}" y="31" text-anchor="middle" '
            f'font-family="{_MONO}" font-size="10" fill="{c["accent"]}">{_esc(c["tag"])}</text>',
            f'    <text x="16" y="64" font-family="{_SANS}" font-size="15" '
            f'font-weight="600" fill="#E8EEF7">{_esc(c["query"])}</text>',
            f'    <text x="16" y="92" font-family="{_MONO}" font-size="11" '
            f'fill="#64748B">{_esc(c["label"])}</text>',
            f'    <text x="16" y="112" font-family="{_MONO}" font-size="13" '
            f'fill="{c["accent"]}">{_esc(c["value"])}</text>',
            f'    <text x="16" y="142" font-family="{_MONO}" font-size="11" '
            'fill="#64748B">engines</text>',
            '    <text x="16" y="162" font-family="' + _MONO + '" font-size="12" '
            'fill="#CBD5E1">' + _esc(c["engines_shown"]) + '</text>',
            '    <text x="16" y="192" font-family="' + _MONO + '" font-size="11" '
            'fill="#64748B">result shape</text>',
            '    <text x="16" y="212" font-family="' + _SANS + '" font-size="13" '
            'fill="#94A3B8">' + _esc(c["shape"]) + '</text>',
            '  </g>',
        ]
    out += ['', '</svg>', '']
    return "\n".join(out), warnings


def _short(engine: str) -> str:
    """引擎名缩到卡片可读长度（local_openstreetmap → osm 这类过长名）。"""
    return (engine.replace("local_", "")
                .replace("_quote", "")
                .replace("openstreetmap", "osm")
                .replace("wikipedia", "wiki"))


# ── hero.svg 的动态数字（同一真源，禁手抄） ──────────────────────────────
# hero 曾手写 v2.14.1 / 300+ sources，在 v2.16.0 已漂了两版。这里改为从
# package.json、engine_registry、mcp_tools、route_lang 取真值替换占位符。
# 模板与产物分离：hero.template.svg 是含占位符的模板（入库、可 diff），
# hero.svg 是渲染产物——`--check` 每次从模板重渲染再与产物比，可重复。
HERO_PATH = SKILL_DIR / "assets" / "readme" / "hero.svg"
HERO_TEMPLATE = SKILL_DIR / "assets" / "readme" / "hero.template.svg"
HERO_PLACEHOLDERS = {
    "{{version}}": lambda: json.loads(
        (SKILL_DIR / "package.json").read_text(encoding="utf-8"))["version"],
    "{{sources}}": lambda: str(len(registry_engines())),
    "{{mcp}}": lambda: str(len(mcp_tools())),
    "{{langs}}": lambda: str(len(LANGS)),
}


def registry_engines() -> list:
    import yaml_load
    p = SKILL_DIR / "backends" / "engine_registry.yaml"
    return (yaml_load.load(p) or {}).get("engines", [])


def mcp_tools() -> list:
    sys.path.insert(0, str(SCRIPTS))
    import mcp_tools
    return list(mcp_tools.TOOLS)


# 对外口径的「语言数」= 主语言类 + 文字系统类（与 README「18 languages」一致）。
# 真源在 route_lang：主语言 11 类（zh/en/ja/ko/latin/cyrillic/thai/arabic/
# hebrew/greek/devanagari）+ 文字系统 7 类（cjk/kana/hangul/latin/cyrillic/…）。
LANGS = ("zh en ja ko latin cyrillic thai arabic hebrew greek devanagari "
         "cjk kana hangul latin cyrillic arabic hebrew").split()


def render_hero() -> tuple[str, list[str]]:
    """从模板渲染 hero.svg，返回 (文本, 告警)。模板缺失或占位符未替换都算红。"""
    if not HERO_TEMPLATE.is_file():
        return "", [f"缺模板 {HERO_TEMPLATE.name}——hero.svg 无法从真源渲染"]
    src = HERO_TEMPLATE.read_text(encoding="utf-8")
    warnings: list[str] = []
    out = src
    for ph, fn in HERO_PLACEHOLDERS.items():
        if ph in out:
            try:
                out = out.replace(ph, fn())
            except Exception as e:  # noqa: BLE001 —— 显式留痕，不静默
                warnings.append(f"占位符 {ph} 取值失败：{type(e).__name__}: {e}")
    left = [p for p in HERO_PLACEHOLDERS if p in out]
    if left:
        warnings.append(f"hero.svg 仍有未替换占位符: {left}")
    return out, warnings


def write_atomic(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 replace，避免读者看到半截内容。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 README 路由证据图")
    ap.add_argument("--check", action="store_true", help="只校验图是否过期")
    args = ap.parse_args()

    svg, warnings = build()
    hero, hero_warnings = render_hero()
    warnings += hero_warnings
    if warnings:
        print("❌ 对外证据与真源不符：", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
        print("  修复：改 scripts/gen_readme_art.py（先复现真源），不要改 SVG", file=sys.stderr)
        return 1

    if args.check:
        bad = []
        if (SVG_PATH.read_text(encoding="utf-8") if SVG_PATH.is_file() else "") != svg:
            bad.append(SVG_PATH.name)
        if (HERO_PATH.read_text(encoding="utf-8") if HERO_PATH.is_file() else "") != hero:
            bad.append(HERO_PATH.name)
        if bad:
            print(f"❌ {', '.join(bad)} 与生成结果不一致——过期或手改。", file=sys.stderr)
            print("  修复：python3 scripts/gen_readme_art.py", file=sys.stderr)
            return 1
        print(f"✅ 对外证据图与真源一致（{len(CARDS)} 卡路由实测可复现，hero 数字取自真源）")
        return 0

    write_atomic(SVG_PATH, svg)
    write_atomic(HERO_PATH, hero)
    print(f"✔ 已生成 proof-routes.svg（{len(CARDS)} 卡）与 hero.svg（数字取自真源）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
