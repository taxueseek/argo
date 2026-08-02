# 数据源缺口评估与归档（2026-08-02）

本文件归档数据源缺口分析结论，供后续按投入产出比分批吸纳。已接入的源不重复列出，
只记录「缺口 → 候选 → 结论」以及中文垂直源的特殊情况。

## 一、缺口领域与候选数据源总表

| 缺口领域 | 推荐数据源 | 接入成本 | 备注 |
|---------|-----------|---------|------|
| 化学/药学结构检索 | PubChem PUG REST、ChEMBL | 零 cost，JSON 原生 | 化学结构检索是通用搜索死角 |
| 蛋白质/基因组 | UniProt、RCSB PDB | 零 cost | 23 万实验结构 + 100 万 AlphaFold 模型 |
| 医学术语统一 | UMLS/UTS | 需账号 | 200+ 术语表统一到 CUI |
| 法律判例 | CourtListener | 免费但 2026-05 后限额骤降 | 5 次/分、125 次/天；美国判例全文唯一免费路径 |
| 欧盟法律 | EUR-Lex CELLAR | 零 cost | 270 万文档 RDF |
| 专利全文/权利要求 | Espacenet OPS、Lens、USPTO PatentsView | OPS 需免费 key | Lens 1.4 亿专利 |
| 经济时间序列 | FRED、World Bank | 已接入（fred/worldbank） | FRED 正吞并 Fed 官方 DDP，是美国数据唯一 API 出口 |
| 地球科学/遥感 | NASA CMR、USGS、NOAA/NWS | 零 cost | 地震/水文/高程/天气 |
| 社科微观数据 | ICPSR | 部分受限 | 25 万+ 文件、1.6 万研究 |
| 政府开放数据 | data.gov、data.europa.eu | 零 cost | 37 万+/200 万+ 数据集 |
| 公版书籍语料 | Project Gutenberg、Internet Archive Wayback CDX | 零 cost | 7.5 万+ 公版书；CDX 可检索已删除内容 |
| 全网正文语料 | Common Crawl | 零 cost 但量大 | 单月 27 亿页/377 TiB |
| 中文社科全文 | NSSD | 无正式公开 API | 2000+ 期刊、930 万+ 篇 |
| 中文统计数据 | 国家统计局 data.stats.gov.cn | 无正式公开 API | easyquery POST 为社区方案，稳定性待验证 |
| 地理实体解析 | GeoNames | 零 cost | 1100 万+ 地名 |
| 音乐元数据 | MusicBrainz | 已在 Argo | 仅作搜索源，未做实体消歧层 |
| 通用知识图谱 | Wikidata SPARQL | 已在 Argo | 未做图查询增强 |

## 二、优先补齐 Top 5（按投入产出比）

1. **PubChem PUG REST + ChEMBL** —— 零 cost、JSON 原生、化学结构检索是通用搜索死角，5 分钟可集成。
2. **ClinicalTrials.gov v2** —— 已在引擎列表（clinicaltrials），未在 SKILL.md 垂直分类中显式标注为医疗入口，建议在路由规则中强化。
3. **FRED + World Bank** —— 经济事实核查黄金组合，已接入；FRED 正在吞并 Fed 官方 DDP。
4. **Common Crawl + Wayback CDX** —— 全网语料与已删除内容检索，回报率最高的「搜索增强」投资。
5. **CourtListener** —— 注意 2026-05 限额骤降（5 次/分、125 次/天），仍是美国判例全文检索唯一免费路径。

## 三、中文垂直源现实情况

中文区没有「小而美、免费、有正式 API」的垂直源。两个最可行入口：

- **NSSD**（社科全文）：无正式公开 API 文档，接入需逆向或爬取，风险与维护成本高。
- **国家统计局 easyquery**（宏观数据）：社区广泛使用但未文档化的 POST 接口，稳定性需自行验证。

结论：两源暂不接入，归档待定。优先把已有源的查询质量做扎实（如 worldbank 国家词分流），
而非堆砌不稳定的中文源。

## 四、已实施改动

- **worldbank 国家词分流**（ddf60f1 后续提交）：`is_foreign_macro_query` 识别非美国国家词，
  fred 引擎对「中国GDP」「日本通胀」类查询返回空，route 层将 worldbank 前置，
  杜绝「搜中国给出来美国数据」的错误（此前 fred 按关键词匹配 series_id 直接返回美国序列，
  且 fast 模式 early-stop 会短路 worldbank 出场机会）。
- 修复文件：`scripts/engines_builders_data.py`、`scripts/route.py`。

## 五、未接入源待办（按优先级）

- [x] PubChem PUG REST（chem 域，30d21da 后接入）
- [x] ChEMBL 活性数据（chem 域，并入 pubchem 引擎兜底）
- [x] Eurostat SDMX（macro_data 域，EU 国家 GDP/人均GDP/失业率/人口）
- [x] GBIF 物种检索（species 域，学名/英文俗名）
- [x] RFC Editor / IETF Datatracker（rfc_search 域）
- [ ] GDELT 2.0（news 域）—— 持续 429 限流（本机 IP 两次实测均被拒），暂缓
- [ ] Common Crawl 索引 + Wayback CDX（通用增强）
- [ ] CourtListener（legal 域，注意配额）
- [ ] GeoNames 地理消歧（geo 层增强）
- [ ] MusicBrainz 实体消歧层（music 域）
- [ ] Wikidata SPARQL 图查询增强
- [ ] ClinicalTrials.gov 路由强化（medical 域）

## 六、新增引擎实测结论（2026-08-02）

- **PubChem**：名称查询走 `compound/name/{q}/cids` + `property`（分子式/分子量/IUPAC/SMILES）；
  `compound/formula` 端点是异步等待模式（返回 Waiting + ListKey），不做轮询，分子式查询由
  ChEMBL 兜底（C9H8O4 → ASPIRIN/CAFFEIC ACID）。ChEMBL 需 `search.json` 后缀或
  `Accept: application/json`，否则返回 XML。
- **Eurostat**：SDMX 2.1 免认证；已验证维度 GDP(nama_10_gdp/B1GQ/CP_MEUR)、
  人均GDP(nama_10_pc/CP_EUR_HAB)、失业率(une_rt_a/Y15-74/PC_ACT)、人口(demo_pjan/age=TOTAL)。
  HICP 数据集（prc_hicp_manr）400 未用，通胀由 worldbank 覆盖。unit 维度 label 的 key 是编码非索引。
- **GBIF**：`/v1/species/search?q=` 对拉丁学名准确，对中文俗名返回无关属种
  （「大熊猫」→ 植物属 Panda 等），引擎加 ASCII 守卫：纯中文查询直接放弃，交给百科类引擎。
- **RFC**：datatracker `name__icontains` 只匹配文档名（rfc9000 名里无 quic），
  必须用 `title__icontains` + `type__slug=rfc`；`type=rfc` 直接过滤无效（type 是 URI）。
