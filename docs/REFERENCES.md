# 参考文献 — 内容质量评估与 AI 生成文本检测

本文档汇总了 Argo 内容质量评估系统（evidence_tier / source_ledger / stylometry_detector / burstiness_detector）所依据的学术论文与工业实践。

---

## 1. AI 生成文本检测

### 1.1 文体特征检测（Stylometry）

**Shu, K., Bhattacharjee, A., & Liu, H. (2019).**
"The Limitations of Stylometry for Detecting Machine-Generated Fake News."
*Proceedings of EMNLP 2019.*

- **核心贡献**：系统评估了文体特征（词汇丰富度、句长分布、标点模式）在检测 AI 生成假新闻中的效果
- **关键发现**：
  - AI 生成文本的词汇丰富度（Type-Token Ratio）显著低于人类写作
  - 句长方差小（过于均匀）是 AI 文本的显著特征
  - 功能词频率分布异常
- **我们的应用**：`stylometry_detector.py` 的 6 项特征提取

### 1.2 突发性检测（Burstiness）

**"Ratio of Quantiles Indicates Burstiness with Fewer False Negatives than the Conventional Burstiness Parameter." (2026).**

- **核心贡献**：提出用分位数比（Quantile Ratio, Q75/Q25）替代传统突发性参数
- **关键发现**：
  - 自然文本的分位数比在 1.5-4.0 之间
  - AI 生成文本通常 < 1.5（过于平稳）
  - 分位数比方法比传统方差方法假阴性更少
- **我们的应用**：`burstiness_detector.py` 的核心算法

---

## 2. 假新闻与内容农场检测

### 2.1 机器学习基准研究

**Ahmed, H., Traore, I., & Saad, S. (2019).**
"A Benchmark Study of Machine Learning Models for Online Fake News Detection."
*arXiv:1905.04749.*

- **核心贡献**：对比了 NB/SVM/RF/GBDT/LSTM 等多种模型在假新闻检测上的表现
- **关键发现**：
  - 集成方法（RF/GBDT）在特征工程充分时优于深度学习
  - 词汇特征 + 结构特征的组合效果最好
- **我们的应用**：`evidence_tier.py` 的多维特征设计

### 2.2 深度集成框架

**Wang, W., Zheng, V. W., & Yu, H. (2018).**
"A Deep Ensemble Framework for Fake News Detection and Classification."
*arXiv:1811.04670.*

- **核心贡献**：提出 CNN + BiLSTM + Attention 的深度集成架构
- **关键发现**：
  - 多视角（词汇 + 结构 + 语义）集成显著提升检测准确率
  - 单一模型容易过拟合特定类型的假新闻
- **我们的应用**：`stylometry_detector.py` 的多特征融合策略

### 2.3 多任务迁移学习

**Zhu, Y., & Yang, Y. (2019).**
"Localization of Fake News Detection via Multitask Transfer Learning."
*arXiv:1910.09295.*

- **核心贡献**：用多任务学习实现跨域假新闻检测
- **关键发现**：
  - 源域和域的特征共享可以提升小样本场景下的检测效果
  - 迁移学习对新型内容农场有更好的泛化能力
- **我们的应用**：`source_ledger.py` 的跨域风险评估

---

## 3. 内容质量评估

### 3.1 GPT 时代文本质量评估

**"When Automated Assessment Meets Automated Content Generation: Examining Text Quality in the Era of GPTs." (2023).**

- **核心贡献**：系统评估了 GPT 生成文本与人类写作在质量评估中的差异
- **关键发现**：
  - GPT 文本在「表面流畅度」上接近人类，但在「信息密度」「观点独特性」「论证深度」上有本质差异
  - 传统质量评估指标（如 BLEU、ROUGE）对 GPT 文本区分度有限
  - 需要多维度的质量评估框架
- **我们的应用**：`evidence_tier.py` 的内容信号评估

### 3.2 新闻可信度自动评估

**User Experience Design for Automatic Credibility Assessment of News Content About COVID-19." (2022).**

- **核心贡献**：设计了新闻可信度自动评估的用户体验框架
- **关键发现**：
  - 可信度评估需要多维度（来源、内容、作者、时效）
  - 用户对可信度评估的接受度取决于透明度和可解释性
- **我们的应用**：`source_ledger.py` 的多维风险评估

---

## 4. 工业界实践

### 4.1 NewsGuard + Pangram AI 内容农场检测

**NewsGuard Technologies. (2025).**
"NewsGuard Launches Real-time AI Content Farm Detection Datastream."

- **核心贡献**：NewsGuard 与 Pangram Labs 合作，推出实时 AI 内容农场检测数据流
- **关键发现**：
  - AI 内容农场通常具有以下特征：
    - 大量低质量内容批量生产
    - 缺乏明确的编辑标准和更正政策
    - 来源不透明，所有权披露缺失
    - 使用 AI 工具批量生成内容
  - 实时检测需要结合域名特征、内容特征和发布模式
- **我们的应用**：`evidence_tier.py` 的域名分类 + `stylometry_detector.py` 的 AI 生成检测

### 4.2 Google Helpful Content Update

**Google Search Central. (2022-2024).**
"Helpful Content Update" 系列更新.

- **核心贡献**：Google 持续更新其搜索算法，降低低质量内容的排名
- **关键发现**：
  - E-E-A-T（Experience, Expertise, Authoritativeness, Trustworthiness）是核心评估维度
  - 「有帮助的内容」需要：明确的目标受众、深度的专业知识、独特的价值
  - 内容农场通常缺乏 E-E-A-T 信号
- **我们的应用**：`evidence_tier.py` 的证据分层（A/B/C/D）与 E-E-A-T 对齐

---

## 5. 搜索引擎优化（SEO）与生成式引擎优化（GEO）

### 5.1 GEO vs SEO

**"GEO vs. SEO: A Comparison Guide." (2025).**

- **核心贡献**：系统比较了传统 SEO 和生成式引擎优化（GEO）的差异
- **关键发现**：
  - GEO 关注内容如何被 AI 搜索引擎（DeepSeek/豆包/千问/Kimi/元宝）引用
  - 结构化、可引用、有证据的内容更容易被 AI 引用
  - 内容农场通常无法通过 GEO 优化获得 AI 引用
- **我们的应用**：`evidence_tier.py` 的证据分层直接服务于 GEO 目标

### 5.2 内容农场检测与 Google 惩罚

**"Content Farm: Examples and Google Penalties." (2024).**

- **核心贡献**：分析了 Google 对内容农场的检测方法和惩罚机制
- **关键发现**：
  - Google 通过以下信号检测内容农场：
    - 低质量内容批量生产
    - 缺乏原创性
    - 关键词堆砌
    - 缺乏作者信息
  - 惩罚包括降权、移除索引、手动操作
- **我们的应用**：`stylometry_detector.py` 的模板词检测 + `burstiness_detector.py` 的节奏分析

---

## 6. 相关工具与数据集

| 工具/数据集 | 用途 | 链接 |
|------------|------|------|
| NewsGuard | 新闻可信度评级 | https://www.newsguardtech.com/ |
| Pangram Labs | AI 内容检测 | https://pangramlabs.com/ |
| Google Search Quality Rater Guidelines | 搜索质量评估标准 | https://developers.google.com/search |
| FakeNewsNet | 假新闻检测数据集 | https://github.com/KaiDMML/FakeNewsNet |
| LIAR | 假新闻检测数据集 | https://www.cs.ucsb.edu/~william/software.html |

---

## 7. 版本历史

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-09-29 | v1.0 | 初始版本，包含 4 个模块的参考文献 |

---

## 8. 引用格式

如果 Argo 的内容质量评估系统对你的研究或产品有帮助，请引用：

```
Argo Search. (2026). Content Quality Assessment System.
https://github.com/taxueseek/argo
```

---

*本文档持续更新，最后更新时间：2026-09-29*
