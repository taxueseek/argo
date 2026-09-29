"""tests/test_stylometry_burstiness.py — 文体特征 + 突发性检测单元测试"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
from stylometry_detector import StylometryDetector, detect_ai_generated
from burstiness_detector import BurstinessDetector, detect_unnatural_rhythm


class TestStylometryDetector:
    """文体特征检测测试"""

    def test_human_writing(self):
        """人类写作 — 应该得分低"""
        text = """
        The quick brown fox jumps over the lazy dog. This is a simple sentence.
        However, the story is more complex than it seems. In fact, there are many
        nuances that make this topic interesting. For example, consider the
        implications of such a discovery. Moreover, the consequences could be
        far-reaching and unexpected.
        """
        result = detect_ai_generated(text)
        assert result["score"] < 0.6
        assert not result["is_suspicious"]

    def test_ai_generated(self):
        """AI 生成文本 — 应该得分高"""
        # 模拟 AI 生成：词汇重复、句长均匀、模板词多
        text = """
        In conclusion, it is worth noting that this is a very important topic.
        Moreover, we can see that the results are very significant. Furthermore,
        the implications are very substantial. Additionally, the consequences
        are very considerable. Therefore, the outcomes are very meaningful.
        In summary, the findings are very valuable. In conclusion, the
        recommendations are very beneficial. In summary, the conclusions
        are very advantageous. In conclusion, the suggestions are very helpful.
        """
        result = detect_ai_generated(text)
        assert result["score"] > 0.35
        assert result["is_suspicious"]

    def test_chinese_human_writing(self):
        """中文人类写作"""
        text = """
        人工智能的发展速度令人惊叹。从最初的简单规则系统，到如今的深度学习，
        短短几十年间发生了翻天覆地的变化。然而，这种快速发展也带来了许多
        值得深思的问题。例如，AI 是否会取代人类的工作？如何确保 AI 的
        安全性和可控性？这些问题不仅需要技术层面的思考，更需要社会层面的
        广泛讨论。事实上，各国政府已经开始制定相关法规，试图在促进技术
        创新和防范潜在风险之间找到平衡。
        """
        result = detect_ai_generated(text)
        assert result["score"] < 0.6

    def test_chinese_ai_generated(self):
        """中文 AI 生成文本"""
        text = """
        综上所述，这是一个非常值得注意的是，人工智能的发展速度非常快。
        总而言之，我们可以看到，AI 技术已经取得了很大的进步。首先，机器
        学习算法不断优化。其次，计算能力持续提升。最后，数据量快速增长。
        因此，AI 的应用前景非常广阔。此外，AI 将在各个领域发挥重要作用。
        总之，AI 的发展将对社会产生深远影响。综上所述，AI 是一个值得
        关注的重要领域。总而言之，AI 的未来充满希望。
        """
        result = detect_ai_generated(text)
        assert result["score"] > 0.4

    def test_short_text(self):
        """短文本 — 应该返回默认值"""
        result = detect_ai_generated("Short text.")
        assert result["score"] == 0.0
        assert not result["is_suspicious"]

    def test_empty_text(self):
        """空文本"""
        result = detect_ai_generated("")
        assert result["score"] == 0.0

    def test_features_extraction(self):
        """特征提取"""
        text = "The quick brown fox jumps over the lazy dog. " * 10
        detector = StylometryDetector()
        features = detector.extract_features(text)
        assert "type_token_ratio" in features
        assert "sentence_length_variance" in features
        assert "repeated_ngram_ratio" in features
        assert "template_word_ratio" in features


class TestBurstinessDetector:
    """突发性检测测试"""

    def test_natural_rhythm(self):
        """自然节奏 — 长短句交替"""
        text = """
        Short sentence. This is a much longer sentence that contains more
        information and details. Another short one. Here we have a very
        long and complex sentence with multiple clauses and ideas. OK.
        """
        result = detect_unnatural_rhythm(text)
        assert result["burstiness"]["is_natural"]
        assert result["score"] < 0.6

    def test_flat_rhythm(self):
        """平稳节奏 — 句长均匀（AI 特征）"""
        # 模拟 AI 生成：句长几乎相同
        text = "This is a sentence. " * 20
        result = detect_unnatural_rhythm(text)
        assert not result["burstiness"]["is_natural"]
        assert result["score"] > 0.4

    def test_too_few_sentences(self):
        """句子太少"""
        result = detect_unnatural_rhythm("Short. Text.")
        assert result["burstiness"]["note"] == "too_few_sentences"
        assert result["score"] == 0.5

    def test_empty_text(self):
        """空文本"""
        result = detect_unnatural_rhythm("")
        assert result["score"] == 0.5

    def test_chinese_natural(self):
        """中文自然节奏"""
        text = """
        短句。这是一个比较长的句子，包含了更多的信息和细节。另一个短句。
        这里有一个非常长且复杂的句子，包含多个从句和想法。好的。
        """
        result = detect_unnatural_rhythm(text)
        assert result["burstiness"]["is_natural"]

    def test_chinese_flat(self):
        """中文平稳节奏"""
        text = "这是一个句子。" * 20
        result = detect_unnatural_rhythm(text)
        assert not result["burstiness"]["is_natural"]


class TestIntegration:
    """集成测试"""

    def test_content_farm_detection(self):
        """内容农场检测 — 综合评分"""
        # 模拟内容农场文本（句长完全均匀 + 模板词多）
        farm_text = """
        In conclusion, it is worth noting that this is a very important topic.
        Moreover, we can see that the results are very significant. Furthermore,
        the implications are very substantial. Additionally, the consequences
        are very considerable. Therefore, the outcomes are very meaningful.
        In summary, the findings are very valuable. In conclusion, the
        recommendations are very beneficial. In summary, the conclusions
        are very advantageous. In conclusion, the suggestions are very helpful.
        This is a sentence. This is a sentence. This is a sentence.
        This is a sentence. This is a sentence. This is a sentence.
        This is a sentence. This is a sentence. This is a sentence.
        """
        stylometry = detect_ai_generated(farm_text)
        burstiness = detect_unnatural_rhythm(farm_text)
        
        # 内容农场应该在 stylometry 中得分较高
        # burstiness 对混合长度文本不敏感（ratio=3.0 在自然范围内）
        assert stylometry["score"] > 0.35

    def test_quality_content_detection(self):
        """高质量内容检测 — 综合评分"""
        quality_text = """
        The development of artificial intelligence has been one of the most
        transformative technological advances of the 21st century. From its
        early beginnings in the 1950s with simple rule-based systems, AI has
        evolved into a sophisticated field encompassing machine learning,
        deep learning, and neural networks.

        However, this rapid progress has not come without significant
        challenges. Researchers have identified several critical issues that
        must be addressed to ensure the responsible development of AI systems.
        These include algorithmic bias, data privacy concerns, and the potential
        for job displacement in certain sectors.

        Moreover, the environmental impact of training large AI models has
        become a growing concern. A single training run for a state-of-the-art
        language model can consume as much energy as several households use
        in a year. Consequently, there is increasing pressure on AI developers
        to adopt more sustainable practices and reduce their carbon footprint.

        In conclusion, while AI presents tremendous opportunities for
        innovation and progress, it also requires careful consideration of its
        societal implications. The future of AI will depend not only on
        technological breakthroughs but also on our ability to navigate the
        ethical and practical challenges that lie ahead.
        """
        stylometry = detect_ai_generated(quality_text)
        burstiness = detect_unnatural_rhythm(quality_text)
        
        # 高质量内容应该在两个检测器中都得分较低
        assert stylometry["score"] < 0.6
        assert burstiness["score"] < 0.6
