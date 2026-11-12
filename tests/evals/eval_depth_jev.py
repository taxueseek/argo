#!/usr/bin/env python3
"""eval_depth_jev — JEV 深度判定黄金评测（真实模型通道）。

为什么不在 pytest 里算 F1（评审 2026-11-12）：真实 F1 需要真实模型输出。
pytest 里 mock 掉 classify 后算出的 F1 是「mock 读取标注再原样返回」的
恒等映射——预测恒等于标注、F1 恒满分，对判定质量退化零敏感，属循环论证
（264b112 首版踩坑，已撤销）。分工因此是：

- tests/test_semantic_evidence_jev.py（快门禁，离线）：管道契约 + 黄金集
  健康度——防止过滤、输入构建、映射、阈值逻辑退化；
- 本脚本（手动/夜间，真实模型）：黄金样本过真实 classify 通道，与人工
  标注对拍 Precision / Recall / F1——度量判定质量本身。

评测按 topic 分组调用（黄金集 10 个主题各 2 条）：assess_substantive_
discussion 的 topic 只有一个槽位，跨主题合评会让 instructions 失真。
JEV 通道本身 one request per passage，请求数与批量等价。

用法：
  .venv/bin/python tests/evals/eval_depth_jev.py             # 真实评测
  .venv/bin/python tests/evals/eval_depth_jev.py --dry-run   # 打印发往模型的输入契约，不调用

退出码：0 = 指标全达标；1 = 跑通但有指标低于阈值；2 = 评测未完成（缺
key、网络、响应异常、部分样本未评上）——失败必须可见，不静默记 0 分。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import semantic_evidence as se  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden_depth_jev.json"
POSITIVE = "substantive in-depth discussion"
DEFAULT_THRESHOLDS = {"min_precision": 0.9, "min_recall": 0.9, "min_f1": 0.9}


def load_golden() -> tuple[list[dict], dict[str, float]]:
    """读黄金集并做 schema 校验。坏格式直接退出——评测不跑在脏数据上。"""
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    samples = data.get("samples") or []
    if len(samples) < 20:
        sys.exit(f"[eval] 黄金集样本不足 20 条（当前 {len(samples)}），先修数据集")
    valid = set(se.SUBSTANTIVE_DISCUSSION_LABELS)
    for s in samples:
        if s.get("expected_label") not in valid:
            sys.exit(f"[eval] 样本 {s.get('id')} 标注非法: {s.get('expected_label')!r}")
    th = dict(DEFAULT_THRESHOLDS)
    th.update({k: float(v) for k, v in (data.get("metric_thresholds") or {}).items()
               if k in DEFAULT_THRESHOLDS})
    return samples, th


def build_items(samples: list[dict]) -> list[dict]:
    return [{"url": f"https://eval.local/{s['id']}", "title": s["title"],
             "text": s["text"]} for s in samples]


def group_by_topic(samples: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for s in samples:
        groups.setdefault(s.get("topic") or "通用主题", []).append(s)
    return groups


def dry_run(samples: list[dict]) -> None:
    """打印 assess 实际会发往模型的输入（捕获 classify 参数），不调用。

    展示第一组——与真实评测同构（评测按 topic 分组逐组调用），
    避免「单 topic 发全部样本」这种与真实执行不符的假契约。
    """
    captured: dict = {}

    def capture(texts, labels, instructions=None, criteria=None, **_kw):
        captured.update(texts=list(texts), labels=list(labels),
                        instructions=instructions, criteria=criteria)
        return None

    groups = group_by_topic(samples)
    topic = next(iter(groups))
    with patch.object(se, "classify", side_effect=capture):
        se.assess_substantive_discussion(topic, build_items(groups[topic]))
    if not captured:
        sys.exit("[dry-run] 管道返回 None：样本全部被过滤（检查黄金集 text/url）")
    print(f"展示第 1/{len(groups)} 组（topic={topic!r}，真实评测逐组调用）")
    print(f"instructions: {captured['instructions']}")
    print(f"labels: {captured['labels']}")
    print(f"criteria keys: {sorted(captured['criteria'])}")
    print(f"本组发送条数: {len(captured['texts'])}")
    for i, t in enumerate(captured["texts"], 1):
        print(f"  [{i}] title={t.splitlines()[0]!r} chars={len(t)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="JEV 深度判定黄金评测（真实模型通道）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印发往模型的输入契约，不调用模型")
    args = ap.parse_args()

    samples, thresholds = load_golden()
    if args.dry_run:
        dry_run(samples)
        return 0

    groups = group_by_topic(samples)

    results: dict[str, dict] = {}
    failed_groups: list[str] = []
    for topic, group in groups.items():
        r = se.assess_substantive_discussion(topic, build_items(group))
        if r:
            results.update(r)
        else:
            failed_groups.append(topic)

    if not results:
        key_ready = bool(se.jev_api_key())
        transport = se._transport()
        hint = (f"key 未配置（需 {'/'.join(se.JEV_KEY_ENV)}，config transport={transport}）"
                if not key_ready else
                f"key 已配置（transport={transport}），检查网络与端点可达性")
        print(f"[eval] classify 通道不可用，评测未执行——{hint}。不静默记 0 分。",
              file=sys.stderr)
        return 2
    if failed_groups:
        print(f"[eval] {len(failed_groups)}/{len(groups)} 组评测失败（{failed_groups}），"
              "结果不完整不计算 F1——先修通道或剔除坏组", file=sys.stderr)
        return 2

    tp = fp = fn = 0
    misses = []
    for s in samples:
        judgment = results.get(f"https://eval.local/{s['id']}") or {}
        pred = bool(judgment.get("is_substantive"))
        gold = s["expected_label"] == POSITIVE
        if pred and gold:
            tp += 1
        elif pred and not gold:
            fp += 1
            misses.append((s, judgment))
        elif not pred and gold:
            fn += 1
            misses.append((s, judgment))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    tn = len(samples) - tp - fp - fn
    print(f"样本 {len(samples)}：TP={tp} FP={fp} FN={fn} TN={tn}")
    print(f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}")
    for s, j in misses:
        print(f"  ✗ {s['id']} pred={j.get('label')!r}/{j.get('confidence')} "
              f"gold={s['expected_label']!r}")
        print(f"    标注理由: {s['rationale'][:80]}")

    ok = (precision >= thresholds["min_precision"]
          and recall >= thresholds["min_recall"]
          and f1 >= thresholds["min_f1"])
    if not ok:
        print(f"[eval] 低于阈值 {thresholds}——判定质量退化或标注需人工复审",
              file=sys.stderr)
        return 1
    print(f"[eval] 达标（阈值 {thresholds}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
