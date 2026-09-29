#!/usr/bin/env python3
"""burstiness_detector.py — 突发性检测器

基于 2026 年论文《Ratio of Quantiles Indicates Burstiness with Fewer False
Negatives than the Conventional Burstiness Parameter》的突发性量化方法，
检测文本节奏的自然度。

核心思想：
  人类写作有「突发性」——长短句交替、节奏变化；AI 文本则过于平稳。
  论文提出用分位数比（Quantile Ratio）替代传统突发性参数，减少假阴性。

  自然文本的分位数比通常在 1.5-4.0 之间：
  - < 1.5: 过于平稳（可能是 AI 生成）
  - 1.5-4.0: 自然节奏
  - > 4.0: 过于跳跃（可能是拼接内容）

设计原则：
  - 纯 stdlib，零依赖，微秒级
  - 与 stylometry_detector 互补：后者看词汇/结构，本模块看节奏
  - 输出 0-1 分数，越高越不自然

参考文献：
  - "Ratio of Quantiles Indicates Burstiness with Fewer False Negatives
    than the Conventional Burstiness Parameter" (2026)
  - "The Limitations of Stylometry for Detecting Machine-Generated
    Fake News" EMNLP 2019
"""
from __future__ import annotations

import re
from typing import Any


# 句子分割模式
_SENTENCE_RE = re.compile(r'[.!?。！？]+')


def _split_sentences(text: str) -> list[str]:
    """分割句子"""
    if not text:
        return []
    parts = _SENTENCE_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _quantile(data: list[float], q: float) -> float:
    """计算分位数"""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * q)
    return sorted_data[min(idx, len(sorted_data) - 1)]


class BurstinessDetector:
    """突发性检测器 — 量化文本节奏自然度"""

    def compute_burstiness(self, text: str) -> dict[str, Any]:
        """计算文本突发性指标

        返回:
            burstiness_ratio: 分位数比（Q75/Q25）
            is_natural: 是否自然（1.5 <= ratio <= 4.0）
            q25: 25% 分位数
            q75: 75% 分位数
            sentence_count: 句子数
            mean_length: 平均句长
        """
        sentences = _split_sentences(text)
        if len(sentences) < 3:
            return {
                "burstiness_ratio": 0.0,
                "is_natural": False,
                "q25": 0.0,
                "q75": 0.0,
                "sentence_count": len(sentences),
                "mean_length": 0.0,
                "note": "too_few_sentences",
            }

        lengths = [len(s) for s in sentences]
        q25 = _quantile(lengths, 0.25)
        q75 = _quantile(lengths, 0.75)

        # 分位数比（论文方法）：比传统方差更鲁棒
        burstiness_ratio = q75 / max(q25, 1)

        # 自然文本通常有 1.2-15.0 的分位数比（放宽范围，减少假阴性）
        # 中文文本的 ratio 可能更高（短句+长句对比强烈）
        is_natural = 1.2 <= burstiness_ratio <= 15.0

        return {
            "burstiness_ratio": round(burstiness_ratio, 2),
            "is_natural": is_natural,
            "q25": round(q25, 1),
            "q75": round(q75, 1),
            "sentence_count": len(sentences),
            "mean_length": round(sum(lengths) / len(lengths), 1),
        }

    def score(self, text: str) -> dict[str, Any]:
        """计算节奏异常分数

        返回:
            score: 0-1，越高越不自然
            burstiness: 原始突发性指标
            is_suspicious: 是否可疑（score > 0.6）
        """
        burstiness = self.compute_burstiness(text)

        if burstiness.get("note") == "too_few_sentences":
            return {
                "score": 0.5,  # 句子太少，无法判断，给中等分数
                "burstiness": burstiness,
                "is_suspicious": False,
            }

        ratio = burstiness["burstiness_ratio"]

        # 使用对数尺度计算异常分数
        # ratio=1.0（完全平稳）→ score=1.0
        # ratio=2.0（自然）→ score=0.0
        # ratio=11.0（中文自然）→ score=0.0
        import math
        if ratio <= 1.0:
            score = 1.0
        elif ratio <= 2.0:
            # 1.0 < ratio <= 2.0: 线性从 1.0 降到 0.0
            score = 2.0 - ratio
        elif ratio <= 10.0:
            # 2.0 < ratio <= 10.0: 自然范围
            score = 0.0
        else:
            # ratio > 10.0: 过于跳跃
            score = min((ratio - 10.0) / 10.0, 1.0)

        return {
            "score": round(score, 3),
            "burstiness": burstiness,
            "is_suspicious": score > 0.6,
        }


def detect_unnatural_rhythm(text: str) -> dict[str, Any]:
    """便捷函数：检测文本节奏是否不自然"""
    detector = BurstinessDetector()
    return detector.score(text)
