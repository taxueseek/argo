#!/usr/bin/env python3
"""calibrate_lowquality.py — 低质内容信号的区分度标定（2026-09-27 新增）。

存在的理由：clickbait / title_body / template 三道门槛（0.5 / 0.5 / 0.75）
原先都是拍脑袋的常数。手工构造两个样本时发现两个问题：
  - 精心打磨的现代农场页 clickbait 得 0.0（PACLIC 那套悬念/夸张词表对
    2026 年的 SEO 写法已失效）；
  - 正常技术长文 template 得 0.833，农场页 0.875 —— 信号基本没有判别力。
两者都是「看几个例子觉得还行」暴露不出来的，只有跑成批样本才知道。

本脚本输出三个量，都是**与阈值无关**的排序性质，故能先于调参使用：
  - AUC        : 该信号单独作为判别器的区分度（0.5 = 无判别力）
  - 最好阈值   : 扫全阈值区间取 F1 最优点，顺带给出现行阈值的位置
  - 混淆       : 现行阈值下 good 被误杀 / low 被漏放的具体样本 id

用法：
  python3 scripts/calibrate_lowquality.py            # 人读表格
  python3 scripts/calibrate_lowquality.py --json     # 机读（供门禁对比）

改任何阈值前后各跑一次，把数字贴进 commit —— 不要再写「感觉变好了」。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

FIXTURE = REPO / "tests" / "golden" / "lowquality_calibration.json"


def _signals(sample: dict) -> dict:
    """取一条样本的三个低质信号（与 content_signals 同源，避免口径分叉）。"""
    from content_signals import (score_clickbait, score_template_repetition,
                                 score_title_body_consistency)
    title = sample.get("title", "")
    content = sample.get("content", "")
    cb = score_clickbait(title)
    tb = score_title_body_consistency(title, content)
    tp = score_template_repetition(content)
    return {
        # clickbait: 越大越像农场
        "clickbait": float(cb.get("score", 0.0)),
        # template: 越大越像模板批量生成
        "template": float(tp.get("repetition", 0.0)),
        # title_body: 越小越文不对题 → 取反，使三个信号同向（越大越低质）
        "title_body_gap": 1.0 - float(tb.get("coverage", 0.5)),
        # keyword_density: 越大越是关键词堆砌（排序层信号，见 rank_signals）
        "keyword_density": _density(sample),
    }


def _density(sample: dict) -> float:
    """整体关键词密度（title+snippet 合并）。延迟导入避免顶层拖慢 --help。"""
    try:
        from rank_signals import keyword_density, relevance_units
    except ImportError:
        return 0.0
    uq = set(relevance_units(sample.get("query", "")))
    return keyword_density(
        f"{sample.get('title', '')} {sample.get('content', '')}", uq)


# 现行 evidence_loop.py 里的实际门槛，改动这里必须同步改那里
CURRENT_THRESHOLDS = {
    "clickbait": ("ge", 0.5),
    "title_body_gap": ("mismatch", 0.5),   # 语义阈值：coverage < 0.5
    "template": ("ge", 0.75),
    # 排序层：rank_signals.stuffing_density_penalty 的门槛
    "keyword_density": ("ge", 0.22),
}


def _auc(pairs: list[tuple[float, int]]) -> float:
    """AUC（越大越 low 方向）：正类（low=1）得分高于负类（good=0）的概率。

    约定：所有进入本函数的信号都**同向**——score 越大越像低质
    （clickbait / template / title_body_gap / keyword_density 皆如此，
    前两个本来就是，title_body_gap 在取反时已转正）。故 AUC 可直接跨信号
    比较，0.5 = 无判别力，>0.5 = 有判别力。ties 记 0.5。

    实现用排序累加而非 sklearn：零依赖，样本量小，O(n²) 完全够。
    """
    pos = [s for s, y in pairs if y == 1]
    neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return float("nan")
    total = 0.0
    for p in pos:
        for n in neg:
            total += 1.0 if p > n else (0.5 if p == n else 0.0)
    return total / (len(pos) * len(neg))


def _best_threshold(pairs: list[tuple[float, int]]) -> tuple[float, float]:
    """扫候选阈值，返回 (最佳 F1 的阈值, 该 F1)。

    候选取所有相邻分数的中点——对连续分数而言，判别边界只可能在数据点之间。
    分数完全相同的一组无法分开，此时 F1 反映的是「按分数盲猜」的上限。
    """
    vals = sorted({s for s, _ in pairs})
    if len(vals) < 2:
        return (vals[0] if vals else 0.0, 0.0)
    cands = [(vals[i] + vals[i + 1]) / 2 for i in range(len(vals) - 1)]
    best = (0.0, 0.0)
    for t in cands:
        tp = sum(1 for s, y in pairs if s >= t and y == 1)
        fp = sum(1 for s, y in pairs if s >= t and y == 0)
        fn = sum(1 for s, y in pairs if s < t and y == 1)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        if f1 > best[1]:
            best = (t, f1)
    return best


def run() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    samples = data["samples"]
    for s in samples:
        s["_sig"] = _signals(s)

    report: dict = {"n": len(samples), "signals": {}}
    for name in ("clickbait", "template", "title_body_gap", "keyword_density"):
        pairs = [(s["_sig"][name], 1 if s["quality"] == "low" else 0) for s in samples]
        auc = _auc(pairs)
        thr, f1 = _best_threshold(pairs)
        cur_t = CURRENT_THRESHOLDS[name][1]
        fp = [s["id"] for s in samples
              if s["quality"] == "good" and s["_sig"][name] >= cur_t]
        fn = [s["id"] for s in samples
              if s["quality"] == "low" and s["_sig"][name] < cur_t]
        report["signals"][name] = {
            "auc": round(auc, 4),
            "best_threshold": round(thr, 4),
            "best_f1": round(f1, 4),
            "current_threshold": cur_t,
            "false_positive_ids": fp,   # good 被判低质
            "false_negative_ids": fn,   # low 被放过
            # 分低质类别的 AUC：一个信号通常只对某一类低质负责，把它不负责的
            # 类别也塞进同一个 AUC 会得出「越改越差」的反直觉结论。
            # 2026-09-27 踩过：堆砌密度信号上线后总体 AUC 0.571→0.370，
            # 实际是因为「文不对题」「标题党」这些**密度天然低**的样本被算进
            # 了分母——它们 relevance 高是**正确**的（确实高度相关，只是别的
            # 问题），不该由密度信号压。按类拆开后该信号在 stuffing 类上
            # AUC=0.964（越大越低质方向），即近乎完美。
            "auc_by_lowclass": _auc_by_lowclass(samples, name),
        }
    return report


def _auc_by_lowclass(samples: list[dict], name: str) -> dict:
    """按低质类别（用 id 前缀归组）分别算 AUC。

    类别取 id 的第一个下划线段：farm_* / stuff_* / seo_good_*。
    """
    out: dict = {}
    groups = sorted({s["id"].split("_")[0] for s in samples
                     if s["quality"] == "low"})
    for g in groups:
        sub_low = [s for s in samples
                   if s["quality"] == "low" and s["id"].startswith(g + "_")]
        if not sub_low:
            continue
        neg = [s for s in samples if s["quality"] == "good"]
        if not neg:
            continue
        pairs = [(s["_sig"][name], 1) for s in sub_low] + \
                [(s["_sig"][name], 0) for s in neg]
        out[g] = round(_auc(pairs), 4)
    return out


def main() -> int:
    as_json = "--json" in sys.argv[1:]
    rep = run()
    if as_json:
        # 走统一入口而非 json.dumps(indent=2)：stdout 不出缩进 JSON
        # （tests/test_context_budget.py 门禁；人读格式在下方非 --json 分支）
        from cli_io import dumps
        print(dumps(rep))
        return 0

    print(f"低质信号区分度标定（{rep['n']} 条样本，好/坏对半）\n")
    print(f"{'信号':<18}{'总体AUC':>9}{'现门槛':>9}{'最佳门槛':>10}{'最佳F1':>9}")
    print("-" * 55)
    for name, m in rep["signals"].items():
        print(f"{name:<18}{m['auc']:>9.3f}{m['current_threshold']:>9.2f}"
              f"{m['best_threshold']:>10.3f}{m['best_f1']:>9.3f}")
    print("\n总体 AUC：0.5=无判别力，0.75=可用，0.9+=强。")
    print("⚠️ 总体 AUC 会误导——一个信号通常只对某一类低质负责。**按类看**：")
    for name, m in rep["signals"].items():
        byc = m.get("auc_by_lowclass") or {}
        if not byc:
            continue
        cells = "  ".join(f"{k}={v:.3f}" for k, v in sorted(byc.items()))
        print(f"  {name:<18} {cells}")
    print("\n误杀(good 判成 low) / 漏放(low 判成 good)：")
    for name, m in rep["signals"].items():
        print(f"  {name:<18} 误杀={m['false_positive_ids'] or '无'}")
        print(f"  {'':<18} 漏放={m['false_negative_ids'] or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
