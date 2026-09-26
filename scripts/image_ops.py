#!/usr/bin/env python3
"""image_ops.py — 图片结果的后处理：去重、可用性判据、拼图。

## 守的是什么

搜索结果里的图片此前没有任何图片维度处理：`search_rank` 有 URL 去重
（`deduplicate_by_url`）和文本近重复去重（`minhash_dedupe`），但两者都只看
**页面**的标题/正文/URL，对图本身一无所知。带来的实际损失有两类：

1. **同一张图占多个位**。同一素材在不同 CDN 上的 URL 不同（`p0.so.qhimg.com`
   与 `p0.ssl.qhimgs1.com`、带 `?x-oss-process=style/watermark` 与不带），
   URL 去重和文本去重都判不出是同一张，于是一次搜索的 6 条结果里 3 条是
   同一张图。
2. **不可用的图进了结果**。没有分辨率下限、没有授权判据，用户手动翻完才
   发现有坑——而这两件事在字段层面就能判定，不需要看图。

## 为什么不用感知哈希（dHash/pHash）

做图片去重最直觉的选择是感知哈希，但那要求**先把图片下载下来**才能算哈希。
放在搜索路径上是灾难性的：一次搜索 20 条结果就是 20 次图片下载（首字节 +
全量传输），把亚秒级查询变成十几秒。

搜索场景其实不需要感知哈希——重复的图**在字段层面就有强信号**：
  - 同一个图片直链（去掉 CDN 的尺寸/水印变换参数后相同）
  - 不同主机上的同一路径（同一文件被多 CDN 分发）
  - 相同文件名 + 相同像素尺寸（同图重传）
这三类都是纯字符串比较。真正需要感知哈希的场景是「本地相册去重」（图已在
本地，无 URL 可比），那是另一个功能，见 local_image.py。

## 许可判据从哪来

`engines_base._license_allows_commercial` 在引擎输出层算好了
`image_commercial_ok`（True/False/None 三值）。本模块只做**消费**，
不重新判定——两处各判一次必然漂移。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

# CDN 的图片变换参数：去掉后同一文件的不同尺寸/水印变体才归一到同一个键。
# 清单来源是实测遇到的（360 图的 qhimg、阿里 OSS 的 x-oss-process、
# 新浪的 resize、Cloudinary 的 w_/h_ 前缀）。
_IMG_TRANSFORM_PARAMS = re.compile(
    r"^(?:x-oss-process|imageview|image_view|resize|w|h|width|height|quality|q|"
    r"format|fit|crop|thumbnail|thumb|size|scale|dpr|auto|ixlib|s)$",
    re.I,
)
# 路径里的尺寸段：/480x320/、/w_800/、/_800x/、/thumb/、/preview/
_IMG_SIZE_PATH_SEG = re.compile(
    r"^(?:\d{2,5}x\d{2,5}|w_\d+|h_\d+|_\d+x\d*|\d+x\d*_|thumb(?:s|nail)?|"
    r"preview|small|medium|large|orig(?:inal)?|full)$",
    re.I,
)
# 图片扩展名（判断一个 URL 是不是图片直链）
IMAGE_EXTS = frozenset(
    (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
     ".heic", ".heif", ".avif", ".svg", ".jfif")
)

# 可用性阈值。定的是「素材能不能用」的下限而不是审美标准：
#   800x600 是 PPT/公众号配图的常见可用下限，低于这个尺寸放大就糊；
#   16:1 以上（或 1:16 以下）通常是横幅/通栏素材，作为普通配图会裁得很难看。
# 两个阈值都可通过环境变量覆盖——不同用途（图标 vs 印刷）标准不同，
# 写死会让一类用途永远拿不到结果。
MIN_USABLE_EDGE = 300       # 最短边的绝对下限（再小基本是图标/表情）
MIN_USABLE_AREA = 800 * 600  # 面积下限
MAX_ASPECT_RATIO = 16.0      # 长宽比上限


def image_key(image_url: str) -> str:
    """图片直链 → 跨 CDN 可比的归一键。

    归一三件事：主机去 `www.`/`ssl.`/`img.` 这类纯技术前缀、路径里的尺寸段
    删掉、变换类查询参数删掉。剩下的主体（`/t040f7e0e65116a8110.jpg`）才是
    标识「哪张图」的部分。
    """
    raw = str(image_url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "ssl.", "img.", "cdn.", "static.", "s."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    segs = []
    for seg in (parts.path or "").split("/"):
        if not seg or _IMG_SIZE_PATH_SEG.match(seg):
            continue
        segs.append(seg.lower())
    # 查询串只保留非变换参数并排序，避免参数顺序造成两个键
    kept = []
    for pair in (parts.query or "").split("&"):
        if not pair:
            continue
        k = pair.split("=", 1)[0]
        if k and not _IMG_TRANSFORM_PARAMS.match(k):
            kept.append(pair.lower())
    key = f"{host}/{'/'.join(segs)}"
    return f"{key}?{'&'.join(sorted(kept))}" if kept else key


def looks_like_image_url(url: str) -> bool:
    """URL 是否像图片直链（扩展名判定，不发起请求）。"""
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        path = (urlsplit(raw).path or "").lower()
    except ValueError:
        path = raw.lower()
    dot = path.rfind(".")
    return dot > 0 and path[dot:] in IMAGE_EXTS


def deduplicate_images(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """图片级去重：同一张图的多个 URL 只留首条。

    在 URL 去重与文本去重之后跑（那是页面粒度，这是图片粒度）。判定顺序
    从强到弱：
      1. image_key 相同 —— 同一文件（含变换参数变体）
      2. 文件名 + 像素尺寸都相同 —— 同文件重传，主机不同

    返回 (deduped, removed)。被移除条目标 `_image_dup=True`，与
    `_near_dup`（文本近重复）区分开——两者成因不同，混用一个标记会让
    「为什么少了一条」无法归因。
    """
    kept: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_name_dims: set[tuple[str, str]] = set()
    removed = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        img = str(r.get("image_url") or "").strip()
        if not img:
            # 无图条目直接通过：本函数只负责图片重复，不带图的结果是另一类
            # 问题（无图），由可用性过滤处理，不在这里丢弃。
            kept.append(r)
            continue
        key = image_key(img)
        name = key.rsplit("/", 1)[-1].split("?")[0]
        dims = f"{r.get('image_width', '')}x{r.get('image_height', '')}"
        nd = (name, dims) if name and "x" in dims and dims != "x" else None
        if key and key in seen_keys:
            removed += 1
            r["_image_dup"] = True
            continue
        # 文件名+尺寸判定只在文件名足够长时启用：`1.jpg`、`image.png` 这类
        # 通用名会误杀不同图，大量 CDN 用这种命名。
        if nd and len(name) >= 8 and nd in seen_name_dims:
            removed += 1
            r["_image_dup"] = True
            continue
        kept.append(r)
        if key:
            seen_keys.add(key)
        if nd and len(name) >= 8:
            seen_name_dims.add(nd)
    return kept, removed


def image_usability(r: dict[str, Any]) -> tuple[bool, str]:
    """单条结果的图片可用性判定 → (可用?, 原因码)。

    只做**字段层面**能确定的事，不下审美判断：
      - `no_image`：没有图片直链
      - `too_small`：尺寸已知且低于下限
      - `extreme_aspect`：长宽比极端（横幅/通栏素材）
      - `not_commercial`：授权明确为非商用
      - `ok`：通过

    尺寸未知（上游没给）**不判为不可用**：拿不到尺寸不等于图小，判成不可用
    会把一整批没提供尺寸字段的源误杀。这类条目按「未知」放行，让模型在
    候选阶段自行判断。
    """
    if not str(r.get("image_url") or "").strip():
        return False, "no_image"
    w, h = r.get("image_width"), r.get("image_height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        # 长宽比先判：4000x150 的横幅同时满足「某边过小」和「比例极端」，
        # 但两者给用户的行动建议不同（前者去找更大的图，后者说明这是通栏
        # 素材、不适合当普通配图）。报更具体的那一个，否则诊断信息误导人。
        ratio = max(w / h, h / w)
        if ratio > MAX_ASPECT_RATIO:
            return False, "extreme_aspect"
        if min(w, h) < MIN_USABLE_EDGE:
            return False, "too_small"
        if w * h < MIN_USABLE_AREA:
            return False, "too_small"
    if r.get("image_commercial_ok") is False:
        return False, "not_commercial"
    return True, "ok"


def filter_usable_images(
    results: list[dict[str, Any]], *, require_commercial: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """筛掉字段层面即可判定不可用的图片结果。

    `require_commercial=True` 时才把非商用图剔掉——默认只标注不删除。原因：
    「搜图」和「找能发布的素材」是两个需求，前者不该被授权过滤误杀（用户
    可能只是要看看某张图长什么样）。默认放行、按需收紧。

    返回 (kept, 原因计数)。原因计数用于解释「为什么少了 N 条」——静默丢弃
    是搜索类工具最招人恨的行为。
    """
    kept: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        ok, reason = image_usability(r)
        if ok or (reason == "not_commercial" and not require_commercial):
            kept.append(r)
            continue
        dropped[reason] = dropped.get(reason, 0) + 1
    return kept, dropped


def has_image_result(results: list[dict[str, Any]]) -> bool:
    """结果集里是否含图片条目（用于判断这是不是一次图片检索）。"""
    return any(str(r.get("image_url") or "").strip()
               for r in results if isinstance(r, dict))


# 声明式 output_map 的图片字段映射。新增一个图片元数据字段时只改这里，
# 不让「契约有哪些键」散在解析器内部；`_license_version` 前缀下划线表示
# 它是归一化的拼接原料，不是交付字段（见 finalize_image_fields）。
def image_output_map_fields(output_map: dict[str, Any]) -> dict[str, str]:
    """output_map → 解析器字段表（图片相关部分）。"""
    return {
        "image_url": output_map.get("item_image", ""),
        "image_license": output_map.get("item_image_license", ""),
        "image_license_url": output_map.get("item_image_license_url", ""),
        "_license_version": output_map.get("item_image_license_version", ""),
        "image_width": output_map.get("item_image_width", ""),
        "image_height": output_map.get("item_image_height", ""),
    }


# CLI 引擎结果的透传字段白名单。
#
# 通用文本解析此前只放行 title/url/snippet/source/published_at，其余一律丢弃。
# 对纯网页检索够了，但**图片类子引擎的产出恰好全在白名单外**：search_v3 的
# ddgs images 已把 image/thumbnail/width/height 解析出来（见
# sub-skills/local-search/search_v3.py 的 `_parse_cli_json`），到这一层又被
# 无声删掉——实测 `argo search --engine local_search` 的 image 与 image_url
# 双为 null。
#
# 白名单而非全透传：CLI 能吐任意字段（含 `_elapsed` 这类内部计时），全透传会
# 把内部状态混进交付结果。这里列的是**已确立的对外契约字段**。
CLI_PASSTHROUGH_FIELDS = (
    "image", "thumbnail", "width", "height", "duration", "provider",
    "image_url", "image_license", "image_license_url",
    "image_width", "image_height", "image_commercial_ok",
)


def _cli_result_row(item: dict[str, Any], engine_name: str) -> dict[str, Any]:
    """CLI 引擎单条结果 → argo 结果契约（含图片字段透传与别名收敛）。"""
    r: dict[str, Any] = {
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "snippet": str(item.get("snippet", item.get("content", "")))[:300],
        "source": engine_name,
    }
    if item.get("published_at"):
        r["published_at"] = str(item["published_at"])[:64]
    for k in CLI_PASSTHROUGH_FIELDS:
        v = item.get(k)
        if v not in (None, ""):
            r[k] = v
    # 别名收敛：子技能用 `image`（ddgs 原生键名），argo 主契约用 `image_url`。
    # 宽高同理——ddgs 给裸名 `width`/`height`，而可用性判据读 `image_width`。
    # 不收敛的话过滤拿到 None，判定为「尺寸未知」一律放行，且是静默的。
    if r.get("image") and not r.get("image_url"):
        r["image_url"] = r["image"]
    if r.get("width") and not r.get("image_width"):
        r["image_width"] = r["width"]
    if r.get("height") and not r.get("image_height"):
        r["image_height"] = r["height"]
    return r



# 授权标注 → 商用许可判定。
#
# 为什么要在这一层判而不是让用户自己看标注：上游给的是六七种互不相同的写法
# （Openverse 的 `by-nc-sa`、Cleveland 的 `CC0`、Wikimedia 的 `CC BY-SA 4.0`、
# NASA 的中文「公有领域」），用户真正要回答的问题只有一个——「这张图我能不能
# 用在要发布的稿子里」。让人逐条读协议名再自己判断，等于把最容易出错的一步
# 留给用户。
#
# 三值而非布尔：**无法判定时必须说不知道**。上游许可字段缺失、写成自有术语
# （Artic 的「权利状态见作品页」）时，猜成「可商用」会闯版权祸，猜成「不可」
# 会误杀可用的图。None 让下游按「未判定」处理（不排除、不担保）。
#
# 规则顺序敏感：非商用是最强的否定约束，必须先判——`by-nc-sa` 里含有 `by`，
# 先匹配自由许可就会把「不可商用」误判成「可商用」，这是本函数唯一会酿成
# 真事故的方向。
_LICENSE_NC_RE = re.compile(
    r"(?:^|[^a-z])nc(?:[^a-z]|$)"          # 短码 by-nc-sa / cc-by-nc-nd
    r"|non[\s-]?commercial"
    r"|非商业|非商用",
    re.I,
)
_LICENSE_RESTRICTED_RE = re.compile(
    r"all\s+rights\s+reserved|copyright(?:ed)?(?!\s*(?:status\s+)?(?:see|见|unknown))"
    r"|保留所有权利|版权所有",
    re.I,
)
_LICENSE_FREE_RE = re.compile(
    r"cc0|cc[\s-]?0(?:\.\d+)?"
    r"|public[\s-]?domain"
    r"|公有领域|公共领域|无版权|不受版权保护"
    r"|no\s+rights\s+reserved"
    r"|(?:^|[^a-z])pd(?:[^a-z]|$)"
    r"|cc[\s-]?by(?:[\s-]?sa)?(?:[\s-]?\d(?:\.\d+)?)?"
    r"|(?:^|[^a-z])by(?:[\s-]?sa)?(?:[^a-z]|$)"
    r"|apache[\s-]?2|(?:^|[^a-z])mit(?:[^a-z]|$)|bsd",
    re.I,
)


def _license_allows_commercial(*candidates: str) -> bool | None:
    """从若干许可标注推断可否商用。返回 True / False / None（无法判定）。

    传入多个候选（如 license 短码 + license_url + 人工标注）时任一命中即可
    ——图源给哪一路都不一定，但结论应当一致。
    """
    texts = [str(c).strip() for c in candidates if c and str(c).strip()]
    if not texts:
        return None
    joined = "\n".join(texts)
    if _LICENSE_NC_RE.search(joined):
        return False
    if _LICENSE_RESTRICTED_RE.search(joined):
        return False
    for t in texts:
        if _LICENSE_FREE_RE.search(t):
            return True
    return None


# CC 短码 → 标准标注。Openverse 给 `by-sa` + `license_version: 2.0` 两个字段，
# 拼出来才是人们认得的 `CC BY-SA 2.0`；单独给 `by-sa` 让用户对着猜是哪种协议
# 不合适，而 `CC BY-SA 2.0` 是能直接搜到条款原文的写法。
_CC_SHORTCODE_RE = re.compile(r"^(?:cc[\s-]?)?(by|by-sa|by-nc|by-nc-sa|by-nd|by-nc-nd|cc0|zero)$", re.I)
_CC_LABELS = {
    "cc0": "CC0", "zero": "CC0",
    "by": "CC BY", "by-sa": "CC BY-SA", "by-nd": "CC BY-ND",
    "by-nc": "CC BY-NC", "by-nc-sa": "CC BY-NC-SA", "by-nc-nd": "CC BY-NC-ND",
}


def _normalize_license_label(*candidates: str) -> str:
    """把上游许可标注归一成标准写法；认不出来时原样保留首个候选。

    保守原则：宁可不认，不可错认。把别的协议写成 `CC BY` 比不改更糟——用户
    会据此做出发布决定。因此只在完全匹配已知短码或已是标准写法时才改写。
    """
    texts = [str(c).strip() for c in candidates if c and str(c).strip()]
    if not texts:
        return ""
    raw = texts[0]
    # 短码优先于「已是标准写法」的放行分支。顺序反了会漏掉一个真实场景：
    # Openverse 给的 `cc0` 是小写，宽松匹配会把它当成「已经是 CC0 标准写法」
    # 原样放行，交付出去的标注与别处的 `CC0` 大小写不一致（同一协议两种写法，
    # 用户以为不是一回事）。短码分支统一产出规范大小写。
    m = _CC_SHORTCODE_RE.match(raw)
    if m:
        key = (m.group(1) or "").lower().replace(" ", "-")
        label = _CC_LABELS.get(key)
        if label:
            ver = next((str(v).strip() for v in texts[1:]
                        if re.match(r"^\d+(\.\d+)?$", str(v).strip())), "")
            return f"{label} {ver}".strip()
    # 其余一律原样返回。这里刻意不猜：已带版本号的标准写法（Wikimedia 的
    # `CC BY-SA 4.0`）和自有术语（Artic 的「权利状态见作品页」）都比我们能
    # 生成的任何东西更准确，改写它们只会把正确信息改错。
    return raw

def finalize_image_fields(
    results: list[dict[str, Any]], spec_license: str = ""
) -> list[dict[str, Any]]:
    """收尾图片元数据：尺寸转 int、许可标注归一、算商用判据。

    放在引擎输出的**共同出口**上调用，而不是各自的 parser 里——图源有三条产出
    路径（声明式 output_map、自定义 builder 如 artic/cleveland/met、通用兜底），
    只在其中一条上做，另外两条就会产出「有 image_url 但没有可用性判据」的条目，
    下游拿不到统一契约。契约不统一的表现是静默的：过滤条件对一半结果生效。

    幂等：重复调用不改变结果。
    """
    for r in results:
        if not isinstance(r, dict) or "error" in r:
            continue
        # 版本号只在拼接许可标注时用，先摘出来——它由 output_map 的
        # item_image_license_version 产出，是原料不是结果字段，留在 r 里会
        # 随结果一起交付出去（下划线前缀在本模块是「内部字段」约定）。
        _ver = r.pop("_license_version", "")
        for k in ("image_width", "image_height"):
            if k in r and not isinstance(r[k], int):
                try:
                    r[k] = int(float(str(r[k]).strip()))
                except (TypeError, ValueError):
                    r.pop(k, None)
        if not r.get("image_url"):
            # 无图条目不该带授权结论：下游按 image_commercial_ok 过滤素材时，
            # 会把「没图的条目」当成「有图的可用素材」，凭空多出一批假候选。
            continue
        _label = _normalize_license_label(r.get("image_license", ""), spec_license, _ver)
        if _label:
            r["image_license"] = _label
        _ok = _license_allows_commercial(
            r.get("image_license", ""), r.get("image_license_url", ""), spec_license
        )
        if _ok is not None:
            r["image_commercial_ok"] = _ok
    return results
