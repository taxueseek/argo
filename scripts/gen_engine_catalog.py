#!/usr/bin/env python3
"""gen_engine_catalog.py — 生成搜索源使用文档（docs/ENGINE_CATALOG.md）。

为什么要生成而不是手写：引擎清单、密钥要求、可用状态、费用档位分散在
config.yaml / engines/specs/*.yaml / quota_profiles.json / engine_env 四处，
手写文档必然与事实脱节（本仓 2026-09-12 就出现过「文档说 12 个 MCP 工具、
实际 14 个」「文档说 150+ 引擎、分不清收录与可用」）。这里的做法是：
文档从**运行时同一批函数**取事实，`--check` 比对磁盘上的文档，
`tests/test_engine_catalog.py` 把 `--check` 挂进检查——文档改不动也漂不掉。
同一批数字还要露在五种语言的 README、徽章与包描述里，那几处也一起校正
（`sync_counts()`）：源数、免密钥数、业务域数、MCP 工具数、版本号都只看真源。

用法：
  python3 scripts/gen_engine_catalog.py            # 生成/覆盖文档并校正计数
  python3 scripts/gen_engine_catalog.py --check    # 只校验，过期或不一致退出码 1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
DOC_PATH = SKILL_DIR / "docs" / "ENGINE_CATALOG.md"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 文档只用「声明事实」分档，不用运行时状态（密钥是否已配、是否被熔断/封锁
# 都是本机瞬时状态）：文档要进仓库、要被别的机器生成复核，一旦写进
# 「本机此刻缺哪些密钥」，换个环境生成就与磁盘不符，检查随机变红。
# 运行时状态交给 `argo search --list-engines --detail` 现场查。
DOC_STATUS_KEY = "需自备密钥"
DOC_STATUS_DEP = "需装后端工具"
DOC_STATUS_DISABLED = "已停用"
DOC_STATUS_EXPLICIT = "显式专用"
DOC_STATUS_USABLE = "可直接用"


def _doc_status(row: dict[str, Any]) -> str:
    if not row["enabled"]:
        return DOC_STATUS_DISABLED
    parts = []
    if row["required_env"]:
        parts.append(DOC_STATUS_KEY)
    if row["requires"]:
        parts.append("需装后端工具" if not parts else "后端工具")
    if row.get("explicit_only"):
        parts.append(DOC_STATUS_EXPLICIT)
    return " + ".join(parts) if parts else DOC_STATUS_USABLE
# 费用档位 → 人话
COST_LABEL = {
    "free": "免费",
    "low": "低价计费",
    "api": "按调用计费",
    "paid": "付费",
}
# 能力族 → 文档分组标题
FAMILY_LABEL = {
    "web_general": "全网搜索",
    "academic": "学术文献",
    "code": "代码 / 包 / 文档",
    "finance_market": "行情 / 资金",
    "finance_macro": "宏观数据",
    "news_flash": "快讯 / 电报",
    "social": "社区 UGC",
    "hot_trending": "热榜",
    "world_news": "本地新闻流",
    "knowledge": "百科 / 实体",
    "science_chem": "化学 / 药学",
    "science_bio": "生物 / 蛋白",
    "science_geo": "地球 / 空间",
    "legal": "法律判例",
    "media_book": "媒体 / 图书",
    "sports": "体育",
    "archive": "归档 / 历史",
    "structured_card": "垂直结构化卡",
    "personal_data": "个人数据",
    "misc_vertical": "其他垂直",
}


def _collect() -> dict[str, Any]:
    """从运行时取事实（与 CLI/MCP 同一批函数，不另建计算方式）。"""
    from config import load_config, get_domains
    from engine_families import family_of
    from engine_policy import GENERAL_FREE_FALLBACK, get_engine_tier
    from engine_status import list_engines_detail
    import research as _research

    cfg = load_config()
    specs = cfg.get("engines", {})
    rows = list_engines_detail()
    quota = json.loads((SKILL_DIR / "backends" / "quota_profiles.json")
                       .read_text(encoding="utf-8"))

    # 分发路径：本引擎被哪些域/清单引用（文档要能回答「什么时候会用到它」）
    dispatch: dict[str, list[str]] = defaultdict(list)
    for d in get_domains(cfg):
        for e in d.get("engines_combo") or []:
            dispatch[e].append(f"域 {d['name']}")
    for e in GENERAL_FREE_FALLBACK:
        dispatch[e].append("通用兜底链")
    for e in (set(_research._RESEARCH_EN_BOOSTS)
              | set(_research._RESEARCH_ACADEMIC_BOOSTS)
              | set(_research._RESEARCH_JA_KO_BOOSTS)):
        dispatch[e].append("深度研究 boost")
    # 第 5 条路径：TF-IDF 语义画像（domain_profiles 里 documents 非空即参与语义路由）
    try:
        dp = json.loads((SKILL_DIR / "backends" / "domain_profiles.json")
                        .read_text(encoding="utf-8"))
        for e, v in dp.items():
            if isinstance(v, dict) and (v.get("documents") or []):
                dispatch[e].append("语义画像命中")
    except Exception:
        pass
    for r in rows:
        spec = specs.get(r["engine_id"]) or {}
        q = quota.get(r["engine_id"]) or {}
        r["family"] = family_of(r["engine_id"], spec)
        r["family_label"] = FAMILY_LABEL.get(r["family"], r["family"])
        r["tier"] = get_engine_tier(r["engine_id"], spec)
        r["cost_label"] = COST_LABEL.get(q.get("cost_tier", "free"), q.get("cost_tier", "免费"))
        r["limit"] = q.get("limit")
        r["period"] = q.get("period")
        r["enabled"] = bool(spec.get("enabled", True))
        # required_env / requires 是「声明要求」，与机器上是否已配无关
        r["required_env"] = list(r.get("required_env") or [])
        r["requires"] = list(r.get("requires") or [])
        r["dispatch"] = sorted(set(dispatch.get(r["engine_id"], [])))
        # desc 取引擎声明自述（engine_status 的 detail 不带 desc）
        r["desc"] = (spec.get("desc") or spec.get("label") or "").strip()
        r["doc_status"] = _doc_status(r)
    return {"rows": rows, "specs": specs, "cfg": cfg,
            "domains": get_domains(cfg)}


def _fmt_quota(row: dict[str, Any]) -> str:
    limit, period = row.get("limit"), row.get("period")
    if not limit:
        return "不限"
    unit = {"second": "秒", "minute": "分", "hour": "小时",
            "day": "天", "month": "月"}.get(str(period), str(period))
    return f"{limit}/{unit}"


def _where(row: dict[str, Any]) -> str:
    """这源什么时候会被用到（分发路径）。"""
    if row["dispatch"]:
        return "、".join(row["dispatch"])
    if row.get("explicit_only"):
        return "显式调用（--engine）"
    if row["engine_id"].startswith("local_"):
        return "经 local_search 展开"
    if not row["enabled"]:
        return "已停用"
    return "—"


def _table(rows: list[dict[str, Any]]) -> list[str]:
    out = ["| 引擎 | 状态 | 费用 | 频率上限 | 需自备密钥 | 什么时候用到 | 说明 |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        env = "、".join(r["required_env"]) if r["required_env"] else "—"
        desc = (r.get("desc") or "").replace("|", "／").strip() or "—"
        out.append(
            f"| `{r['engine_id']}` | {r['doc_status']} "
            f"| {r['cost_label']} | {_fmt_quota(r)} | {env} | {_where(r)} | {desc} |")
    return out


def render() -> str:
    d = _collect()
    rows = d["rows"]
    disabled = [r for r in rows if not r["enabled"]]
    keyed = [r for r in rows if r["enabled"] and r["required_env"]]
    needs_dep = [r for r in rows if r["enabled"] and r["requires"]]
    usable = [r for r in rows if r["enabled"] and not r["required_env"]
              and not r["requires"]]
    cost_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        cost_groups[r["cost_label"]].append(r)
    explicit = [r for r in rows if r.get("explicit_only")]
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in columns_sorted(rows):
        by_family[r["family_label"]].append(r)

    d_domains = d["domains"]
    L: list[str] = []
    L.append("# Argo 搜索源使用文档（自动生成）")
    L.append("")
    L.append("> 本文件由 `scripts/gen_engine_catalog.py` 生成，**勿手改**。")
    L.append("> 改引擎声明后重新生成：`python3 scripts/gen_engine_catalog.py`；")
    L.append("> 过期会被 `tests/test_engine_catalog.py` 拦下。")
    L.append("")
    L.append("## 一、总量与口径")
    L.append("")
    L.append(f"- **收录 {len(rows)} 个源**（config.yaml + `engines/specs/*.yaml` 声明合并后的总数）")
    L.append(f"- **开箱可用 {len(usable)} 个**：不需要你配任何密钥或装额外工具，"
             "自动路由就会用上")
    L.append(f"- **需自备密钥 {len(keyed)} 个**：{_names(keyed)}"
             "（没配也不影响搜索，路由会跳过）")
    L.append(f"- **需装后端工具 {len(needs_dep)} 个**：{_names(needs_dep)}"
             "（装好并登录后即可用）")
    L.append(f"- **已停用 {len(disabled)} 个**：{_names(disabled)}")
    L.append(f"- **显式专用 {len(explicit)} 个**：{_names(explicit)}"
             "（设计上不进自动路由，按 `--engine` 或交接提示调用）")
    L.append("")
    L.append("自己核一遍（口径不同，别混用）：")
    L.append("")
    L.append("```bash")
    L.append("argo search --list-engines | wc -l                  # 有实现的源（可实例化）")
    L.append("argo search --list-engines --detail | wc -l         # 收录总数（含已停用）")
    L.append("argo search --list-engines --routable-only | wc -l  # 本机此刻真能路由的")
    L.append("argo search --list-engines --detail                 # 逐源状态/密钥/依赖")
    L.append("```")
    L.append("")
    L.append("本文件的分档是**声明口径**（照引擎自己的声明算，换台机器也一样）；"
             "最后一行是**本机口径**——它把已配好密钥、已装好后端工具的源也算进来，"
             "再扣掉当前被熔断/封锁的源。两个数字不相等是正常的；"
             "想知道本机此刻到底哪些就绪，看 `--detail` 里的 `missing_env`（空 = 已配）。")
    L.append("")
    L.append("## 二、费用与密钥：哪些白用、哪些要钱")
    L.append("")
    billable = [(r["engine_id"], r["cost_label"], bool(r["dispatch"]), r["required_env"])
                for r in rows if r["cost_label"] != "免费"]
    free_rows = cost_groups.get("免费") or []
    free_keyed = sorted(r["engine_id"] for r in free_rows if r["required_env"])
    L.append(f"- **免费档 {len(free_rows)} 个**（含已停用）：无需密钥，或只需免费注册的密钥"
             + (f"；其中 {len(free_keyed)} 个要自备密钥（免费额度）："
                f"{'、'.join(free_keyed)}" if free_keyed else ""))
    L.append(f"- **计费档 {len(billable)} 个**（下表逐个列出，"
             "档位取自各源自己的 `cost_tier` 声明）")
    L.append("")
    L.append("| 引擎 | 档位 | 是否进自动路由 | 需自备密钥 |")
    L.append("|---|---|---|---|")
    for name, label, on_path, env in sorted(billable, key=lambda x: (x[1], x[0])):
        L.append(f"| `{name}` | {label} | {'是' if on_path else '否，仅显式调用'} "
                 f"| {'、'.join(env) if env else '—'} |")
    L.append("")
    paid_on_path = [n for n, lab, on, _ in billable if lab == "付费" and on]
    L.append("**结论**："
             + ("付费档（paid）目前**没有**接进自动路由，只有显式 `--engine` 才会用到；"
                if not paid_on_path else
                f"付费档里有 {len(paid_on_path)} 个进了自动路由（{', '.join(paid_on_path)}），"
                "用之前先确认额度；")
             + "低价/按量计费的源有 "
             f"{len([1 for _, lab, on, _ in billable if lab in ('低价计费', '按调用计费') and on])} "
             "个在自动路由路径上，多数带免费额度或已配密钥。"
             "额度记在本地配额表（`backends/quota_profiles.json` 的 limit / period，"
             "用量存在本机状态库），用尽后该源在语义路由里被降权；"
             "本地表统计的是 argo 自己的调用，若同一密钥还被别的工具用，"
             "实际额度以服务商侧为准。想彻底避开计费源，用 `--engine` 显式指定免费源，"
             "或走 `--mode budget`。")
    L.append("")
    L.append("## 三、特别能力（不是普通网页搜索）")
    L.append("")
    L.append("| 能力 | 怎么用 | 说明 |")
    L.append("|---|---|---|")
    L.append("| 垂直结构化卡 | 直接问「北京到上海高铁」「今天油价」「黄金价格」"
             "「2026 年历」「上海车牌摇号」等 | 火车票 / 油价 / 贵金属 / 万年历 / 星座 / 手机 / 汽车 / 挂号"
             "等有标准答案的问题，直接给答案而不是一堆链接 |")
    L.append("| 抽取型取证 | `argo search \"<推文URL或ID>\" --engine twitter_syndication` | "
             "按 URL 取单条推文，免登录、零密钥；关键词查询会诚实返回空（该通道没有搜索端点） |")
    L.append("| 学术与数据集 | 问「XX 论文」走学术域（arXiv/OpenAlex/Crossref/EuropePMC/DBLP/"
             "Semantic Scholar）；问「数据集/开放数据」走数据集域（DataCite/Zenodo） | "
             "论文与数据集分开走，互不串味 |")
    L.append("| 美股申报原文 | 问「苹果 10-K」「SEC filing」「招股书」 | "
             "SEC EDGAR 官方全文检索（免密钥），直出申报文件 |")
    L.append("| 深度研究 | `argo research \"问题\"` | 问题分解 → 多源采集 → 综合报告；"
             "注意它不套娃，worker 里不会再用研究工具 |")
    L.append("| 证据核验 | `argo evidence \"query\"`（或管道喂搜索结果） | "
             "对结果打分、标注事实/推断/未知 |")
    L.append("| 公众号全文 | `argo article \"<链接>\"` | 标题 / 正文 / 图片全量抽取 |")
    L.append("| 招聘聚合 | `argo job \"岗位\"` | BOSS / 猎聘 / 智联 / 前程无忧 / 597 / 今日招聘 |")
    L.append("| 抓取与爬站 | `argo fetch \"<url>\"` / `argo crawl \"<url>\"` / `argo extract \"<url>\"` | "
             "四级降级抓取（md 变体 → HTTP → TLS 指纹 → 浏览器）；crawl 走 sitemap/BFS；extract 取表格/Meta/JSON-LD |")
    L.append("| 批量预检 | `argo preflight \"url1\" \"url2\" …` | 开工前判定哪些能拿、哪些要登录、哪些已死，"
             "三档结论 go / go_with_skips / stop |")
    L.append("| 本地文件搜索 | `argo search \"词\" --include-local` | "
             "并入本机文件/笔记命中（source=local_files，不参与融合评分） |")
    L.append("| 可复算 | MCP `argo_recompute` / `--allow-recompute` | "
             "数值重算，默认关闭，需要显式授权 |")
    L.append("| 网页截图 / PDF | MCP `argo_screenshot` / `argo_pdf` | 长尾取证能力，默认不挂载 MCP 时用不到 |")
    L.append("")
    L.append("## 四、默认关闭的能力怎么打开")
    L.append("")
    L.append("- **MCP 19 工具面**（与 CLI 能力面对齐）：默认关（工具定义会常驻注入上下文）。DSH 用户在 profile patch "
             "里取消 `mcp-argo` 段注释后重启；其他客户端见 README「MCP 接入」。")
    L.append("- **原生工具**：默认只注册 `argo_search` / `argo_fetch`；"
             "`nativeTools` 配置可按需放开全部 18 个（`argo_research` 除外）。")
    L.append("- **需密钥的源**：把密钥写进 `~/.config/argo/env`（600 权限）或环境变量，"
             "下表中「需自备密钥」一列即变量名。")
    L.append("- **显式专用源**：不进自动路由，用 `argo search \"词\" --engine <引擎名>` 调用。")
    L.append("")
    L.append("## 五、逐源清单（按能力族）")
    L.append("")
    L.append("状态含义：**可直接用** = 自动路由会用上；**需自配密钥 / 需装后端工具** = 配好后即可用；"
             "**被上游封锁** = 源站当前拒绝；**已停用** = 配置层面关闭。")
    L.append("")
    for fam in sorted(by_family, key=lambda f: (-len(by_family[f]), f)):
        group = by_family[fam]
        L.append(f"### {fam}（{len(group)}）")
        L.append("")
        L.extend(_table(group))
        L.append("")
    L.append("## 六、分发域清单（自动生成）")
    L.append("")
    L.append(f"共 {len(d_domains)} 个业务域；"
             "命中即按域内的组合取源（多意图时按配置顺序取，窄意图域排在宽泛域之前）。"
             "自己看某个域为什么选这些源，看 `config.yaml` 的 `domains` 段。")
    L.append("")
    L.append("| 域 | 主源 | 组合 |")
    L.append("|---|---|---|")
    for dom in d_domains:
        combo = "、".join(f"`{x}`" for x in (dom.get("engines_combo") or []))
        L.append(f"| `{dom['name']}` | `{dom.get('primary') or '—'}` | {combo or '—'} |")
    L.append("")
    L.append("## 七、怎么自己查当前状态")
    L.append("")
    L.append("```bash")
    L.append(f"argo search --list-engines --detail | python3 -m json.tool | less   # 全部 {len(rows)} 个源的详情")
    L.append("argo search --list-engines --detail --routable-only              # 只看现在能用的")
    L.append("python3 scripts/matrix_search_eval.py --offline                   # 可达性门：有没有死源")
    L.append("python3 scripts/engine_validate.py --engine <名> --stage all       # 单个源的健康+质量双阶段体检")
    L.append("```")
    L.append("")
    return "\n".join(L) + "\n"


def columns_sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (0 if r["doc_status"] == DOC_STATUS_USABLE else 1,
                                       r["engine_id"]))


def _names(rows: list[dict[str, Any]]) -> str:
    return "、".join(f"`{r['engine_id']}`" for r in rows) if rows else "无"


# ── 对外计数：五语 README、徽章与包描述 ────────────────────────────────
# 源数、免密钥数、业务域数、MCP 工具数散落在五种语言与四个包描述里，
# 发布时靠手改必然漏：2026-10 的基线里中文写「覆盖 97 个业务领域」、
# 日文写「97 の業務ドメイン」、韩文还停在「272개의 검색 소스」、西班牙文
# 写「235 disponibles」，四处彼此矛盾。这里让文档生成器顺带把数字校正
# 到真源（catalog 的收录/开箱可用行、config 的域清单、mcp_tools 的工具表、
# SKILL.md 的版本号），`--check` 一并校验——改了引擎却忘了改 README 会红。
#
# 规则里只锚定「整句模板」，不做全局数字替换：发布说明里的历史数字
# （如「218 → 220 fuentes」「fuentes 237→253」）是事实，不能跟着变。
COUNT_RULES: dict[str, list[tuple[str, str]]] = {
    "README.md": [
        (r"badge/engines-(?P<total>\d+)-orange", "badge/engines-{total}-orange"),
        (r"badge/version-(?P<version>[\d.]+)-informational", "badge/version-{version}-informational"),
        (r"badge/MCP-(?P<tools>\d+)%20tools-purple", "badge/MCP-{tools}%20tools-purple"),
        (r'(?P<pre>alt="Argo 阿尔戈：[^"]*?，)(?P<total>\d+)(?P<post> 个搜索源，)',
         "{pre}{total}{post}"),
        (r"背后连着 (?P<total>\d+) 个搜索源，覆盖 (?P<domains>\d+) 个业务领域",
         "背后连着 {total} 个搜索源，覆盖 {domains} 个业务领域"),
        (r"另有 1 个 `wide_research` 并行研究和 (?P<tools>\d+) 个 MCP 工具按需开启",
         "另有 1 个 `wide_research` 并行研究和 {tools} 个 MCP 工具按需开启"),
        # 这句原本写死「其余 271 个源不受影响」，可它说的其实是「缺 key 时哪些源
        # 停用」，算不出发版数减几——去掉数字，只说事实（其余源不受影响）。
        (r"其余 (?P<n>\d+) 个源不受影响", "其余源不受影响"),
        (r"\*\*(?P<total>\d+) 个源，(?P<usable>\d+) 个免密钥开箱可用。\*\*",
         "**{total} 个源，{usable} 个免密钥开箱可用。**"),
        (r"目前 (?P<total>\d+) 个源（(?P<usable>\d+) 个免密钥开箱可用）、(?P<domains>\d+) 个业务域",
         "目前 {total} 个源（{usable} 个免密钥开箱可用）、{domains} 个业务域"),
        (r"当前配置 \*\*(?P<total>\d+)\*\* 个源（\*\*(?P<usable>\d+)\*\* 个免密钥开箱可用）、\*\*(?P<domains>\d+)\*\* 个业务域",
         "当前配置 **{total}** 个源（**{usable}** 个免密钥开箱可用）、**{domains}** 个业务域"),
        (r"搜索、抓取、核验、本机文件检索走同一条管线，(?P<tools>\d+) 个 MCP 工具和 CLI 命令一一对应",
         "搜索、抓取、核验、本机文件检索走同一条管线，{tools} 个 MCP 工具和 CLI 命令一一对应"),
    ],
    "README.en.md": [
        (r"badge/engines-(?P<total>\d+)-orange", "badge/engines-{total}-orange"),
        (r"badge/version-(?P<version>[\d.]+)-informational", "badge/version-{version}-informational"),
        (r"badge/MCP-(?P<tools>\d+)%20tools-purple", "badge/MCP-{tools}%20tools-purple"),
        (r'(?P<pre>alt="Argo: search and deep research for agents — )(?P<total>\d+)(?P<post> search sources,)',
         "{pre}{total}{post}"),
        (r"It connects to (?P<total>\d+) search sources across (?P<domains>\d+) business domains",
         "It connects to {total} search sources across {domains} business domains"),
        (r"and (?P<tools>\d+) MCP tools you can enable on demand",
         "and {tools} MCP tools you can enable on demand"),
        (r"and the other (?P<n>\d+) sources are unaffected", "and the other sources are unaffected"),
        (r"\*\*(?P<total>\d+) sources, (?P<usable>\d+) keyless and ready out of the box\.\*\*",
         "**{total} sources, {usable} keyless and ready out of the box.**"),
        (r"\((?P<usable>\d+) keyless sources\)", "({usable} keyless sources)"),
        (r"All five are already shipped across the v2\.9\.x line: (?P<total>\d+) sources, "
         r"(?P<domains>\d+) domains, (?P<usable>\d+) keyless out of the box",
         "All five are already shipped across the v2.9.x line: {total} sources, "
         "{domains} domains, {usable} keyless out of the box"),
        (r"the (?P<tools>\d+) MCP tools map one-to-one onto CLI commands",
         "the {tools} MCP tools map one-to-one onto CLI commands"),
        (r"\*\*(?P<total>\d+) sources \((?P<usable>\d+) usable with no key\), "
         r"(?P<domains>\d+) domains\*\*",
         "**{total} sources ({usable} usable with no key), {domains} domains**"),
        (r"Config currently has \*\*(?P<total>\d+)\*\* sources \(\*\*(?P<usable>\d+)\*\* usable "
         r"with no key\) and \*\*(?P<domains>\d+)\*\* domains",
         "Config currently has **{total}** sources (**{usable}** usable with no key) "
         "and **{domains}** domains"),
    ],
    "README.es.md": [
        (r"badge/engines-(?P<total>\d+)-orange", "badge/engines-{total}-orange"),
        (r"badge/version-(?P<version>[\d.]+)-informational", "badge/version-{version}-informational"),
        (r"badge/MCP-(?P<tools>\d+)%20tools-purple", "badge/MCP-{tools}%20tools-purple"),
        (r"(?P<pre>alt=\"Argo: búsqueda e investigación profunda para agentes, )(?P<total>\d+)(?P<post> fuentes de búsqueda,)",
         "{pre}{total}{post}"),
        (r"Detrás tiene (?P<total>\d+) fuentes de búsqueda que cubren (?P<domains>\d+) dominios de negocio",
         "Detrás tiene {total} fuentes de búsqueda que cubren {domains} dominios de negocio"),
        (r"Además hay 1 `wide_research` de investigación en paralelo y (?P<tools>\d+) herramientas MCP",
         "Además hay 1 `wide_research` de investigación en paralelo y {tools} herramientas MCP"),
        (r"y las otras (?P<n>\d+) fuentes no se ven afectadas", "y las demás fuentes no se ven afectadas"),
        (r"\*\*(?P<total>\d+) fuentes, (?P<usable>\d+) disponibles sin clave desde el primer momento\.\*\*",
         "**{total} fuentes, {usable} disponibles sin clave desde el primer momento.**"),
        (r"Los cinco puntos ya están implementados en la serie v2\.9\.x: "
         r"(?P<total>\d+) fuentes, (?P<domains>\d+) dominios, (?P<usable>\d+) sin clave",
         "Los cinco puntos ya están implementados en la serie v2.9.x: "
         "{total} fuentes, {domains} dominios, {usable} sin clave"),
        (r"(?P<tools>\d+) herramientas MCP se corresponden una a una",
         "{tools} herramientas MCP se corresponden una a una"),
        (r"docs/ENGINE_CATALOG\.md: (?P<total>\d+) fuentes, (?P<usable>\d+) sin clave, (?P<domains>\d+) dominios",
         "docs/ENGINE_CATALOG.md: {total} fuentes, {usable} sin clave, {domains} dominios"),
        (r"La config tiene ahora \*\*(?P<total>\d+)\*\* fuentes \(\*\*(?P<usable>\d+)\*\* sin clave\) "
         r"y \*\*(?P<domains>\d+)\*\* dominios",
         "La config tiene ahora **{total}** fuentes (**{usable}** sin clave) y **{domains}** dominios"),
    ],
    "README.ja.md": [
        (r"badge/engines-(?P<total>\d+)-orange", "badge/engines-{total}-orange"),
        (r"badge/version-(?P<version>[\d.]+)-informational", "badge/version-{version}-informational"),
        (r"badge/MCP-(?P<tools>\d+)%20tools-purple", "badge/MCP-{tools}%20tools-purple"),
        (r"(?P<pre>alt=\"Argo アルゴ：[^\"]*?、)(?P<total>\d+)(?P<post> の検索ソース、)", "{pre}{total}{post}"),
        (r"背後には (?P<total>\d+) の検索ソースがあり、(?P<domains>\d+) の業務ドメイン",
         "背後には {total} の検索ソースがあり、{domains} の業務ドメイン"),
        (r"(?P<tools>\d+) 個の MCP ツール", "{tools} 個の MCP ツール"),
        (r"\*\*(?P<total>\d+) のソース、うち (?P<usable>\d+) はキー不要ですぐ使える。\*\*",
         "**{total} のソース、うち {usable} はキー不要ですぐ使える。**"),
        (r"（(?P<usable>\d+) ソースが無設定）", "（{usable} ソースが無設定）"),
        (r"v2\.9\.x 系で全て実装済み：(?P<total>\d+) ソース / (?P<domains>\d+) ドメイン / (?P<usable>\d+) 無設定",
         "v2.9.x 系で全て実装済み：{total} ソース / {domains} ドメイン / {usable} 無設定"),
        (r"ENGINE_CATALOG\.md に清单されている：(?P<total>\d+) ソース、(?P<usable>\d+) 無設定、"
         r"(?P<domains>\d+) ドメイン",
         "ENGINE_CATALOG.md に清单されている：{total} ソース、{usable} 無設定、{domains} ドメイン"),
        (r"設定上は \*\*(?P<total>\d+)\*\* ソース（うち \*\*(?P<usable>\d+)\*\* はキー不要）、"
         r"\*\*(?P<domains>\d+)\*\* ドメイン",
         "設定上は **{total}** ソース（うち **{usable}** はキー不要）、**{domains}** ドメイン"),
    ],
    "README.ko.md": [
        (r"badge/engines-(?P<total>\d+)-orange", "badge/engines-{total}-orange"),
        (r"badge/version-(?P<version>[\d.]+)-informational", "badge/version-{version}-informational"),
        (r"badge/MCP-(?P<tools>\d+)%20tools-purple", "badge/MCP-{tools}%20tools-purple"),
        (r"뒤에는 (?P<total>\d+)개의 검색 소스가 있고 (?P<domains>\d+)개 업무 영역",
         "뒤에는 {total}개의 검색 소스가 있고 {domains}개 업무 영역"),
        (r"(?P<tools>\d+)개의 MCP 도구", "{tools}개의 MCP 도구"),
        (r"\*\*(?P<total>\d+)개 소스, 그중 (?P<usable>\d+)개는 키 없이 바로 사용\.\*\*",
         "**{total}개 소스, 그중 {usable}개는 키 없이 바로 사용.**"),
        (r"\(무설정 (?P<usable>\d+)개\)", "(무설정 {usable}개)"),
        (r"v2\.9\.x 계열에서 모두 구현되었습니다: (?P<total>\d+) 소스 / (?P<domains>\d+) 도메인 / "
         r"(?P<usable>\d+) 무설정",
         "v2.9.x 계열에서 모두 구현되었습니다: {total} 소스 / {domains} 도메인 / {usable} 무설정"),
        (r"ENGINE_CATALOG\.md에 있다: (?P<total>\d+) 소스, (?P<usable>\d+) 무설정, (?P<domains>\d+) 도메인",
         "ENGINE_CATALOG.md에 있다: {total} 소스, {usable} 무설정, {domains} 도메인"),
        (r"설정에는 현재 \*\*(?P<total>\d+)\*\* 소스\(\*\*(?P<usable>\d+)\*\*개는 키 불필요\)와 "
         r"\*\*(?P<domains>\d+)\*\* 도메인",
         "설정에는 현재 **{total}** 소스(**{usable}**개는 키 불필요)와 **{domains}** 도메인"),
    ],
    "SKILL.md": [
        (r"收录 (?P<total>\d+) 个源、(?P<usable>\d+) 免密钥", "收录 {total} 个源、{usable} 免密钥"),
    ],
    "package.json": [
        (r"(?P<total>\d+) 个源（(?P<usable>\d+) 个免密钥开箱可用）",
         "{total} 个源（{usable} 个免密钥开箱可用）"),
        (r"(?P<tools>\d+) 个 MCP 工具（与 CLI 能力面对齐）", "{tools} 个 MCP 工具（与 CLI 能力面对齐）"),
    ],
    "packages/marketplace.json": [
        (r"over (?P<total>\d+) sources", "over {total} sources"),
        (r"（(?P<total>\d+) 源）", "（{total} 源）"),
    ],
    "packages/argo-zcode/.zcode-plugin/plugin.json": [
        (r"over (?P<total>\d+) sources \((?P<usable>\d+) keyless\)",
         "over {total} sources ({usable} keyless)"),
        (r"over (?P<total>\d+) sources(?! \()", "over {total} sources"),
        (r"(?P<total>\d+) 个源、(?P<usable>\d+) 个免密钥可用", "{total} 个源、{usable} 个免密钥可用"),
    ],
    "packages/argo-zcode/skills/argo/SKILL.md": [
        (r"收录 (?P<total>\d+) 个源、(?P<usable>\d+) 个免密钥可用",
         "收录 {total} 个源、{usable} 个免密钥可用"),
        (r"（(?P<total>\d+) 源、(?P<usable>\d+) 免密钥）", "（{total} 源、{usable} 免密钥）"),
    ],
}


def _count_truth() -> dict[str, str]:
    """对外计数的唯一真源：catalog 分档 + config 域表 + mcp_tools 工具表 + 版本号。"""
    from config import get_domains, load_config  # noqa: PLC0415  (与运行时同源)
    from mcp_tools import TOOLS  # noqa: PLC0415

    rows = _collect()["rows"]
    total = len(rows)
    # 与 render() 里「开箱可用」同一口径：enabled 且不挑密钥、不挑后端工具。
    usable = sum(1 for row in rows if row["enabled"] and not row["required_env"] and not row["requires"])
    version = re.search(r"^version:\s*(\S+)", (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"), re.M)
    if version is None:
        raise SystemExit("SKILL.md 缺 version 行，无法同步徽章")
    return {
        "total": str(total),
        "usable": str(usable),
        "domains": str(len(get_domains(load_config()))),
        "tools": str(len(TOOLS)),
        "version": version.group(1),
    }


def sync_counts(write: bool) -> list[str]:
    """把 README / 徽章 / 包描述里的计数校正到真源，返回有差异的文件。"""
    truth = _count_truth()
    stale: list[str] = []
    for rel, rules in COUNT_RULES.items():
        path = SKILL_DIR / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        fixed = text
        for pattern, template in rules:
            # 具名组只提供 pre/post 这类字面片段，数字一律用真源里的 total/usable…
            fixed = re.sub(
                pattern,
                lambda m, t=template: t.format(**{**m.groupdict(), **truth}),
                fixed,
            )
        if fixed != text:
            stale.append(rel)
            if write:
                path.write_text(fixed, encoding="utf-8", newline="\n")
    return stale


def main() -> int:
    ap = argparse.ArgumentParser(description="生成搜索源使用文档")
    ap.add_argument("--check", action="store_true", help="只校验文档是否过期")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not DOC_PATH.exists():
            print(f"❌ 文档不存在：{DOC_PATH}", file=sys.stderr)
            return 1
        if DOC_PATH.read_text(encoding="utf-8") != text:
            print("❌ 搜索源文档已过期，重新生成：python3 scripts/gen_engine_catalog.py",
                  file=sys.stderr)
            return 1
        stale = sync_counts(write=False)
        if stale:
            print("❌ 以下文件的计数与引擎真源不一致：", file=sys.stderr)
            for rel in stale:
                print(f"   - {rel}", file=sys.stderr)
            print("   重新生成：python3 scripts/gen_engine_catalog.py", file=sys.stderr)
            return 1
        print(f"✅ 搜索源文档与当前引擎声明一致（{len(_collect()['rows'])} 个源）")
        return 0
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(text, encoding="utf-8")
    print(f"已生成 {DOC_PATH.relative_to(SKILL_DIR)}（{len(_collect()['rows'])} 个源）")
    for rel in sync_counts(write=True):
        print(f"已校正计数：{rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
