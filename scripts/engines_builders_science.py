#!/usr/bin/env python3
"""专用构建器：科研与公共数据库（化学、生物、医药、预印本、地震、地球科学）

自 engines_builders_data.py 按主题拆出（2026-11-08）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cjk_tokens import cjk_term_grams
from engines_base import (
    rank_score,
    safe_search, http_open,
)

logger = logging.getLogger("unified_search.engines")

# ── ClinicalTrials.gov v2 ────────────────────────────────────────────────────

def _build_clinicaltrials_engine(spec: dict[str, Any]) -> Any:
    """ClinicalTrials.gov API v2 试验检索（免认证）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        q = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]*", query))
        if not re.search(r"[A-Za-z]{2,}", q):
            return []
        to = _timeout or timeout
        url = "https://clinicaltrials.gov/api/v2/studies?" + up.urlencode({
            "query.term": q, "pageSize": min(n, 20), "format": "json"})
        try:
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.5", "Accept": "application/json"}), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"clinicaltrials 失败: {e}")
            return []
        results = []
        for _rk5, st in enumerate((data.get("studies") or [])[:n]):
            ps = st.get("protocolSection") or {}
            ident = ps.get("identificationModule") or {}
            desc = ps.get("descriptionModule") or {}
            status = (ps.get("statusModule") or {}).get("overallStatus") or ""
            nct = ident.get("nctId") or ""
            title = ident.get("briefTitle") or ident.get("officialTitle") or nct
            summary = desc.get("briefSummary") or status
            if not title and not nct:
                continue
            results.append({
                "title": str(title)[:200],
                "url": f"https://clinicaltrials.gov/study/{nct}" if nct else "",
                "snippet": str(summary).replace("\n", " ")[:300],
                "source": "clinicaltrials",
                "score": rank_score(0.85, _rk5),
            })
        return results
    return _engine


# ── openFDA 药品标签 ─────────────────────────────────────────────────────────

def _build_openfda_engine(spec: dict[str, Any]) -> Any:
    """openFDA drug label 搜索（免认证）。"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        # openFDA Lucene 语法与拉丁索引守卫：提取英文药名并剥离 CJK 与问号/冒号
        q = re.sub(r"(?i)\b(drug|medicine|label|openfda|fda)\b", " ", query)
        q = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]*", q))
        if not re.search(r"[A-Za-z]{2,}", q):
            return []
        search = f'openfda.brand_name:"{q}" openfda.generic_name:"{q}"'
        url = "https://api.fda.gov/drug/label.json?" + up.urlencode({
            "search": search, "limit": min(n, 20)})
        try:
            with http_open(urllib.request.Request(
                    url, headers={"User-Agent": "argo-search/2.5", "Accept": "application/json"}), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            # 回退：全文检索
            try:
                url2 = "https://api.fda.gov/drug/label.json?" + up.urlencode({
                    "search": q, "limit": min(n, 20),
                })
                with http_open(urllib.request.Request(
                        url2, headers={"User-Agent": "argo-search/2.5"}), timeout=to, engine=spec.get("_name", "")) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as e2:
                logger.warning(f"openfda 失败: {e2}")
                return []
        results = []
        for _rk6, item in enumerate((data.get("results") or [])[:n]):
            of = item.get("openfda") or {}
            brands = of.get("brand_name") or []
            generics = of.get("generic_name") or []
            title = (brands[0] if brands else "") or (generics[0] if generics else "") or item.get("id", "drug label")
            purpose = item.get("purpose") or item.get("indications_and_usage") or []
            if isinstance(purpose, list):
                purpose = purpose[0] if purpose else ""
            set_id = (of.get("spl_set_id") or [None])[0] if isinstance(of.get("spl_set_id"), list) else of.get("spl_set_id")
            url_out = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}" if set_id else "https://open.fda.gov/apis/drug/label/"
            results.append({
                "title": str(title)[:200],
                "url": url_out,
                "snippet": str(purpose).replace("\n", " ")[:300],
                "source": "openfda",
                "score": rank_score(0.8, _rk6),
            })
        return results
    return _engine


# ── PubChem + ChEMBL 化学引擎（chem 域） ────────────────────────────────────

_PUBCHEM_API = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
_CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"
_RE_FORMULA = re.compile(r"^[A-Z][a-z]?\d*([A-Z][a-z]?\d*)*$")
# 检索噪声词：剥掉后用核心化合物名打 API（「阿司匹林分子式」→「阿司匹林」）
_CHEM_NOISE_RE = re.compile(
    r"(?i)\b(?:molecular\s+weight|chemical\s+formula|cas\s*number|iupac|smiles|"
    r"formula|weight|compound|molecule|mw)\b|"
    r"分子式|分子量|摩尔质量|化学式|结构式|化合物|药物|CAS号?"
)
# 常用中文药名 → PubChem 英文名（中文直查常 miss，ChEMBL 会乱返回无关分子）
_CHEM_CN_ALIASES: dict[str, str] = {
    "阿司匹林": "aspirin",
    "乙酰水杨酸": "aspirin",
    "布洛芬": "ibuprofen",
    "对乙酰氨基酚": "acetaminophen",
    "扑热息痛": "acetaminophen",
    "咖啡因": "caffeine",
    "葡萄糖": "glucose",
    "乙醇": "ethanol",
    "甲醇": "methanol",
    "胆固醇": "cholesterol",
    "青霉素": "penicillin",
    "阿莫西林": "amoxicillin",
    "二甲双胍": "metformin",
    "奥美拉唑": "omeprazole",
    "硝苯地平": "nifedipine",
    "氨氯地平": "amlodipine",
    "阿托伐他汀": "atorvastatin",
    "氯吡格雷": "clopidogrel",
    "华法林": "warfarin",
    "胰岛素": "insulin",
    "吗啡": "morphine",
    "可待因": "codeine",
    "尼古丁": "nicotine",
    "维生素c": "ascorbic acid",
    "维生素C": "ascorbic acid",
    "抗坏血酸": "ascorbic acid",
}
_CHEM_TOKEN_NOISE = frozenset({
    "分子量", "分子式", "化学", "化学式", "结构式", "化合物", "药物",
    "weight", "formula", "molecular", "chemical", "compound", "molecule",
    "cid", "chembl", "smiles", "iupac", "cas", "mw", "acid", "hydrochloride",
})


def _strip_chem_noise(query: str) -> str:
    """去掉「分子式/分子量」等修饰，保留化合物核心名。"""
    cleaned = _CHEM_NOISE_RE.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;|/")
    return cleaned or query.strip()


def _chem_lookup_names(query: str) -> list[str]:
    """生成 PubChem/ChEMBL 查询候选：核心名 + 中文别名英文化。"""
    raw = query.strip()
    core = _strip_chem_noise(raw)
    names: list[str] = []
    for candidate in (core, raw):
        if candidate and candidate not in names:
            names.append(candidate)
    # 中文别名：核心或原文含中文药名时追加英文
    hay = f"{core} {raw}"
    for cn, en in _CHEM_CN_ALIASES.items():
        if cn in hay and en not in names:
            names.append(en)
    return names


def _chem_tokens(text: str) -> set[str]:
    """化合物相关 token：英文词 + 连续中文 + 数字。"""
    if not text:
        return set()
    low = text.lower()
    tokens = set(re.findall(r"[a-z][a-z0-9\-]{1,}", low))
    tokens.update(cjk_term_grams(text))
    tokens.update(re.findall(r"\d{2,}", text))
    return {t for t in tokens if t not in _CHEM_TOKEN_NOISE and len(t) >= 2}


def _chem_result_overlaps(query: str, title: str, snippet: str = "",
                          pref_name: str = "") -> bool:
    """结果与查询是否相关：token 交集或核心名子串命中。

    无重叠则视为假阳性（ChEMBL 对中文常返回无关分子）。
    """
    q_tokens = _chem_tokens(query)
    core = _strip_chem_noise(query).lower()
    if core:
        q_tokens |= _chem_tokens(core)
    for cn, en in _CHEM_CN_ALIASES.items():
        if cn in query or cn in core:
            q_tokens.add(en.lower())
            for part in en.lower().split():
                if len(part) >= 2:
                    q_tokens.add(part)
    doc = _chem_tokens(f"{title} {snippet} {pref_name}")
    if not q_tokens:
        return True  # 无法判定时不误杀
    if q_tokens & doc:
        return True
    hay = f"{title} {snippet} {pref_name}".lower()
    if core and len(core) >= 2 and core in hay:
        return True
    # 英文别名整词
    for cn, en in _CHEM_CN_ALIASES.items():
        if cn in query and en.lower() in hay:
            return True
    return False


def _build_pubchem_engine(spec: dict[str, Any]) -> Any:
    """化学/药学化合物检索：PubChem PUG REST 主路径 + ChEMBL 保底。

    支持化合物名、分子式、CAS 号、IUPAC 名查询，返回分子式/分子量/IUPAC/SMILES。
    质量闸门：ChEMBL 结果须与查询 token 重叠，否则丢弃当 no-results（宁空勿假）。
    """
    timeout = spec.get("timeout", 10)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    def _jget(url: str, to: float) -> dict:
        req = urllib.request.Request(url, headers=_HEADERS)
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        results: list[dict[str, Any]] = []
        lookup_names = _chem_lookup_names(q)
        display_name = _strip_chem_noise(q) or q

        # 1. PubChem 主路径：多候选名 → CID → 属性（formula 端点异步，不在此轮询）
        cid = ""
        hit_name = display_name
        for name in lookup_names:
            try:
                d = _jget(
                    f"{_PUBCHEM_API}/compound/name/{urllib.parse.quote(name)}/cids/JSON",
                    to,
                )
                cids = (d.get("IdentifierList") or {}).get("CID") or []
                if cids:
                    cid = str(cids[0])
                    hit_name = name
                    break
            except Exception:
                continue
        if cid:
            try:
                d = _jget(
                    f"{_PUBCHEM_API}/compound/cid/{cid}/property/"
                    f"MolecularFormula,MolecularWeight,IUPACName,CanonicalSMILES/JSON", to)
                props = ((d.get("PropertyTable") or {}).get("Properties") or [{}])[0]
                formula = props.get("MolecularFormula", "")
                mw = props.get("MolecularWeight", "")
                iupac = props.get("IUPACName", "") or ""
                smiles = props.get("CanonicalSMILES", "") or ""
                title = f"{display_name} (PubChem CID {cid})"
                if hit_name.lower() != display_name.lower():
                    title = f"{display_name} / {hit_name} (PubChem CID {cid})"
                if formula:
                    title += f" 分子式 {formula}"
                if mw:
                    title += f" 分子量 {mw}"
                snippet = f"CID {cid}"
                if iupac:
                    snippet += f" | IUPAC: {iupac}"
                if smiles:
                    snippet += f" | SMILES: {smiles}"
                results.append({
                    "title": title,
                    "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    "snippet": snippet[:400],
                    "source": "pubchem",
                    "score": 0.95,
                })
            except Exception as e:
                logger.warning(f"PubChem 属性失败: {e}")

        # 2. ChEMBL 保底：仅保留与查询重叠的结果（无重叠 = 空，禁止交差评）
        if not results:
            seen_ids: set[str] = set()
            for name in lookup_names:
                try:
                    d = _jget(
                        f"{_CHEMBL_API}/molecule/search.json?"
                        f"q={urllib.parse.quote(name)}&limit={min(n, 5)}",
                        to,
                    )
                except Exception as e:
                    logger.warning(f"ChEMBL 失败 ({name}): {e}")
                    continue
                for m in (d.get("molecules") or [])[:n]:
                    props = m.get("molecule_properties") or {}
                    chembl_id = m.get("molecule_chembl_id", "")
                    if chembl_id in seen_ids:
                        continue
                    pref = (m.get("pref_name") or "") or ""
                    title_base = pref or chembl_id or name
                    title = title_base
                    if props.get("mw_freebase"):
                        title += f" 分子量 {props['mw_freebase']}"
                    parts = [f"ChEMBL {chembl_id}"]
                    if props.get("full_molformula"):
                        parts.append(f"分子式 {props['full_molformula']}")
                    if props.get("canonical_smiles"):
                        parts.append(f"SMILES {props['canonical_smiles'][:100]}")
                    snippet = " | ".join(parts)[:400]
                    # 质量闸门：与原查询或当前 lookup 名无重叠则丢弃
                    if not (
                        _chem_result_overlaps(q, title, snippet, pref)
                        or _chem_result_overlaps(name, title, snippet, pref)
                    ):
                        continue
                    seen_ids.add(chembl_id)
                    results.append({
                        "title": title,
                        "url": (f"https://www.ebi.ac.uk/chembl/explore/compound/{chembl_id}"
                                if chembl_id else "https://www.ebi.ac.uk/chembl/"),
                        "snippet": snippet,
                        "source": "chembl",
                        "score": rank_score(0.9, len(results)),
                    })
                if results:
                    break
            if not results:
                logger.info("ChEMBL 结果与查询无重叠，返回空（宁空勿假）")
        return results[: max(n, 3)]

    return _engine


# ── GBIF 生物多样性引擎（species 域） ────────────────────────────────────────

_GBIF_API = "https://api.gbif.org/v1"
_GBIF_RANK_ORDER = {"SPECIES": 3, "SUBSPECIES": 2, "GENUS": 1}


def _build_gbif_engine(spec: dict[str, Any]) -> Any:
    """GBIF 物种检索（api.gbif.org/v1/species/search，免认证）。

    学名/俗名搜索，优先学名包含查询词的 ACCEPTED 物种条目。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)"}

    def _jget(url: str, to: float) -> dict:
        req = urllib.request.Request(url, headers=_HEADERS)
        with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        # 剥离中文查询噪声词（学名/物种/俗名等）；GBIF 只对拉丁学名/英文俗名可靠，
        # 纯中文俗名（如「大熊猫」）搜出来的是无关属种，直接放弃
        q = re.sub(r"(学名|物种|俗名|拉丁名|生物|分类|是什么|有哪些|查询|搜索)", "", q).strip()
        if not re.search(r"[A-Za-z]{2,}", q):
            return []
        to = _timeout or timeout
        ql = q.lower()
        url = f"{_GBIF_API}/species/search?q={urllib.parse.quote(q)}&limit={min(n * 3, 20)}"
        try:
            d = _jget(url, to)
        except Exception as e:
            logger.warning(f"GBIF 失败: {e}")
            return []

        def _key(r: dict) -> tuple:
            sn = (r.get("scientificName") or "").lower()
            name_hit = ql in sn  # 学名包含查询词优先（俗名匹配的噪声大）
            status_ok = (r.get("taxonomicStatus") or "") == "ACCEPTED"
            rank = _GBIF_RANK_ORDER.get(r.get("rank"), 0)
            return (name_hit, status_ok, rank)

        items = sorted((r for r in (d.get("results") or []) if r.get("scientificName")), key=_key, reverse=True)
        results = []
        for _rk9, r in enumerate(items[:n]):
            sci = r.get("scientificName", "")
            rank = r.get("rank", "")
            kingdom = r.get("kingdom") or r.get("kingdomKey") or ""
            status = r.get("taxonomicStatus") or ""
            key = r.get("nubKey") or r.get("key")
            title = f"{sci} ({rank})"
            if status == "ACCEPTED":
                title = f"{sci}（有效名）"
            snip = f"界 {kingdom} | 分类 {rank}"
            if status:
                snip += f" | 状态 {status}"
            if r.get("vernacularName"):
                snip += f" | 俗名 {r['vernacularName']}"
            results.append({
                "title": title,
                "url": f"https://www.gbif.org/species/{key}" if key else "https://www.gbif.org/",
                "snippet": snip,
                "source": "gbif",
                "score": rank_score(0.92, _rk9),
            })
        return results[: max(n, 3)]

    return _engine


# ── UniProt 蛋白质/基因组引擎 ────────────────────────────────────────────────

_UNIPROT_API = "https://rest.uniprot.org/uniprotkb/search"

# 高频蛋白中文名 → 英文检索词（UniProt 不索引中文）
_UNIPROT_CN = {
    "胰岛素": "insulin", "血红蛋白": "hemoglobin", "肌红蛋白": "myoglobin",
    "白蛋白": "albumin", "胶原蛋白": "collagen", "角蛋白": "keratin",
    "胰蛋白酶": "trypsin", "溶菌酶": "lysozyme", "细胞色素c": "cytochrome c",
    "细胞色素": "cytochrome", "泛素": "ubiquitin", "免疫球蛋白": "immunoglobulin",
    "抗体": "antibody", "转铁蛋白": "transferrin", "纤维蛋白原": "fibrinogen",
    "表皮生长因子受体": "EGFR", "生长激素": "growth hormone",
    "血管内皮生长因子": "VEGF", "肿瘤坏死因子": "TNF", "白介素": "interleukin",
    "干扰素": "interferon", "淀粉酶": "amylase", "胃蛋白酶": "pepsin",
    "弹性蛋白": "elastin", "肌动蛋白": "actin", "肌球蛋白": "myosin",
    "微管蛋白": "tubulin", "组蛋白": "histone", "核糖体蛋白": "ribosomal protein",
    "谷胱甘肽过氧化物酶": "glutathione peroxidase", "超氧化物歧化酶": "superoxide dismutase",
    "载脂蛋白": "apolipoprotein", "脂蛋白": "lipoprotein", "受体蛋白": "receptor protein",
    "蛋白激酶": "protein kinase", "磷酸酶": "phosphatase", "转录因子": "transcription factor",
    "癌基因": "oncogene", "抑癌基因": "tumor suppressor", "p53蛋白": "p53",
}


def _ascii_tokens(q: str) -> str:
    """提取查询中的 ASCII 字母数字 token（UniProt/PDB 只索引英文）。"""
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\.\*]*", q)
    return " ".join(toks).strip()


def _build_uniprot_engine(spec: dict[str, Any]) -> Any:
    """UniProt 蛋白质检索（rest.uniprot.org/uniprotkb/search，免认证）。

    基因名/蛋白名/物种查询，返回 accession、推荐名、基因名与物种。
    中英混合查询只取 ASCII token（UniProt 不索引中文），纯中文查高频蛋白名映射表。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q_raw = query.strip()
        if not q_raw:
            return []
        q = _ascii_tokens(q_raw)
        if not q:
            q = _UNIPROT_CN.get(q_raw, "")
            if not q:
                return []
        to = _timeout or timeout
        url = f"{_UNIPROT_API}?" + urllib.parse.urlencode({
            "query": q, "format": "json", "size": min(n, 25),
        })
        try:
            with http_open(urllib.request.Request(url, headers=_HEADERS), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"UniProt 失败: {e}")
            return []
        results = []
        for _rk10, x in enumerate((data.get("results") or [])[:n]):
            acc = x.get("primaryAccession", "")
            pd = x.get("proteinDescription") or {}
            rn = ((pd.get("recommendedName") or {}).get("fullName") or {}).get("value", "")
            genes = x.get("genes") or []
            gene = ""
            if genes and isinstance(genes[0], dict):
                gene = ((genes[0].get("geneName") or {}).get("value", ""))
            org = ((x.get("organism") or {}).get("scientificName") or "")
            title = rn or (f"{acc}" if acc else q)
            snip = " · ".join(p for p in (org, gene) if p)
            results.append({
                "title": f"{title} ({acc})" if acc else title,
                "url": f"https://www.uniprot.org/uniprotkb/{acc}" if acc else "",
                "snippet": snip[:300],
                "source": "uniprot",
                "score": rank_score(0.9, _rk10),
            })
        return results
    return _engine


# ── RCSB PDB 蛋白质结构引擎 ──────────────────────────────────────────────────

_PDB_SEARCH_API = "https://search.rcsb.org/rcsbsearch/v2/query"


def _build_rcsb_pdb_engine(spec: dict[str, Any]) -> Any:
    """RCSB PDB 蛋白质结构检索（search.rcsb.org v2，免认证）。

    全文搜索返回 PDB ID 列表，链接到 rcsb.org/structure/{id}。
    """
    timeout = spec.get("timeout", 15)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)",
                "Accept": "application/json", "Content-Type": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = _ascii_tokens(query)
        if not q:
            return []
        to = _timeout or timeout
        body = json.dumps({
            "query": {"type": "terminal", "service": "full_text", "parameters": {"value": q}},
            "return_type": "entry",
            "request_options": {"return_all_hits": True},
        }).encode("utf-8")
        try:
            req = urllib.request.Request(_PDB_SEARCH_API, data=body, headers=_HEADERS, method="POST")
            with http_open(req, timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"RCSB PDB 失败: {e}")
            return []
        results = []
        for _rk11, x in enumerate((data.get("result_set") or [])[:n]):
            pid = x.get("identifier", "")
            if not pid:
                continue
            results.append({
                "title": f"PDB 结构 {pid}（{q}）",
                "url": f"https://www.rcsb.org/structure/{pid}",
                "snippet": f"RCSB PDB 实验结构 {pid}，分辨率 {x.get('score', '')}"[:300],
                "source": "rcsb_pdb",
                "score": rank_score(0.85, _rk11),
            })
        return results
    return _engine


# ── USGS 地震引擎 ────────────────────────────────────────────────────────────

_USGS_EQ_API = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# 中文地点词 → (place 匹配正则, bbox 地理框或 None)
# USGS place 是英文；美国国内震 place 用「城市, 州缩写」格式（"6 km NW of Pinnacles, CA"）。
# bbox 可让「台湾地震」这类查询不依赖全球 top-50，直接框选该地区全部地震。
_USGS_PLACE_CN = {
    "日本": ("japan", dict(minlatitude=24, maxlatitude=47, minlongitude=122, maxlongitude=147)),
    "台湾": ("taiwan", dict(minlatitude=20, maxlatitude=26, minlongitude=119, maxlongitude=124)),
    "加州": (r"california|, ca\b", dict(minlatitude=32, maxlatitude=42, minlongitude=-125, maxlongitude=-114)),
    "中国": ("china", None), "香港": ("hong kong", None),
    "美国": (r", (ca|nv|ak|or|wa|hi|az|ut|mt|id|nm|tx|ok|ar|mo|ky|tn|sc|nc|ga)\b", None),
    "阿拉斯加": ("alaska", dict(minlatitude=48, maxlatitude=72, minlongitude=-170, maxlongitude=-130)),
    "夏威夷": ("hawaii", None),
    "印尼": ("indonesia", None), "菲律宾": ("philippines", None), "新西兰": ("new zealand", None),
    "智利": ("chile", None), "秘鲁": ("peru", None), "墨西哥": ("mexico", None), "土耳其": ("turkey", None),
    "伊朗": ("iran", None), "印度": ("india", None), "尼泊尔": ("nepal", None), "意大利": ("italy", None),
    "希腊": ("greece", None), "冰岛": ("iceland", None), "阿富汗": ("afghanistan", None), "缅甸": ("myanmar", None),
    "巴布亚": ("papua", None), "所罗门": ("solomon", None), "汤加": ("tonga", None), "瓦努阿图": ("vanuatu", None),
    "斐济": ("fiji", None), "关岛": ("guam", None), "千岛群岛": ("kuril", None),
}


def _build_usgs_engine(spec: dict[str, Any]) -> Any:
    """USGS 地震检索（earthquake.usgs.gov FDSNWS，免认证）。

    返回最近 30 天 M2.5+ 地震；查询含地点词（如「日本」「加州」）时客户端侧
    按 place 字段过滤，命中地点相关地震。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = query.strip()
        if not q:
            return []
        to = _timeout or timeout
        # 提取地点词：去掉「地震/earthquake/震级」等噪声
        place_kw = re.sub(r"(?i)(地震|earthquake|seismic|震级|magnitude|quakes?|最近|最新)", " ", q)
        place_kw = re.sub(r"\s+", " ", place_kw).strip()
        params = {
            "format": "geojson", "limit": 50,
            "starttime": time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400)),
            "minmagnitude": "2.5",
        }
        if place_kw:
            kw = place_kw.lower()
            bbox = None
            # 中文地点词查映射表（正则 + 可选 bbox）；英文词直接按原文匹配 place
            if not re.search(r"[a-z]", kw):
                entry = _USGS_PLACE_CN.get(place_kw)
                if entry:
                    kw, bbox = entry
            if bbox:
                params.update(bbox)
                # bbox 精确框选时无需放大样本，直接按时间取
                params["limit"] = min(n * 3, 30)
            else:
                # 无 bbox 时按震级降序取样本，避免目标地点被全球密集地震挤出 top
                params["orderby"] = "magnitude"
        url = _USGS_EQ_API + "?" + urllib.parse.urlencode(params)
        try:
            with http_open(urllib.request.Request(url, headers=_HEADERS), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"USGS 失败: {e}")
            return []
        feats = data.get("features") or []
        if place_kw:
            if kw:
                feats = [f for f in feats if re.search(kw, str(f.get("properties", {}).get("place", "")).lower())]
        results = []
        for _rk15, f in enumerate(feats[:n]):
            p = f.get("properties", {})
            mag = p.get("mag")
            place = p.get("place", "")
            ts = (p.get("time") or 0) // 1000
            url_ = p.get("url", "")
            results.append({
                "title": f"M{mag} {place}",
                "url": url_ or "",
                "snippet": f"USGS 地震 · {ts and time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(ts))}"[:300],
                "source": "usgs",
                "score": rank_score(0.85, _rk15),
            })
        return results
    return _engine


# ── NASA CMR 地球科学数据目录引擎 ────────────────────────────────────────────

_CMR_API = "https://cmr.earthdata.nasa.gov/search/collections.json"


def _build_nasa_cmr_engine(spec: dict[str, Any]) -> Any:
    """NASA CMR 地球科学数据目录检索（cmr.earthdata.nasa.gov，免认证）。

    MODIS/卫星/遥感等关键词 → 数据集集合（含概念 ID，链接 Earthdata Search）。
    """
    timeout = spec.get("timeout", 12)
    _HEADERS = {"User-Agent": "argo-search/2.6 (unified-search@local)", "Accept": "application/json"}

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        q = _ascii_tokens(query)
        if not q:
            return []
        to = _timeout or timeout
        url = _CMR_API + "?" + urllib.parse.urlencode({"keyword": q, "page_size": min(n, 10)})
        try:
            with http_open(urllib.request.Request(url, headers=_HEADERS), timeout=to, engine=spec.get("_name", "")) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning(f"NASA CMR 失败: {e}")
            return []
        results = []
        for _rk16, x in enumerate(((data.get("feed") or {}).get("entry") or [])[:n]):
            title = x.get("title") or q
            cid = x.get("id") or ""
            summary = (x.get("summary") or "")[:200]
            results.append({
                "title": title,
                "url": f"https://search.earthdata.nasa.gov/search/granules?p={cid}" if cid else "",
                "snippet": summary[:300],
                "source": "nasa_cmr",
                "score": rank_score(0.8, _rk16),
            })
        return results
    return _engine


# ── bioRxiv / medRxiv 预印本引擎 ─────────────────────────────────────────────

# DOI 提取：10. 开头的标准 DOI（bioRxiv DOI 形态 10.1101/2024.01.02.123456）
_PREPRINT_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'，。；）(（]+", re.I)

# 时效模式的触发词：命中才拉「最近 3 天全量」。关键词查询一律短路返回空
# （API 无搜索端点，拉全量再本地过滤只产假阳性——2026-11-09 实测 65 KB/2.7 s
# 换 0 条有效结果，且泛词命中 7 条无关文献）。
_TIMELY_TRIGGERS = frozenset({"最新", "recent", "预印本", "预印"})


def _build_biorxiv_engine(spec: dict[str, Any]) -> Any:
    """bioRxiv / medRxiv 预印本引擎（API 免认证）。

    bioRxiv API 没有关键词搜索端点——只有两种查询语义是诚实的：
      ① DOI（10.xxxx/… 或 bioRxiv/medRxiv 链接）→ 取单篇详情（标题/作者/摘要）
      ② 「最新 / recent」或空 → 最近 3 天全量列表
    关键词查询诚实返回空（不伪造结果）；关键词发现交给 openalex /
    semantic_scholar，本引擎负责预印本原文入口与时效性列表。
    medRxiv 按 DOI 自动路由到 medrxiv 子库。
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        from datetime import date, timedelta
        to = _timeout or timeout
        from engines_base import _http_get_raw
        q = (query or "").strip()
        m = _PREPRINT_DOI_RE.search(q)
        headers = {"Accept": "application/json"}
        if m:
            doi = m.group(0).rstrip(".")
            server = "medrxiv" if ("medrxiv" in q.lower()) else "biorxiv"
            url = f"https://api.biorxiv.org/details/{server}/{doi}"
            raw = _http_get_raw(url, headers, to, engine="biorxiv")
            if not raw:
                return []
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return []
            coll = data.get("collection") or []
            items = coll
        else:
            # 时效模式：最近 3 天（含「最新/recent」触发词）。
            #
            # 2026-11-09 短路：关键词查询**不发请求**，直接返回空。
            # 理由（实测）：API 无关键词搜索端点，旧实现拉「最近 3 天全量」
            # （65 KB / 30 条 / 2.7 s）再本地按词过滤——而 mechanism、paper
            # 这类泛词会命中 7 条无关文献（匹配到 protein response mechanism
            # 之类），全是假阳性；深档实测该引擎 timeout=12s 拖累整批 9.5 s，
            # 最终结果条数与不调用它完全相同（5 条）。
            # 即：2.7-12 s 换 0 条有效结果。关键词发现本就由 openalex /
            # semantic_scholar 承担（见本函数文档串），本引擎只负责 DOI 与
            # 时效列表。短路后 deep 档从 10 s 降到 2.2 s（方案 A 的尾宽之上
            # 再省掉这次请求）。
            kws = [w for w in q.lower().split() if w not in _TIMELY_TRIGGERS]
            if kws:
                return []
            end = date.today()
            start = end - timedelta(days=3)
            url = (f"https://api.biorxiv.org/details/biorxiv/"
                   f"{start.isoformat()}/{end.isoformat()}/0")
            raw = _http_get_raw(url, headers, to, engine="biorxiv")
            if not raw:
                return []
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return []
            coll = data.get("collection") or []
            items = coll[:n]

        results = []
        for c in items:
            doi = str(c.get("doi") or "")
            results.append({
                "title": str(c.get("title") or "")[:120],
                "url": f"https://doi.org/{doi}" if doi else "https://www.biorxiv.org/",
                "snippet": f"{str(c.get('authors') or '')[:80]} | {str(c.get('abstract') or '')[:220]}",
                "source": "biorxiv",
                "score": 0.9 if m else 0.75,
                "published_at": str(c.get("date") or "")[:10],
            })
        return results
    return _engine
