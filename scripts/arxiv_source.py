"""arXiv 源码包（e-print）获取与安全解包 — 论文 LaTeX 精读的取数层。

P1 论文深读三能力之一（2026-09-30 P0 实测后立项）：外部论文工具的
get_paper_latex 实测 3.4s 拿到 main tex + 源文件清单，argo 此前无此能力。

安全边界（全部内存态，不落盘解包）：
  1. 只认 arxiv.org 的 https URL（scheme + 域名白名单，拒环回/私有地址）
  2. 下载总量封顶（默认 64MB），成员数封顶（500），单文件解压封顶（2MB）
  3. tar 成员名拒绝绝对路径 / 路径穿越 / 盘符——本模块只读内存，永不写文件
  4. 只收 .tex/.bbl 文本（其余成员只列名不收内容）

缓存：走 fetch 缓存（同 id 1h 内二读零成本），与 pdf 提取缓存同一纪律。
"""
from __future__ import annotations

import gzip
import io
import tarfile
from typing import Any

from net_proxy import open_url
from pdf_extract import CONTENT_WARNING, chunk_text

# 域名白名单（e-print 只在 arxiv.org 主站）
_ALLOWED_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_MAX_DOWNLOAD = 64 * 1024 * 1024        # 压缩包下载上限
_MAX_MEMBERS = 500                       # 成员数上限
_MAX_TOTAL_UNCOMPRESSED = 64 * 1024 * 1024  # 解压总量上限
_MAX_FILE_UNCOMPRESSED = 2 * 1024 * 1024    # 单文件解压上限（.tex/.bbl）
_MAX_CACHE_TEXTS = 2 * 1024 * 1024       # 进缓存的文本总量上限（超出只存清单）
_TEX_EXTS = (".tex", ".bbl")
_DOCUMENTCLASS_RE = None  # 惰性编译（见 _is_main_candidate）


class EprintError(Exception):
    """e-print 取数/解包失败（消息可直接进用户输出）。"""


def _is_main_candidate(name: str, text: str) -> bool:
    global _DOCUMENTCLASS_RE
    if _DOCUMENTCLASS_RE is None:
        import re
        _DOCUMENTCLASS_RE = re.compile(r"\\documentclass")
    return _DOCUMENTCLASS_RE.search(text) is not None


def _validate_url(url: str) -> None:
    """仅允许白名单域名的 http(s) URL（拒环回/私有/保留地址）。"""
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise EprintError(f"仅允许 http(s) URL：{url[:80]}")
    host = (p.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise EprintError(f"域名不在白名单（{'/'.join(sorted(_ALLOWED_HOSTS))}）：{host}")


def _safe_name(name: str) -> bool:
    """tar 成员名安全判定：拒绝对路径/盘符/穿越（只读内存态，防御性双保险）。"""
    if not name or name.startswith(("/", "\\")):
        return False
    if ":" in name.split("/")[0]:  # C:\ 盘符形态
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts


def _pick_main(texts: dict[str, str]) -> str | None:
    """主 tex：含 \\documentclass 的最大文件；没有则最大的 .tex。"""
    candidates = [n for n in texts if n.endswith(".tex")]
    if not candidates:
        return None
    doc_class = [n for n in candidates if _is_main_candidate(n, texts[n])]
    pool = doc_class or candidates
    return max(pool, key=lambda n: len(texts[n]))


def parse_eprint(body: bytes, *, max_members: int = _MAX_MEMBERS,
                 max_total: int = _MAX_TOTAL_UNCOMPRESSED,
                 max_file: int = _MAX_FILE_UNCOMPRESSED) -> dict[str, Any]:
    """解析 e-print 字节流（tar.gz / 单文件 gzip / 纯 tex），全内存态。

    返回 {format, files:[{name,size}], texts:{name:content}, rejected:[...]}，
    非 LaTeX 源包（如误传 PDF）抛 EprintError。
    """
    if body[:5] == b"%PDF-":
        raise EprintError("这是 PDF 不是 LaTeX 源包（e-print 端点才返回源码）")
    tf = None
    try:
        try:
            tf = tarfile.open(fileobj=io.BytesIO(body), mode="r:*")
            members = [(m.name, m.size, tf.extractfile(m)) for m in tf.getmembers()]
            fmt = "tar"
        except tarfile.ReadError:
            if body[:2] == b"\x1f\x8b":  # 单文件 gzip（老论文常见：单个 .tex.gz）
                text = gzip.decompress(body).decode("utf-8", errors="replace")
                name = "main.tex"
                return {"format": "gz", "main_file": name,
                        "files": [{"name": name, "size": len(text)}],
                        "texts": {name: text}, "rejected": []}
            text = body.decode("utf-8", errors="replace")
            return {"format": "plain", "main_file": "main.tex",
                    "files": [{"name": "main.tex", "size": len(text)}],
                    "texts": {"main.tex": text}, "rejected": []}
    except EprintError:
        raise
    except Exception as e:
        raise EprintError(f"源码包解析失败: {str(e)[:120]}")

    files: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    rejected: list[str] = []
    total = 0
    if len(members) > max_members:
        raise EprintError(f"成员数 {len(members)} 超上限 {max_members}，疑似恶意包")
    for raw_name, size, fh in members:
        if not _safe_name(raw_name):
            rejected.append(raw_name)
            continue
        if size > max_file:
            rejected.append(f"{raw_name}（单文件 {size} 超上限）")
            continue
        total += size
        if total > max_total:
            raise EprintError(f"解压总量超上限 {max_total // (1024 * 1024)}MB")
        if not raw_name.lower().endswith(_TEX_EXTS) or fh is None:
            files.append({"name": raw_name, "size": size})
            continue
        data = fh.read(max_file + 1)
        if len(data) > max_file:
            rejected.append(f"{raw_name}（解压超限）")
            continue
        text = data.decode("utf-8", errors="replace")
        files.append({"name": raw_name, "size": size})
        texts[raw_name] = text
    main = _pick_main(texts)
    return {"format": fmt, "files": files, "texts": texts,
            "main_file": main, "rejected": rejected}


def fetch_eprint(arxiv_id: str, use_cache: bool = True) -> dict[str, Any]:
    """拉取并解析 arXiv 源码包；带 fetch 缓存（1h 内二读零成本）。

    返回 parse_eprint 结果 + {"content_warning": ...}；texts 总量超缓存上限时
    只缓存清单并置 texts_omitted（本次调用仍返回全文）。
    """
    aid = arxiv_id.strip()
    if not aid or any(c.isspace() for c in aid):
        raise EprintError(f"arXiv id 形态不对：{aid[:40]}")
    url = f"https://arxiv.org/e-print/{aid}"
    _validate_url(url)

    cache = None
    cache_key = f"arxiv-eprint:{aid}"
    if use_cache:
        try:
            from cache import SearchCache
            cache = SearchCache()
            hit = cache.get_fetch(cache_key)
            if isinstance(hit, dict) and hit.get("files"):
                hit["_cached"] = True
                return hit
        except Exception:
            cache = None

    with open_url(url, timeout=60) as resp:
        body = resp.read(_MAX_DOWNLOAD + 1)
    if len(body) > _MAX_DOWNLOAD:
        raise EprintError(f"源码包超过下载上限 {_MAX_DOWNLOAD // (1024 * 1024)}MB")

    result = parse_eprint(body)
    result["arxiv_id"] = aid
    result["content_warning"] = CONTENT_WARNING
    if cache is not None:
        try:
            payload = {k: v for k, v in result.items() if k != "_cached"}
            if sum(len(t) for t in payload.get("texts", {}).values()) > _MAX_CACHE_TEXTS:
                payload["texts"] = {}
                payload["texts_omitted"] = True
            cache.set_fetch(cache_key, payload)
        except Exception:
            pass  # 缓存写失败不影响本次结果
    return result


def latex_read(source: dict[str, Any], file: str | None = None,
               start: int = 0, max_chars: int | None = None) -> dict[str, Any]:
    """从已解析的源码包按文件分页读取（默认主 tex）；chunk 契约与 pdf 相同。"""
    texts = source.get("texts") or {}
    name = file or source.get("main_file")
    if not name:
        raise EprintError("源码包内没有可读的 .tex 文件")
    if name not in texts:
        if source.get("texts_omitted"):
            raise EprintError("该源码包文本未入缓存（体积超限），请重新拉取")
        available = ", ".join(sorted(texts))[:200] or "（无 .tex 成员）"
        raise EprintError(f"文件 {name} 不在源码包内。可用：{available}")
    seg = chunk_text(texts[name], start, max_chars or 12000)
    return {"content_warning": CONTENT_WARNING, "file": name,
            "main_file": source.get("main_file"), "source_format": source.get("format"),
            **seg}
