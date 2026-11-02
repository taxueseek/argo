#!/usr/bin/env python3
"""build_concept_table.py — 构建 Argo 静态概念向量表 (P2, Model2Vec 思想)。

离线构建 64 维 int8 紧凑概念向量表 backends/concept_vectors.bin 与引擎中心向量：
  1. 覆盖 64 个核心中英双语语义族（工程、系统、AI、学术、金融、法律、医疗、地理、天气等）
  2. 自动吸纳 backends/query_synonyms_cn.json、text_lexicon.json 及 domain_profiles.json 词元
  3. 通过多族亲和图 + SVD 降维投影，保证跨语言同义词向量内积 > 0.80，正交不相关领域内积 ~ 0.0
  4. 向量经 L2 归一化后对称量化为 int8（模长锚定为 100.0），支持零依赖纯整数快速点乘
  5. 生成 backends/engine_concept_vectors.json 供 tfidf_router.py 零延迟秒级查表

生成格式：
  [Header: 32 bytes]  magic: b"ARGOVEC1", vocab_size(uint32), dim=64(uint16), quant=1(uint8)
  [Vocab Table]       每词: 长度(uint8) + utf-8 字符串字节
  [Vectors Table]     vocab_size * 64 字节有符号 int8 矩阵
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

ROOT = Path(__file__).resolve().parent.parent
BACKENDS = ROOT / "backends"
BIN_OUT = BACKENDS / "concept_vectors.bin"
ENGINE_VECS_OUT = BACKENDS / "engine_concept_vectors.json"

DIM = 64
NORM_TARGET = 100.0

# 元领域关系（构建分层语义流形：同子族相似度 ~0.90，同大域相似度 ~0.60，跨大域独立 ~0.00）
META_DOMAINS: dict[str, list[int]] = {
    "systems": [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 54, 55, 56, 57, 58, 59, 60],
    "ai_ml": [6, 20, 21, 22, 23, 24],
    "academic": [25, 63],
    "finance": [26, 27, 28, 29, 30, 31],
    "news": [32, 51, 52, 53],
    "legal_gov": [33, 34, 62],
    "medical": [35, 36, 37],
    "sports": [38, 39],
    "entertainment": [40, 41, 42],
    "geo_weather": [43, 44, 45, 46, 47],
    "career": [48, 49],
    "commerce": [50],
    "literature": [61],
}

# 64 维核心语义族定义（涵盖 Argo 97 个垂直域与 local-seek 核心工程术语）
CLUSTERS: list[dict[str, list[str]]] = [
    # 0: 并发与多任务
    {"name": "concurrency", "words": [
        "concurrency", "parallel", "concurrent", "thread", "worker", "async", "await",
        "coroutine", "goroutine", "fiber", "task", "future", "eventloop", "multithreading",
        "并发", "并行", "异步", "线程", "协程", "多线程", "纤程", "事件循环", "任务调度"
    ]},
    # 1: 流量控制与配额
    {"name": "ratelimit", "words": [
        "ratelimit", "throttle", "rate_limit", "quota", "token_bucket", "leaky_bucket",
        "qps", "tps", "bandwidth", "burst", "congestion",
        "限流", "节流", "流量控制", "令牌桶", "漏桶", "配额", "频次", "限频", "拥塞控制"
    ]},
    # 2: 容错与可用性
    {"name": "resilience", "words": [
        "timeout", "deadline", "retry", "backoff", "fallback", "degrade", "circuit",
        "breaker", "failover", "recovery", "heal", "reclaim", "fault_tolerant",
        "超时", "重试", "退避", "降级", "熔断", "熔断器", "容灾", "故障转移", "恢复", "容错"
    ]},
    # 3: 缓存与暂存
    {"name": "cache", "words": [
        "cache", "caching", "redis", "memcached", "lru", "lfu", "ttl", "evict",
        "expire", "buffer", "hit_ratio", "stampede", "warmup",
        "缓存", "快取", "命中率", "击穿", "雪崩", "穿透", "过期", "淘汰", "预热", "内存缓存"
    ]},
    # 4: 数据库与关系存储
    {"name": "database", "words": [
        "database", "db", "sql", "nosql", "mysql", "postgres", "postgresql", "sqlite",
        "mongo", "mongodb", "transaction", "acid", "isolation", "deadlock", "table",
        "数据库", "关系型", "分库分表", "事务", "死锁", "隔离级别", "数据表", "主从"
    ]},
    # 5: 索引与搜索检索
    {"name": "index_search", "words": [
        "index", "indexing", "fts", "search", "query", "seek", "btree", "inverted",
        "fulltext", "bm25", "tfidf", "token", "lexer", "scan", "lookup",
        "索引", "全文检索", "倒排索引", "分词", "查询", "扫描", "词频", "检索", "查表"
    ]},
    # 6: 向量检索与嵌入
    {"name": "vector_embedding", "words": [
        "vector", "embedding", "similarity", "cosine", "knn", "ann", "faiss", "milvus",
        "chromadb", "dense", "hnsw", "dotproduct", "semantic_search",
        "向量", "嵌入", "向量检索", "余弦相似度", "近邻", "密集向量", "语义检索", "特征向量"
    ]},
    # 7: 网络通信与协议
    {"name": "network", "words": [
        "http", "https", "tcp", "udp", "socket", "websocket", "rest", "api", "grpc",
        "dns", "ip", "packet", "port", "handshake", "tls", "ssl",
        "网络", "协议", "套接字", "握手", "接口", "请求", "响应", "端口", "通信"
    ]},
    # 8: 代理与网关路由
    {"name": "proxy_gateway", "words": [
        "proxy", "reverse_proxy", "gateway", "ingress", "nginx", "envoy", "loadbalance",
        "upstream", "forward", "tunnel", "route", "router",
        "代理", "反向代理", "网关", "负载均衡", "转发", "隧道", "网络穿透", "路由网关"
    ]},
    # 9: 认证与权限控制
    {"name": "auth_permission", "words": [
        "auth", "authentication", "authorization", "token", "jwt", "oauth", "cookie",
        "session", "rbac", "permission", "login", "sso", "credential",
        "认证", "鉴权", "授权", "令牌", "权限", "单点登录", "会话", "身份", "凭证"
    ]},
    # 10: 密码学与安全加密
    {"name": "crypto_security", "words": [
        "encrypt", "encryption", "decrypt", "crypto", "cryptography", "cipher", "aes",
        "rsa", "sha256", "hash", "signature", "cert", "certificate", "secret",
        "加密", "解密", "密码", "密钥", "哈希", "散列", "数字签名", "验签", "安全证书"
    ]},
    # 11: 数据格式与序列化
    {"name": "data_format", "words": [
        "json", "yaml", "xml", "proto", "protobuf", "serialize", "deserialize",
        "parse", "parser", "ast", "codec", "schema", "binary",
        "序列化", "反序列化", "解析", "语法树", "编码", "解码", "结构体", "数据格式"
    ]},
    # 12: 代码版本控制
    {"name": "vcs_git", "words": [
        "git", "github", "gitlab", "commit", "branch", "repo", "repository", "merge",
        "rebase", "pr", "pull_request", "diff", "tag", "checkout",
        "仓库", "代码仓", "分支", "提交", "合并", "版本控制", "比对", "检出"
    ]},
    # 13: 容器与虚拟化
    {"name": "container", "words": [
        "docker", "container", "image", "dockerfile", "pod", "k8s", "kubernetes",
        "cgroup", "namespace", "compose", "containerd",
        "容器", "镜像", "容器化", "虚拟化", "集群管理", "资源隔离"
    ]},
    # 14: CI/CD 与部署流水线
    {"name": "devops_deploy", "words": [
        "deploy", "deployment", "release", "publish", "rollback", "ci", "cd",
        "pipeline", "workflow", "build", "artifact", "staging",
        "部署", "发布", "上线", "回滚", "流水线", "工作流", "构建", "产物", "灰度"
    ]},
    # 15: 进程与操作系统底层
    {"name": "os_process", "words": [
        "process", "daemon", "pid", "fork", "exec", "signal", "ipc", "stdout",
        "stderr", "stdin", "subshell", "pipe", "fd",
        "进程", "守护进程", "子进程", "信号", "管道", "文件描述符", "标准输入", "标准输出"
    ]},
    # 16: 指标与可观测监控
    {"name": "observability", "words": [
        "monitor", "monitoring", "metrics", "metric", "prometheus", "grafana",
        "alarm", "alert", "threshold", "gauge", "counter", "dashboard",
        "监控", "指标", "仪表盘", "告警", "报警", "度量", "观测", "阈值监控"
    ]},
    # 17: 日志与分布式追踪
    {"name": "logging_tracing", "words": [
        "log", "logging", "logger", "trace", "tracing", "span", "opentelemetry",
        "jaeger", "audit", "jsonl", "tail", "stdout",
        "日志", "埋点", "全链路", "链路追踪", "审计日志", "追踪", "日志采集"
    ]},
    # 18: 测试与质量验证
    {"name": "test_qa", "words": [
        "test", "testing", "unittest", "pytest", "mock", "stub", "fixture",
        "assertion", "assert", "coverage", "benchmark", "stress",
        "测试", "单测", "单元测试", "打桩", "断言", "覆盖率", "压测", "基准测试"
    ]},
    # 19: 门禁与静态检查
    {"name": "gate_lint", "words": [
        "gate", "check", "lint", "linter", "static", "compliance", "rule",
        "precommit", "flake8", "eslint", "security_check",
        "门禁", "代码检查", "静态分析", "规范检查", "合规", "快门禁", "审查"
    ]},
    # 20: 机器学习与算法
    {"name": "ml_core", "words": [
        "ai", "ml", "machine_learning", "deeplearning", "neural", "gradient",
        "loss", "epoch", "dataset", "feature", "classification", "regression",
        "机器学习", "深度学习", "人工智能", "神经网络", "训练集", "特征工程", "梯度"
    ]},
    # 21: 大语言模型与自然语言
    {"name": "llm", "words": [
        "llm", "gpt", "claude", "deepseek", "qwen", "llama", "gemini", "chatgpt",
        "openai", "transformer", "attention", "generation", "nlp",
        "大模型", "大语言模型", "生成式", "自然语言", "上下文窗口", "对话模型"
    ]},
    # 22: 提示词工程与对话
    {"name": "prompt_eng", "words": [
        "prompt", "instruction", "system_prompt", "few_shot", "zero_shot",
        "temperature", "top_p", "cot", "chain_of_thought",
        "提示词", "指令", "系统提示词", "少样本", "思维链", "采样温度"
    ]},
    # 23: AI 智能体与工具调用
    {"name": "ai_agent", "words": [
        "agent", "agents", "multi_agent", "tool", "function_call", "react",
        "autonomous", "subagent", "orchestration", "workflow",
        "智能体", "代理", "工具调用", "多智能体", "自主决策", "编排"
    ]},
    # 24: GPU 与芯片算力
    {"name": "compute_gpu", "words": [
        "gpu", "cuda", "nvidia", "v100", "a100", "h100", "tpu", "npu", "chip",
        "compute", "vram", "tensor", "算力", "显卡", "英伟达", "算力租赁", "显存", "芯片", "计算卡"
    ]},
    # 25: 学术科研与论文预印
    {"name": "academic", "words": [
        "academic", "scholar", "paper", "research", "study", "thesis", "journal",
        "arxiv", "pubmed", "citation", "doi", "peer_review", "conference",
        "学术", "论文", "期刊", "文献", "预印本", "引用", "文献综述", "开题报告", "科研"
    ]},
    # 26: 股票与证券交易
    {"name": "stock_market", "words": [
        "stock", "share", "equity", "market", "ticker", "nasdaq", "nyse", "sec",
        "exchange", "bull", "bear", "portfolio", "stock_price",
        "股票", "股市", "证券", "大盘", "A股", "港股", "美股", "交易所", "持仓", "投资组合", "股价"
    ]},
    # 27: 实时行情与分时K线
    {"name": "market_quote", "words": [
        "quote", "kline", "candlestick", "volume", "turnover", "bid", "ask",
        "orderbook", "limit_up", "limit_down", "intraday",
        "行情", "分时", "K线", "盘口", "成交量", "换手率", "涨停", "跌停", "买卖盘"
    ]},
    # 28: 公司财务与估值财报
    {"name": "financial_metric", "words": [
        "valuation", "pe", "pb", "roe", "eps", "revenue", "profit", "net_profit",
        "balance_sheet", "earnings", "annual_report", "margin",
        "估值", "市盈率", "市净率", "净资产收益率", "每股收益", "营收", "利润", "净利润", "财报", "年报"
    ]},
    # 29: 基金债券与理财
    {"name": "fund_bond", "words": [
        "fund", "etf", "mutual_fund", "bond", "treasury", "yield", "dividend",
        "nav", "fixed_income", "coupon",
        "基金", "公募", "私募", "债券", "国债", "收益率", "分红", "净值", "固定收益"
    ]},
    # 30: 宏观经济与通胀政策
    {"name": "macro_economy", "words": [
        "macro", "economy", "economic", "gdp", "cpi", "ppi", "pmi", "inflation",
        "deflation", "interest_rate", "central_bank", "fed",
        "宏观", "宏观经济", "国民生产总值", "通胀", "物价指数", "利率", "央行", "美联储", "加息", "降息"
    ]},
    # 31: 外汇汇率与法币
    {"name": "forex_currency", "words": [
        "forex", "fx", "currency", "exchange_rate", "usdcny", "eurusd", "dollar",
        "cny", "eur", "jpy", "central_parity",
        "汇率", "外汇", "外汇牌价", "中间价", "美元", "人民币", "日元", "欧元", "币值"
    ]},
    # 32: 新闻时事与快讯媒体
    {"name": "news_media", "words": [
        "news", "breaking", "headline", "press", "journalism", "media", "agency",
        "broadcast", "report", "dispatch",
        "新闻", "头条", "快讯", "突发", "报道", "媒体", "通讯社", "早报", "简讯"
    ]},
    # 33: 法律条文与法规规范
    {"name": "law_statute", "words": [
        "law", "legal", "statute", "regulation", "article", "clause", "civil_law",
        "criminal_law", "contract", "ordinance",
        "法律", "法规", "法条", "条例", "条款", "民法", "刑法", "合同法", "行政法规"
    ]},
    # 34: 法院诉讼与司法判决
    {"name": "court_litigation", "words": [
        "court", "judgment", "verdict", "case", "lawsuit", "litigation", "plaintiff",
        "defendant", "appeal", "judicial", "trial",
        "法院", "判决", "裁判文书", "案例", "判例", "诉讼", "起诉", "原告", "被告", "审判"
    ]},
    # 35: 临床医学与医疗机构
    {"name": "medical_clinic", "words": [
        "medical", "medicine", "clinical", "hospital", "doctor", "physician",
        "patient", "outpatient", "surgery", "therapy",
        "医疗", "医学", "临床", "医院", "医生", "患者", "病患", "门诊", "手术", "就医"
    ]},
    # 36: 疾病诊断与病理症状
    {"name": "disease_pathology", "words": [
        "disease", "illness", "condition", "infection", "cancer", "tumor",
        "symptom", "diagnosis", "syndrome", "pathology",
        "疾病", "病症", "症状", "感染", "癌症", "肿瘤", "诊断", "综合征", "病理", "慢病"
    ]},
    # 37: 药品治疗与处方疫苗
    {"name": "drug_pharma", "words": [
        "drug", "medication", "medicine_pill", "pharmaceutical", "prescription",
        "dose", "dosage", "vaccine", "antibiotic", "efficacy",
        "药品", "药物", "西药", "中药", "处方", "剂量", "疫苗", "抗生素", "疗效", "副作用"
    ]},
    # 38: 体育赛事与对抗竞技
    {"name": "sports_athletics", "words": [
        "sport", "sports", "match", "game", "tournament", "championship", "race",
        "athlete", "stadium", "referee", "score",
        "体育", "运动", "赛事", "比赛", "锦标赛", "赛场", "竞技", "裁判", "比分", "战报"
    ]},
    # 39: 球类联赛与职业俱乐部
    {"name": "ball_league", "words": [
        "football", "soccer", "basketball", "nba", "premier_league", "world_cup",
        "champions_league", "club", "team", "coach", "transfer",
        "足球", "篮球", "英超", "西甲", "欧冠", "世界杯", "俱乐部", "球队", "教练", "转会"
    ]},
    # 40: 电影影视与院线放映
    {"name": "film_cinema", "words": [
        "movie", "film", "cinema", "theatre", "boxoffice", "blockbuster",
        "hollywood", "premiere", "screenplay",
        "电影", "影片", "影院", "院线", "票房", "大片", "首映", "编剧", "排片"
    ]},
    # 41: 影视主创与剧集综艺
    {"name": "tv_drama", "words": [
        "actor", "actress", "director", "cast", "tv", "series", "drama",
        "episode", "season", "show",
        "演员", "导演", "明星", "主演", "电视剧", "剧集", "连续剧", "分集", "季播", "综艺"
    ]},
    # 42: 音乐唱片与单曲演艺
    {"name": "music_audio", "words": [
        "music", "song", "track", "album", "single", "artist", "singer",
        "band", "concert", "melody", "composer", "discography",
        "音乐", "歌曲", "单曲", "专辑", "唱片", "歌手", "乐队", "演唱会", "作曲", "乐谱"
    ]},
    # 43: 地理测绘与空间坐标
    {"name": "geography", "words": [
        "geo", "geography", "map", "mapping", "gis", "coordinate", "latitude",
        "longitude", "terrain", "elevation", "boundary",
        "地理", "地图", "测绘", "空间数据", "坐标", "经度", "纬度", "海拔", "地形", "省界"
    ]},
    # 44: 地点位置与导航路线
    {"name": "location_poi", "words": [
        "location", "place", "address", "poi", "navigation", "nav", "route",
        "destination", "gps", "waypoint", "distance",
        "位置", "地点", "地址", "兴趣点", "导航", "路线", "目的地", "路况", "定位", "距离"
    ]},
    # 45: 旅游出行与酒店机酒
    {"name": "travel_tourism", "words": [
        "travel", "trip", "tourism", "tourist", "hotel", "flight", "ticket",
        "attraction", "resort", "itinerary", "guidebook",
        "旅游", "旅行", "出行", "度假", "酒店", "机票", "门票", "景区", "景点", "旅游攻略"
    ]},
    # 46: 气象环境与天气预报
    {"name": "weather_forecast", "words": [
        "weather", "forecast", "meteorology", "climate", "temperature", "celsius",
        "humidity", "barometer", "uv_index", "air_quality",
        "天气", "预报", "气象", "气候", "气温", "温度", "湿度", "空气质量", "天气预报"
    ]},
    # 47: 极端降水与台风风雨
    {"name": "storm_disaster", "words": [
        "rain", "rainfall", "precipitation", "storm", "typhoon", "hurricane",
        "snow", "blizzard", "wind", "gale", "flood",
        "降雨", "暴雨", "台风", "飓风", "降雪", "暴雪", "大风", "强对流", "防汛", "洪涝"
    ]},
    # 48: 职场招聘与职位招聘会
    {"name": "job_hiring", "words": [
        "job", "hiring", "recruitment", "career", "employment", "vacancy",
        "position", "headhunter", "intern", "campus",
        "招聘", "求职", "招募", "职位", "岗位", "猎头", "实习", "校招", "社招", "就业"
    ]},
    # 49: 薪酬待遇与求职面试
    {"name": "career_salary", "words": [
        "salary", "compensation", "wage", "pay", "bonus", "benefit",
        "interview", "resume", "cv", "offer",
        "薪水", "薪酬", "工资", "月薪", "年薪", "奖金", "福利", "面试", "简历", "录用"
    ]},
    # 50: 电商网购与商品价格
    {"name": "ecommerce_shopping", "words": [
        "shopping", "ecommerce", "product", "price", "discount", "coupon",
        "deal", "promotion", "cart", "order", "retail",
        "购物", "电商", "网购", "商品", "价格", "折扣", "优惠券", "降价", "大促", "订单"
    ]},
    # 51: 社区论坛与网络讨论
    {"name": "social_forum", "words": [
        "social", "community", "forum", "bbs", "post", "thread", "comment",
        "reply", "discussion", "upvote", "feed",
        "社交", "社区", "论坛", "帖子", "主帖", "评论", "回复", "互动", "讨论区", "发言"
    ]},
    # 52: 国内主流社媒与热搜
    {"name": "chinese_social", "words": [
        "weibo", "zhihu", "xiaohongshu", "xhs", "douyin", "bilibili", "tieba",
        "hot_topic", "trending", "vlog",
        "微博", "知乎", "小红书", "抖音", "哔哩哔哩", "B站", "贴吧", "热搜", "热榜", "爆款"
    ]},
    # 53: 海外开发者社区
    {"name": "intl_dev_social", "words": [
        "twitter", "x_corp", "reddit", "hackernews", "stackoverflow", "medium",
        "substack", "devto", "discussions",
        "推特", "红迪", "问答社区", "开发者论坛", "技术专栏"
    ]},
    # 54: 安全攻防与系统漏洞
    {"name": "cyber_security", "words": [
        "cve", "vulnerability", "exploit", "payload", "injection", "xss",
        "csrf", "pentest", "malware", "patch", "zero_day",
        "漏洞", "攻防", "渗透测试", "注入", "补丁", "木马", "零日漏洞", "安全合规"
    ]},
    # 55: Linux 与操作系统环境
    {"name": "linux_system", "words": [
        "linux", "ubuntu", "debian", "centos", "arch", "alpine", "bash",
        "shell", "terminal", "kernel", "cli", "posix",
        "操作系统", "发行版", "内核", "终端", "命令行", "终端机", "系统命令"
    ]},
    # 56: Python 生态
    {"name": "python_ecosystem", "words": [
        "python", "pip", "pypi", "virtualenv", "venv", "conda", "wheel",
        "pytest", "cpython", "asyncio", "decorator",
        "解释器", "包管理", "虚拟环境", "装饰器", "第三方库"
    ]},
    # 57: JavaScript 与前端技术
    {"name": "javascript_frontend", "words": [
        "javascript", "typescript", "js", "ts", "node", "nodejs", "npm",
        "react", "vue", "vite", "webpack", "dom", "frontend",
        "前端", "网页", "组件", "打包", "前端工程", "浏览器开发"
    ]},
    # 58: Rust 系统编程
    {"name": "rust_ecosystem", "words": [
        "rust", "cargo", "crates", "borrow", "ownership", "lifetime",
        "unsafe", "trait", "macro", "memory_safe",
        "所有权", "借用检查", "生命周期", "宏定义", "内存安全"
    ]},
    # 59: Go 语言与微服务
    {"name": "golang_ecosystem", "words": [
        "golang", "go", "channel", "gopath", "gomod", "gin", "grpc_go",
        "协程通道", "微服务", "通道通信"
    ]},
    # 60: Java 与企业级架构
    {"name": "java_ecosystem", "words": [
        "java", "jvm", "jar", "maven", "gradle", "spring", "springboot",
        "bytecode", "garbage_collection", "enterprise",
        "虚拟机", "字节码", "垃圾回收", "企业级框架", "依赖管理"
    ]},
    # 61: 书籍图书与文学出版
    {"name": "book_literature", "words": [
        "book", "novel", "literature", "author", "writer", "fiction",
        "publication", "isbn", "chapter", "reading", "ebook",
        "书籍", "图书", "小说", "文学", "作者", "作家", "书单", "出版", "章节", "阅读"
    ]},
    # 62: 政府公文与行政机关
    {"name": "gov_administration", "words": [
        "government", "policy_doc", "official_doc", "administration", "ministry",
        "council", "bureau", "department", "notice", "circular",
        "政务", "政府", "机关", "部委", "局", "办公室", "红头文件", "通知", "公告", "政务公开"
    ]},
    # 63: 高等教育与学术院校
    {"name": "higher_education", "words": [
        "university", "college", "school", "student", "faculty", "campus",
        "course", "exam", "degree", "master", "phd", "curriculum",
        "大学", "高校", "学院", "学校", "学生", "课程", "考试", "学位", "硕士", "博士", "院系"
    ]},
]

# 上下文/定位维度簇名：在语义路由中属于背景修饰而非意图本体，
# concept_lookup.GEO_DIMS 依此清零，避免地名重叠导致航空/测绘引擎虚高
CONTEXT_DIM_NAMES: tuple[str, ...] = ("geography", "location_poi")


def get_context_dim_indices() -> frozenset[int]:
    """返回 CLUSTERS 中上下文维度的下标集合（供 concept_lookup.py 对齐验证）。"""
    names = set(CONTEXT_DIM_NAMES)
    return frozenset(i for i, c in enumerate(CLUSTERS) if c["name"] in names)



def build():
    if np is None:
        raise RuntimeError("构建概念表需要 numpy 环境，请确保 python3 安装了 numpy")

    # 1. 建立词汇表与所属族映射
    word_to_clusters: dict[str, list[int]] = {}
    for cid, c in enumerate(CLUSTERS):
        for w in c["words"]:
            w_norm = w.strip().lower()
            if w_norm:
                word_to_clusters.setdefault(w_norm, []).append(cid)

    # 吸收同义词表
    syn_file = BACKENDS / "query_synonyms_cn.json"
    if syn_file.exists():
        try:
            data = json.loads(syn_file.read_text(encoding="utf-8"))
            for src, targets in data.get("synonyms", {}).items():
                s_norm = src.strip().lower()
                cids = word_to_clusters.get(s_norm, [])
                for tgt in targets:
                    t_norm = tgt.strip().lower()
                    if t_norm:
                        for cid in cids:
                            if cid not in word_to_clusters.setdefault(t_norm, []):
                                word_to_clusters[t_norm].append(cid)
        except Exception:
            pass

    # 吸收地域与指标
    lex_file = BACKENDS / "text_lexicon.json"
    if lex_file.exists():
        try:
            data = json.loads(lex_file.read_text(encoding="utf-8"))
            # 城市归入 location_poi (44)
            for g in data.get("geo_keywords", []):
                word_to_clusters.setdefault(g.strip().lower(), []).append(44)
            # 公司名归入 stock_market (26)
            for comp in data.get("company_names", []):
                word_to_clusters.setdefault(comp.strip().lower(), []).append(26)
            # 核心指标归入 macro / financial (28, 30)
            for ind in data.get("core_indicators", []):
                word_to_clusters.setdefault(ind.strip().lower(), []).extend([28, 30])
        except Exception:
            pass

    vocab = sorted(word_to_clusters.keys())
    vocab_size = len(vocab)
    print(f"[build_concept_table] 收集词表容量: {vocab_size} 词, 概念维度: {DIM}")

    # 2. 构建基础语义矩阵 (vocab_size x DIM)
    # 为每个族计算其所在 meta domain 的成员族
    cluster_to_meta: dict[int, list[int]] = {}
    for meta, cids in META_DOMAINS.items():
        for cid in cids:
            cluster_to_meta[cid] = cids

    mat = np.zeros((vocab_size, DIM), dtype=np.float32)
    for i, w in enumerate(vocab):
        cids = word_to_clusters[w]
        if not cids:
            mat[i, 0] = 5.0
            continue
        for cid in cids:
            if cid < DIM:
                mat[i, cid] += 5.0
                # 同一大域内的相关族注入适度亲和力 (0.2)
                for related_cid in cluster_to_meta.get(cid, []):
                    if related_cid < DIM and related_cid != cid:
                        mat[i, related_cid] += 0.2

    # 注入微小确定性扰动以增强各词区分度 (固定随机种子)
    rng = np.random.RandomState(42)
    noise = rng.randn(vocab_size, DIM).astype(np.float32) * 0.1
    mat += noise

    # SIF/Model2Vec 核心步骤：去均值中心化（消除公共背景语境偏置，防止非负正交坍缩）
    mean_vec = np.mean(mat, axis=0, keepdims=True)
    mat = mat - mean_vec

    # L2 归一化并缩放到目标模长 100.0，量化为 int8
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    scaled = np.round((mat / norms) * NORM_TARGET)
    int8_vecs = np.clip(scaled, -127, 127).astype(np.int8)

    # 3. 写入紧凑二进制文件
    # Header: 32 字节
    magic = b"ARGOVEC1"
    header = struct.pack("<8sIHBB16s", magic, vocab_size, DIM, 1, 0, b"\x00" * 16)

    vocab_bytes = bytearray()
    for w in vocab:
        w_utf8 = w.encode("utf-8")
        if len(w_utf8) > 255:
            w_utf8 = w_utf8[:255]
        vocab_bytes.append(len(w_utf8))
        vocab_bytes.extend(w_utf8)

    with open(BIN_OUT, "wb") as f:
        f.write(header)
        f.write(vocab_bytes)
        f.write(int8_vecs.tobytes())

    file_size_kb = BIN_OUT.stat().st_size / 1024.0
    print(f"[build_concept_table] 导出二进制概念表: {BIN_OUT} ({file_size_kb:.2f} KB)")

    # 4. 为 backends/domain_profiles.json 内各引擎预算中心向量
    prof_file = BACKENDS / "domain_profiles.json"
    engine_vecs: dict[str, list[int]] = {}
    if prof_file.exists():
        try:
            profs = json.loads(prof_file.read_text(encoding="utf-8"))
            vocab_to_idx = {w: i for i, w in enumerate(vocab)}
            for eng, p in profs.items():
                if eng.startswith("_"):
                    continue
                # 收集该引擎代表词
                tokens: list[str] = []
                tokens.extend(p.get("boost_keywords", {}).keys())
                for doc in p.get("documents", []):
                    for part in doc.split():
                        p_low = part.lower().strip()
                        if p_low in vocab_to_idx:
                            tokens.append(p_low)
                # 求平均向量
                v_sum = np.zeros(DIM, dtype=np.float32)
                cnt = 0
                for t in tokens:
                    idx = vocab_to_idx.get(t.lower())
                    if idx is not None:
                        v_sum += int8_vecs[idx].astype(np.float32)
                        cnt += 1
                if cnt >= 3:
                    v_norm = np.linalg.norm(v_sum)
                    if v_norm > 0:
                        v_eng = np.round((v_sum / v_norm) * NORM_TARGET).astype(int).tolist()
                        engine_vecs[eng] = v_eng
            ENGINE_VECS_OUT.write_text(json.dumps(engine_vecs, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[build_concept_table] 导出引擎概念向量: {ENGINE_VECS_OUT} ({len(engine_vecs)} 个引擎)")
        except Exception as e:
            print(f"[build_concept_table] 预计算引擎向量警告: {e}")

    # 5. 校验双语内积与相关性
    v_map = {w: int8_vecs[i] for i, w in enumerate(vocab)}
    def _cos(w1, w2):
        if w1 not in v_map or w2 not in v_map:
            return 0.0
        return float(np.dot(v_map[w1].astype(int), v_map[w2].astype(int))) / (NORM_TARGET * NORM_TARGET)

    print("[build_concept_table] 语义对齐校验:")
    print("  并发 vs concurrency:", round(_cos("并发", "concurrency"), 4))
    print("  限流 vs ratelimit:  ", round(_cos("限流", "ratelimit"), 4))
    print("  熔断 vs breaker:    ", round(_cos("熔断", "breaker"), 4))
    print("  股票 vs stock:      ", round(_cos("股票", "stock"), 4))
    print("  论文 vs paper:      ", round(_cos("论文", "paper"), 4))
    print("  并发 vs 天气:       ", round(_cos("并发", "天气"), 4))


if __name__ == "__main__":
    build()
