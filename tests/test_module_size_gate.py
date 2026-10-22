#!/usr/bin/env python3
"""test_module_size_gate.py — 单文件体积门禁（防「千行文件」复发）。

## 为什么要这道门

2026-09-21 的结构审查里，route.py 长到 2402 行、search.py 3033 行，同一批
职责（域规则 / 语言选源 / 组合装配 / 预算策略 / 排序 / 输出 / 缓存）全挤在
两个文件里。后果不是「不好看」，而是**改动的定位成本**：修一条域正则要在
2400 行里找匹配层，改排序要读完整条网络调度链。

拆完之后需要一道门把趋势钉住，否则半年后又会回到原样。规则三条：

1. **上限 1000 行**：`scripts/` 下任何文件不得超过；
2. **祖父清单**：拆不动的大文件（引擎构建器这类数据表、尚未分解的
   execute_search/super_search）显式登记 `上限 = 登记时的行数`，
   只能减不能增；
3. **新增文件天然合规**：新文件不在清单里，直接吃 1000 上限。

## 为什么要「只能减不能增」而不是留余量

留余量（比如「上限 = 当前 + 10%」）在多次小改动后会被吃光，且没人察觉。
写死成当前行数，任何增长都要**在这个文件里改一个数字**——一次可见、可评审的
动作，而不是无声的漂移。想加功能又超上限时，正解是拆模块（把上限往下调），
不是把数字往上调；真要上调，理由必须写在 reason 里。

## 清单怎么维护

- 拆走一块 → 把该条的上限改成新的行数（或整条删掉，若已 ≤ 1000）；
- 上限**不许**高于文件实际行数（下面有用例锁），所以清单不会虚高。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

HARD_LIMIT = 1000

# 祖父清单：路径 → (上限行数, 为什么不拆/拆到哪一步)
#
# 排序即待办优先级（最上面的最该拆）。新增条目要在 PR 描述里说明为什么
# 现在不拆——「以后再拆」不算理由，要写清楚卡在哪（缺锁、缺测试、边界不清）。
GRANDFATHERED: dict[str, tuple[int, str]] = {
    "scripts/engines_builders_data.py": (
        2923, "源声明构建器（数据表性质，逐源一段），拆开只是把同一张表切成多份；"
              "2026-09-27 −3：百度百科相关度门与化学 token 改用 cjk_tokens 共享切分；"
              "2026-09-30 −1：octen/tavily 常量分叠加位次衰减后注释收敛"),
    "scripts/engines_builders_cn.py": (
        2283, "中文源声明构建器，逐源一段（数据表性质）；按语言/领域切只会把同一张表切碎；"
              "2026-09-28 −36：bocha_ai 模态卡特化移除（_BOCHA_CARD_NAMES 类型表/"
              "_flatten_card helper/卡分支，非 webpage 消息一律跳过）"),
    "scripts/engines_builders_batch9.py": (
        1468, "批次九的源声明构建器，逐源一段；与既有构建器同形，拆开不减少概念"),
    "scripts/engines_builders_intl.py": (
        1239, "国际（日韩/欧洲）源声明构建器，逐源一段；含各源 XML/RSS 解析差异；"
              "2026-09-29 +19：open_meteo 多 place forecast 并行化（串行 9.28s → "
              "3.9-5.1s，bounded_run 有界并发 + 保序）"),
    "scripts/caliber_isolation.py": (
        195, "口径隔离：RRF 融合前按口径分组，不同口径不直接 merge；"
              "纯函数，可独立测试；"
              "2026-10-17 +9：分组单位从「单条结果」改回「整条引擎列表」——"
              "旧写法让 merge_within_caliber 的 i 恒为 0，同引擎全部同分、"
              "名次衰减归零（tests/test_caliber_isolation.py 锁三条不变式）；"
              "同日清理 15 个已下线/改名的口径条目（-2，新增 registry 同步门禁）"),
    "scripts/rrf_classic.py": (
        66, "经典 RRF 融合（weighted=False 路径）；"
              "所有引擎列表在同一个池中竞争，每个列表独立贡献排名"),
    "scripts/x_article_fetch.py": (
        141, "X Article 正文提取（Draft.js 重建）；"
              "通过 FxTwitter API 获取 blocks 并重建为 Markdown"),
    "scripts/url_optimize.py": (
        56, "URL 优化：Reddit 重写、追踪参数清理；"
              "纯函数，可独立测试；"
              "2026-10-17 +2：normalize_url 真正去掉 fragment（docstring 一直"
              "这么写，旧实现把它原样带回）"),
    "scripts/engines_builders_tech.py": (
        1359, "技术社区源声明构建器（V2EX/StackExchange 等），逐源一段；"
              "2026-09-26 批次十增强净增 +402：sov2ex 一级全文来源（v2ex 就地升级）、"
              "tineye/bing_rss 新引擎、exa 免 key 匿名通道——与原逐源一段同性质，"
              "拆出只会把同一张表切碎（新引擎的下一站是 batch 模块）；"
              "2026-10-17 +6：Exa 分数改 None 哨兵（显式 0 分不再被 or 抬成 "
              "0.75，与 2026-09-30 修过的同类同构）；"
              "2026-09-28 并行会话进行中的搜狗微信中间链解析 WIP（+96 行起，"
              "工作区未提交、当日仍在增长 1600→1604）——上限随工作区现值登记，"
              "随该工作正式提交后由其转正或回调；"
              "2026-10-05 A1 惰性 logging 导入（+11）：模块级 import logging 改"
              "_LazyLogger 代理类，省 ~10ms/次冷启动；"
              "2026-10-14 7ca2f22 移除 OSV/CISA KEV 代码段（−261）"),
    "scripts/fetch_v3.py": (
        1804, "抓取降级链（HTTP→md 变体→TLS 指纹→jina→Parallel→浏览器），"
              "每级都要保留顺序与超时语义，尚未找到能一次搬走且可验证的切面；"
              "2026-09-27 +10：identity memory 内存缓存（dirty flag + 30s 写盘节流）；"
              "2026-09-28 +2：_identity_mem 内存表 512 有界淘汰；"
              "2026-09-29 +10：llms-full.txt token 炸弹守卫（候选出口过滤）；"
              "2026-10-05 js-run 车道已拆出 fetch_jsrun.py（回撤一段），"
              "+4：deadline_s 参数级预算（verify 快道用）；"
              "2026-10-22 +25：article_meta（links/byline/excerpt）接入 "
              "_make_result；后续可把该段拆到 fetch_meta 再回调；"
              "2026-10-06 +1：jsrun 车道退避预算接线（backoff_budget_s，"
              "挑战重试不再越过链级 deadline——本体与测试都在 fetch_jsrun 侧）；"
              "2026-10-06 A2 登录态车道 +11（auth_profile 判定与传参），"
              "同日浏览器档整级拆出 fetch_browser.py −36"),
    "scripts/mcp_handlers.py": (
        1060, "MCP 工具 handler 分发（19 工具 → CLI 模块）；"
              "2026-10-22 +2：argo_fetch include_links 输出开关；"
              "2026-09-29 +8：661e0f1（H2 seek 进程内化）带入的增量，"
              "并行会话未登记即合入——本行补登记；同日 −1：路由预热改调 "
              "route_domains.warm_compiled_domains（原在此内联复刻同一句，"
              "使它成了无人调用的死函数）；拆分候选：surface 五工具"
              "已拆出（mcp_handlers_surface），剩余是分发表与粘合层；"
              "2026-10-03 +32：数值参数守卫收口（_InvalidParam/_int_param "
              "助手 + timeout×5 与 max_chars×6 共 11 处裸 int() 转换，"
              "非法类型显式 -32602，测试 test_mcp_param_guard.py 锁行为）；"
              "2026-10-18 +35：warm 线程补 anysearch 预连接（_prewarm_anysearch_"
              "connection，initialize 后把代理 TLS 藏进首包前的空闲期；CLI 侧实测"
              "无窗口故不改 bin/argo），上限 1039 → 1074 随动登记"),
    "scripts/engines_base.py": (
        1521, "引擎基类 + HTTP 出口 + 输出映射，与 100+ 源声明的字段契约绑在一起；"
              "2026-10-17 +13：通用 HTML 解析器接入 jump_resolver 的离线跳转"
              "还原（Bing ck/a 的 u=a1<base64url>），不发网络请求；"
              "2026-10-17 +7：http_open 错误体归因读加 KeyError 兜底——3.9 的 "
              "HTTPError(fp=None) 首次 .read() 抛 KeyError('file')，漏接把「诚实"
              "上报 403」退化成静默空结果（tests/test_bocha.py 403 可见性用例）；"
              "2026-10-10 −12：serp 守卫接线+误杀账本拆到 serp_kill_log.py（尺寸门"
              "哲学：加东西先拆模块），本处留薄委托；"
              "2026-09-26 +42：SERP 垃圾守卫接线（守卫本体独立在 serp_guard.py，此处只留"
              "冻结集+调用点）、反爬全文级标记（DDG challenge 实测）、key 脱敏三形态兜底；"
              "同日 −28：移除 DDG Instant Answer 解析器（引擎随本机可达性门下线）；"
              "同日 +41：{UUID} 进程级身份占位符（BAIDUID 反关联）+ tls_impersonate/"
              "resolve_redirects 两契约接线（跳转解析本体独立在 jump_resolver.py）；"
              "同日 −1：语言参数动态化补上 mkt（_build_html_engine 这侧漏了，"
              "lang_detect 的 mkt 表一直备着却无人调用），注释同步精简；"
              "2026-09-27 +14：logging 延迟加载（_get_logger 模式）+ "
              "_redact_secrets fallback 加基本脱敏（Bearer token + key=value）；"
              "2026-09-28 +2：_detect_anti_bot head 区按需 lower（性能优化）"),
    "scripts/cache.py": (
        1373, "结果缓存 + 路由软命中 + 指纹；「键/存储/命中策略」三段的第二段"
              "已落地——2026-10-16 命中判据层（normalize_query / minhash "
              "query_similarity / split_qualifiers / is_freshness_sensitive_query"
              "共 ~115 行）整段拆至 cache_similarity.py，本文件只剩存储实现与门面"
              "（re-export 保持既有 import 与 monkeypatch 面不变），上限随拆随降"
              "（1382 → 1357，随拆随降）；第一段「键」在 cache_key_vdom.py、第三段「准入"
              "策略」在 cache_guard.py。历史记录："
              "+1=except_sets 具名异常导入行；+3=2026-09-27 退化写入守卫的两处"
              "调用点与一行 import——判定逻辑本身已拆到 cache_guard.py，本文件"
              "只剩调用，不再是「准入策略混在存储实现里」的状态；"
              "2026-09-27 +9：sqlite3 延迟导入（_get_sqlite3 模式）+ L1 100→500；"
              "−1=2026-09-27 引擎级垂直域维度（--domain/--sub_domain）并入缓存键，"
              "摊键逻辑拆到 cache_key_vdom.py，本文件只留调用点；"
              "2026-09-28 +2：normalize_query 加 @lru_cache 装饰器（性能优化，"
              "随 2026-10-16 拆出迁入 cache_similarity.py）；"
              "2026-09-29 +2：失败态写入守卫调用点（判据在 cache_guard.py，"
              "治「配好 key 仍回放 45s 前的失败」——issue #12 续发同类）；"
              "2026-09-30 +5：per-engine 层补负缓存不覆盖好条目守卫（同族 combo "
              "2026-09-19 修复）+ 自适应 TTL 按层取帽（硬帽层不超帽、无帽层 2×、"
              "日末延长不压缩）；"
              "2026-10-15 B-4：+113（cache.enabled / max_size_mb 死配置接线 + "
              "_conn() 连接上下文管理器修 fd 泄漏）——超出当时上限 1382，"
              "本次拆模块一并收口；"
              "2026-10-17 +28：①软命中跳过负缓存来源（空 results 条目不当命中"
              "交出，4 行）；②SearchCache.set 加 recovered 形参——恢复链成功"
              "的批次整批退化时降级短 TTL 写入而非硬拒（治「重复查询重付 "
              "dispatch+恢复链」结构性循环，12 行；策略常量在 cache_guard."
              "RECOVERED_DEGRADED_TTL）；随动上限 1357 → 1385"),
    "scripts/engine_dispatch.py": (
        982, "引擎编排调度器（wave-1 竞速 / wave-2 补全 / 串行对冲 / 收账）；"
              "2026-10-21 −36：DispatchResult 与墙钟记账拆到 dispatch_result.py"
              "（纯数据、无 IO；顺带把尾等待从 wasted 单值拆成 tail/abandoned "
              "两个可行动量），上限 1019 → 983；"
              "不拆的理由：三个分支共用同一棵闭包（raw_results / engine_latency / "
              "engine_done_ms / abandoned_ms / quota_batch 等 15 处共享状态），"
              "拆分会把闭包状态摊成参数面，风险与 diff 面都大于 1000 行本身的 "
              "维护成本——与 engines_builders_* 的「数据表性质」不同类，这里 "
              "拆不开是耦合性质决定的，不是懒；"
              "2026-10-16 +29：tail_grace_fn 分层尾宽接线（默认关，"
              "ARGO_ADAPTIVE_TAIL，对拍见 docs-local/调度对拍_尾宽分层与并发_"
              "2026-10-16.md）+ 审查收口（gate 关闭时永不起步的队列条目不计入 "
              "分层判据——死条目会把尾宽拉长，方向反了），上限 1000 → 1029 随动登记；"
              "2026-10-17 +4：per-engine 缓存 get/set 键从 query 改为 "
              "retrieval_query（键与实际检索词同源，clean_query 变更不再发旧语义"
              "结果），上限 1029 → 1033 随动登记；"
              "2026-10-18 +3：硬配额耗尽独立分类（_NOTE_STATUS 加 "
              "quota-exhausted，_QUOTA_ERROR_KEYWORDS 补「余额/额度/配额」文本），"
              "上限 1033 → 1036 随动登记；"
              "2026-10-19 −17：_QUOTA_ERROR_KEYWORDS 收口至 cache_guard 唯一真源 + "
              "熔断器记账分支改为 _BREAKER_FAIL_KIND 表驱动，上限 1036 → 1019；"
              "2026-10-21 −1：重试按失败类别区分（确定性失败不再重试）——"
              "classify_error_status / is_retryable_failure / 拦截页词表 / "
              "NON_RETRYABLE_STATUS 拆到 retry_policy.py（重试判据与归因判据"
              "同源，且本文件已贴上限）；顺带删掉 search.py 里只导入未使用的 "
              "_QUOTA_ERROR_KEYWORDS 死导入、job.py 改从真源 cache_guard 取表，"
              "上限 983 → 982"),
    "scripts/http_client.py": (
        1053, "HTTP 客户端（UA 轮换 + Cookie 积累 + 重试 + 主机节流）；"
              "2026-09-27 +8：host throttle buckets 加 LRU 淘汰（100 个上限）；"
              "2026-09-28 +10：淘汰只踢零活跃桶（_active 计数 + idle 过滤），"
              "消灭「踢掉在用桶 → 同主机限速击穿」；"
              "同日 +12：POST curl fallback 补 resolve_proxy（POST 代理缝隙）+ "
              "Set-Cookie 解析失败 debug 留痕（最高频静默点）；"
              "2026-10-18 +26：POST 改走 conn_pool（借/还/失效换新重试一次，"
              "与 GET 同构）——anysearch 是 POST 引擎，长驻 MCP/daemon 此前每次"
              "查询重付代理后 TLS，实测同连接第二次省 322ms（anysearch）/ 561ms"
              "（octen）；上限 1027 → 1053 随动登记"),
    "scripts/search_rank.py": (
        991, "RRF 融合 + minhash 去重 + 五维重排；"
              "2026-10-17 口径隔离逻辑拆出 caliber_isolation.py（−79 行）；"
              "2026-10-17 +4：_attach_selection_signals 接 authority_level 离散"
              "等级（DeepSeek 语义 0-4，映射与上游采信逻辑在 rank_signals）+"
              "2026-09-27 +18：_weight_cache/_rel_factor_cache 加 TTL+大小限制（_evict_cache）；"
              "2026-09-28 +4：语言调整失败不写 _weight_cache（防 30s 固化降级权重）；"
              "2026-09-29 −3：RRF 加权开关收编到 env_flag（第五套真值表消亡，"
              "try/except 双路径与本地真值表一并删除）；"
              "2026-09-29 +4：_apply_consensus_and_sort 夹非负 max_results"
              "（1 行代码 + 3 行注释：负数切片从尾部截，10 条给 7 条，"
              "且 MCP/库调用绕开 argparse，兜底必须落在切片点）；"
              "2026-10-08 +2：traceability 评分接入快速路径"
              "（score_traceability 调用 + 权重调整 0.40→0.35/0.35→0.30）; "
              "1 行 import；"
              "2026-10-16 +2：minhash_dedupe 近似命中带内接 rapidfuzz 模糊近重复"
              "（可选依赖、判定逻辑全部在 rank_signals.fuzzy_near_dup，本文件只留"
              "调用点与数据行豁免排序注释）；"
              "2026-10-16 +1：_content_sig 末尾接 normalize_title_suffix 剥站后缀"
              "（跨站同文对差异几乎全在结尾后缀，零依赖；归一化逻辑在 rank_signals）"),
    "scripts/search_pipeline.py": (
        1076, "搜索编排主管线（路由→分派→融合→恢复→漏斗输出）；"
              "2026-10-19 −24：自适应学习循环改用 cache_guard.should_record_adaptive "
              "统一状态态过滤 + learner.batch() 单事务批量提交，上限 1101 → 1077；"
              "2026-10-17 +3：finalize 的 cache.set 透传 recovered 标记（恢复成功"
              "批次整批退化时降级短 TTL 写入，判据与常量在 cache_guard，存储侧"
              "改动在 cache.SearchCache.set）；"
              "2026-10-17 +25：site:/-site: 后置过滤接线（恢复链与精排之后、"
              "截断之前；判据与过滤本体在 query_operators，此处只留调用点）；"
              "2026-10-17 +20：查询词回声守卫接线（恢复链触发 + 档位提升到 L3 "
              "+ L1 假恢复过滤）——修「anysearch 超时后 local_bing 把整句当单词"
              "查、回声被原样交付」；判据与两个信号本体（echo_recovery_needed / "
              "drop_echo_items）全在 postprocess_signals.py，本文件只留三处调用点"
              "（与 search_attribution / L0 同一手法）；"
              "2026-10-16 +19：两个可选信号钩子（TF-IDF 抛光 / 数字交叉验证）"
              "2026-10-16 +19：两个可选信号钩子（TF-IDF 抛光 / 数字交叉验证）"
              "接线后主管线曾到 1070——方案二梳理提出到 postprocess_signals.py，"
              "本文件只留每特性 3~4 行调用点（与 search_attribution 同形态），"
              "净 +19；信号本体含开关/降级/日志都在新模块，"
              "基线 1029（距原登记 1030 仅 1 行余量），"
              "2026-10-15 +3：finalize 的 usage_log 接入引擎级耗时归因"
              "（+1 import usage_log_attribution、+1 行接线注释、"
              "+1 行 **usage_log_attribution(engine_latency) 展开）；"
              "归因的提取/聚合/字段构造三件事全部落在新模块 "
              "search_attribution.py，本文件只留一行调用点"
              "（与 L0 拆 recovery_l0 同一手法）；"
              "2026-09-30 +2：恢复链与变体召回补透传 --domain/--sub_domain"
              "（用户域约束此前被两条辅路旁路）+ variant_recalled/phrase_dropped"
              " 观测补账（变体条目不进漏斗 returned 口径，不补账会出现"
              "「deduped > returned」的矛盾账）；拆分候选：恢复执行器"
              "_recovery_executor 可独立成 recovery_runner；"
              "2026-10-11 +15：恢复链 L0（early-stop 误判时补跑被跳过的"
              "同域引擎）拆为独立模块 recovery_l0.py（执行器注入、"
              "不依赖编排状态），本文件仅留调用点胶水——L0 主逻辑"
              "已不在主管线；"
              "2026-10-11 甲轮收口 +15：L0 死线（补跑不得推出用户超时预算，"
              "语义在 recovery_l0）+ L3 恢复结果壳口径复检（恢复产出是主链"
              "壳过滤之后的唯一壳回流入口，过滤逻辑在 recovery_l0."
              "finalize_l3_results，判定唯一出口 serp_guard.drop_shell_urls）；"
              "下一步拆 _recovery_executor（L3）后本文件"
              "可回落到 1000 行内"),
    "scripts/job.py": (
        1186, "招聘多平台聚合，各平台解析各成一段（数据表性质）；"
              "2026-09-28 +4：MCPJOBS_DIR 收编 argo_paths 平台缓存根 + "
              "ARGO_MCPJOBS_DIR 显式覆盖（原硬编码 ~/.cache）"),
    "scripts/matrix_search_eval.py": (
        1120, "离线路由矩阵（138 条检查项），用例表占多数；"
              "2026-10-10 +2：入口状态隔离（真实搜索路径会写生产状态，实测夹具"
              "引擎已混入 circuit_breaker/usage_log），接 argo_paths.isolate_state_dir；"
              "2026-10-22 +15：补 company_search 场景 3 条用例（此前该域零用例，"
              "「中国企业股权/工商信息」被路由到本域却无中文能力的问题长期无人"
              "发现）+ 华为总部用例（钉住 geo_places 让位修复）"),
    "scripts/route.py": (
        1008, "路由决策主干（route_query + 三个 _route_by_* 判定器）；"
              "2026-10-17 +10：lang_override 参数（查询操作符 lang: 的入口，"
              "覆盖逻辑本体在 route_lang.apply_lang_override，此处只留传参+"
              "调用+绕磁盘缓存三处——覆盖改变决策，不能与无覆盖查询共享键）；"
              "2026-09-28 拆出 Bangs 解析（route_bangs.py，−26 行）后登记；"
              "同日 +2：breaker.status 读失败 debug 留痕（fail-open 语义不变）；"
              "2026-09-30 +1：routable memo 接线（get_engines 不传 cfg，"
              "route 热路径 2.9×）；"
              "2026-10-02 −16：死源地板/熔断槽位回填/TF-IDF 选源循环"
              "抽到 route_combo（engine_is_dead、build_refill_pool、"
              "backfill_after_breaker、select_tfidf_best 四助手，"
              "route.py 三处死源判定各写一遍的重复随之消除）；"
              "2026-10-03 −1：TF-IDF 弱证据辅源化——tfidf_lead_combo"
              "进 route_combo，route.py 只留调用点与 reason 分叉；"
              "2026-10-14 +6：显式引擎域归因——--engine X 时补 domain 判定，"
              "修复缓存 TTL 一刀切为 general 及 usage_log 统计失真"),
}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _python_files() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.is_file())


def test_no_file_exceeds_hard_limit_unless_grandfathered():
    offenders = []
    for path in _python_files():
        rel = str(path.relative_to(ROOT))
        if rel in GRANDFATHERED:
            continue
        n = _line_count(path)
        if n > HARD_LIMIT:
            offenders.append(f"{rel}: {n} 行（上限 {HARD_LIMIT}）")
    assert not offenders, (
        "文件超过 1000 行且未登记。请先拆模块（见 route_* / search_rank 的做法），"
        "确有必要登记时在 GRANDFATHERED 里写明理由：\n  " + "\n  ".join(offenders))


def test_grandfathered_files_do_not_grow():
    """祖父文件只能减不能增——增长必须显式改这个文件里的数字。"""
    grown = []
    for rel, (ceiling, _reason) in GRANDFATHERED.items():
        path = ROOT / rel
        if not path.is_file():
            grown.append(f"{rel}: 文件不存在（拆走或改名后请删掉本条）")
            continue
        n = _line_count(path)
        if n > ceiling:
            grown.append(f"{rel}: {n} 行 > 登记上限 {ceiling}")
    assert not grown, (
        "祖父文件变大了。正解是拆模块并把上限调低；确要上调请在 GRANDFATHERED "
        "里改数字并说明理由：\n  " + "\n  ".join(grown))


def test_ceilings_are_not_inflated():
    """上限不得高于实际行数：清单一旦虚高，门禁就形同虚设。"""
    inflated = []
    for rel, (ceiling, _reason) in GRANDFATHERED.items():
        path = ROOT / rel
        if not path.is_file():
            continue
        n = _line_count(path)
        if ceiling > n:
            inflated.append(f"{rel}: 上限 {ceiling} > 实际 {n}（请下调到 {n}）")
    assert not inflated, (
        "祖父清单的上限比实际行数高——把上限收到实际行数：\n  " + "\n  ".join(inflated))


@pytest.mark.parametrize("rel", sorted(GRANDFATHERED))
def test_every_grandfathered_entry_has_a_reason(rel):
    """理由不是可选项：没有理由的登记会在下一次审查里被当成噪声删掉。"""
    _ceiling, reason = GRANDFATHERED[rel]
    assert len(reason.strip()) >= 12, f"{rel} 的 reason 太短，写清楚卡在哪"
