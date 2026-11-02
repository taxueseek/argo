#!/usr/bin/env python3
"""concept_lookup.py — Argo 静态概念向量快速查表器 (P2, Model2Vec 思想)。

纯 Python 标准库零依赖实现（零 PyTorch/ONNX，零 C 扩展）：
  - 基于 mmap 内存映射读取 backends/concept_vectors.bin
  - 64 维 int8 对称量化向量，单次点积耗时 < 2 微秒
  - 提供跨语言与同义近义概念余弦相似度计算，赋能 tfidf_router 与 seek_rank
  - 自动加载 backends/engine_concept_vectors.json 引擎语义中心向量
  - 任何文件损坏或缺失时严格 fail-open，不阻断主链路搜索
"""

from __future__ import annotations

import json
import math
import mmap
import os
import struct
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
BACKENDS_DIR = ROOT / "backends"
BIN_PATH = BACKENDS_DIR / "concept_vectors.bin"
ENGINE_VECS_PATH = BACKENDS_DIR / "engine_concept_vectors.json"

_NORM_SCALE = 10000.0


class ConceptLookup:
    """零依赖极速概念向量查表器。"""

    def __init__(self, bin_path: Path | str | None = None):
        self._bin_path = Path(bin_path) if bin_path else BIN_PATH
        self._mm: mmap.mmap | None = None
        self._f = None
        self._vocab: dict[str, int] = {}
        self._vec_offset = 0
        self._vocab_size = 0
        self._dim = 64
        self._engine_vecs: dict[str, list[int]] = {}
        self._available = False
        self._load()

    def _load(self) -> None:
        if not self._bin_path.exists():
            return
        try:
            self._f = open(self._bin_path, "rb")
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
            if len(self._mm) < 32:
                return
            magic, vocab_size, dim, quant_type = struct.unpack_from("<8sIHb", self._mm, 0)
            if magic != b"ARGOVEC1" or dim != 64 or quant_type != 1:
                return
            self._vocab_size = vocab_size
            self._dim = dim

            # 读取词汇表
            offset = 32
            vocab: dict[str, int] = {}
            for i in range(vocab_size):
                if offset >= len(self._mm):
                    return
                w_len = self._mm[offset]
                offset += 1
                w_bytes = self._mm[offset : offset + w_len]
                offset += w_len
                word = w_bytes.decode("utf-8", errors="ignore")
                vocab[word] = i

            self._vocab = vocab
            self._vec_offset = offset
            self._available = True
        except Exception:
            self._available = False
            if self._f:
                try:
                    self._f.close()
                except Exception:
                    pass
            self._mm = None
            self._f = None

        # 尝试加载引擎预置向量
        if ENGINE_VECS_PATH.exists():
            try:
                self._engine_vecs = json.loads(ENGINE_VECS_PATH.read_text(encoding="utf-8"))
            except Exception:
                self._engine_vecs = {}

    @property
    def is_available(self) -> bool:
        return self._available

    def get_vector(self, token: str) -> memoryview | None:
        """获取单个词元的 64 维 int8 向量 (memoryview 零拷贝，带符号 int8)。"""
        if not self._available or not self._mm:
            return None
        idx = self._vocab.get(token.lower().strip())
        if idx is None:
            return None
        start = self._vec_offset + idx * self._dim
        return memoryview(self._mm)[start : start + self._dim].cast("b")

    def vector_cosine(self, vec_a, vec_b) -> float:
        """计算两个 64 维 int8 向量的余弦相似度 (-1.0 ~ 1.0)。"""
        if not vec_a or not vec_b:
            return 0.0
        # 64 维纯整数点积累加
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        return max(-1.0, min(1.0, float(dot) / _NORM_SCALE))

    def text_vector(self, text: str) -> list[int] | None:
        """将短语或查询聚合为归一化的 64 维 int8 概念向量。"""
        if not self._available or not text:
            return None
        # 提取候选词元：整词、空格切词、连续CJK片段
        raw_tokens = []
        for part in text.lower().split():
            clean = part.strip(",.!?;:()[]{}\"'“”‘’，。！？；：")
            if clean:
                raw_tokens.append(clean)
                if len(clean) >= 2:
                    raw_tokens.extend(clean[i : i + 2] for i in range(len(clean) - 1))

        matched_vecs = []
        for t in raw_tokens:
            v = self.get_vector(t)
            if v is not None:
                matched_vecs.append(v)

        if not matched_vecs:
            return None

        # 向量求和后做 L2 归一化并缩放至 100.0
        acc = [0.0] * self._dim
        for vec in matched_vecs:
            for i in range(self._dim):
                acc[i] += float(vec[i])

        norm_sq = sum(x * x for x in acc)
        if norm_sq <= 0:
            return None
        scale = 100.0 / math.sqrt(norm_sq)
        return [int(round(x * scale)) for x in acc]

    def concept_similarity(self, text_a: str, text_b: str) -> float:
        """计算两个文本的静态概念余弦相似度。"""
        va = self.text_vector(text_a)
        if va is None:
            return 0.0
        vb = self.text_vector(text_b)
        if vb is None:
            return 0.0
        return self.vector_cosine(va, vb)

    def get_engine_vector(self, engine: str) -> list[int] | None:
        """获取引擎的预计算语义中心向量。"""
        return self._engine_vecs.get(engine)


_GLOBAL_LOOKUP: Optional[ConceptLookup] = None


def get_concept_lookup() -> ConceptLookup:
    """获取 ConceptLookup 单例。"""
    global _GLOBAL_LOOKUP
    if _GLOBAL_LOOKUP is None:
        _GLOBAL_LOOKUP = ConceptLookup()
    return _GLOBAL_LOOKUP


def concept_router_enabled() -> bool:
    """概念语义路由开关：默认开，ARGO_CONCEPT_ROUTER=0 时关闭。"""
    try:
        from engine_env import env_flag
        return env_flag("ARGO_CONCEPT_ROUTER", True)
    except Exception:
        val = os.environ.get("ARGO_CONCEPT_ROUTER", "1").strip().lower()
        return val not in ("0", "off", "no", "false")
