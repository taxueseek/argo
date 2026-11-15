#!/usr/bin/env python3
"""engines_schema_drift.py — 声明式引擎 Spec 结构漂移只读诊断。

职责：
  - 检测三层契约漂移：Envelope (业务错误封套)、Container (items 列表)、Field (title/url/snippet)
  - 启发式推荐 output_map 补丁与样本密钥快照（仅预览，不自动改写 YAML 真源）
  - 内存影子验证建议补丁是否能成功提取 title 与 url
"""

from __future__ import annotations

from typing import Any

from engines_fields import _dig, _format_url_template, extract_items

_TITLE_SYNONYMS = (
    "title", "name", "headline", "subject", "text", "label",
    "caption", "item_title", "artwork_title", "track_name", "display_name",
)
_URL_SYNONYMS = (
    "url", "link", "web_url", "html_url", "href", "uri",
    "source_url", "landing_page", "target_url", "permalink", "page_url", "site_url",
)
_SUMMARY_SYNONYMS = (
    "snippet", "summary", "description", "abstract", "body",
    "content", "desc", "overview", "intro", "tombstone", "details", "comment",
)
_PUB_SYNONYMS = (
    "published_at", "created_at", "pubdate", "pub_date", "date",
    "time", "timestamp", "premiered", "first_air_date", "release_date",
)


def _flatten_item(item: Any, prefix: str = "", depth: int = 0, max_depth: int = 3) -> dict[str, Any]:
    """将嵌套字典/列表展平为点分路径键值字典（列表只展开前 2 条）。"""
    flat: dict[str, Any] = {}
    if depth > max_depth or not isinstance(item, (dict, list)):
        return flat
    pairs = item.items() if isinstance(item, dict) else enumerate(item[:2])
    for k, v in pairs:
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, (dict, list)) and depth < max_depth:
            flat.update(_flatten_item(v, path, depth + 1, max_depth))
        else:
            flat[path] = v
    return flat


def _score_container(data: list) -> float:
    score = 0.5 + min(0.3, len(data) * 0.02)
    keys = {str(k).lower() for k in data[0].keys()}
    if any(k in keys for k in ("title", "name", "id", "url", "link")):
        score += 0.2
    return score


def _find_candidate_containers(data: Any, prefix: str = "", depth: int = 0, max_depth: int = 4) -> list[tuple[str, list, float]]:
    """递归搜索全部字典列表，并按结构质量降序返回。"""
    candidates: list[tuple[str, list, float]] = []
    if depth > max_depth:
        return candidates
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            candidates.append((prefix or ".", data, _score_container(data)))
        return candidates
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                candidates.append((path, v, _score_container(v)))
            elif isinstance(v, dict) and depth < max_depth:
                candidates.extend(_find_candidate_containers(v, path, depth + 1, max_depth))
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates


def _match_text_field(flat: dict[str, Any], primary: tuple[str, ...], synonyms: tuple[str, ...],
                      *, high: float, mid: float, min_len: int = 1) -> tuple[str | None, Any, float]:
    for path, val in flat.items():
        if not isinstance(val, (str, int)):
            continue
        s = str(val).strip()
        if len(s) < min_len:
            continue
        seg = path.split(".")[-1].lower()
        if seg in primary:
            return path, val, high
        if seg in synonyms:
            return path, val, mid
    return None, None, 0.0


def _match_best_field(target_kind: str, flat: dict[str, Any]) -> tuple[str | None, Any, float]:
    """根据目标类型和扁平键树，启发式匹配最优路径。"""
    if target_kind == "title":
        return _match_text_field(
            flat, ("title", "name", "headline", "artwork_title"), _TITLE_SYNONYMS, high=0.95, mid=0.85,
        )
    if target_kind == "summary":
        return _match_text_field(
            flat, ("snippet", "summary", "description", "abstract"), _SUMMARY_SYNONYMS, high=0.90, mid=0.80, min_len=5,
        )
    if target_kind == "url":
        cands: list[tuple[str, Any, float]] = []
        for path, val in flat.items():
            s = str(val or "").strip()
            if not s.startswith(("http://", "https://", "/")):
                continue
            seg = path.split(".")[-1].lower()
            score = 0.6 + (0.3 if seg in _URL_SYNONYMS else 0.0)
            if s.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                score -= 0.4
            cands.append((path, val, score))
        cands.sort(key=lambda x: x[2], reverse=True)
        if cands and cands[0][2] >= 0.5:
            return cands[0]
        for path, val in flat.items():
            if path.split(".")[-1].lower() in _URL_SYNONYMS and val:
                return path, val, 0.75
    return None, None, 0.0


def _shadow_check(raw_data: Any, spec_or_omap: dict[str, Any], patch: dict[str, str]) -> tuple[bool, str]:
    omap = spec_or_omap.get("output_map") if isinstance(spec_or_omap.get("output_map"), dict) else spec_or_omap
    merged = {**(omap or {}), **patch}
    items = extract_items(raw_data, merged.get("items", ""))
    if not items or not isinstance(items[0], dict):
        return False, f"容器路径 '{merged.get('items', '')}' 未提取到字典条目"
    title_k = merged.get("item_title", "title")
    url_k = merged.get("item_url", "url")
    url_tmpl = spec_or_omap.get("url_template") if isinstance(spec_or_omap, dict) else None
    checked = 0
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        t = _dig(it, title_k) if title_k else None
        u = (_dig(it, url_k) if url_k else None) or (_format_url_template(url_tmpl, it) if url_tmpl else None)
        if not t or not str(t).strip():
            return False, f"样本标题为空 (字段 '{title_k}')"
        if not u or not str(u).strip():
            return False, f"样本 URL 为空 (字段 '{url_k}')"
        checked += 1
    return (True, f"成功对 {checked} 条样本完成 title 与 url 提取验证") if checked else (False, "无有效样本条目")


def diagnose_schema_drift(raw_data: Any, spec_or_output_map: dict[str, Any]) -> dict[str, Any]:
    """上游数据源结构漂移只读诊断（Envelope / Container / Field 三层对拍）。"""
    omap = spec_or_output_map.get("output_map") if isinstance(spec_or_output_map.get("output_map"), dict) else spec_or_output_map
    if not isinstance(omap, dict):
        omap = {}

    cur_items = omap.get("items", "")
    cur_fields = {
        "item_title": (omap.get("item_title", "title"), "title", "标题"),
        "item_url": (omap.get("item_url", "url"), "url", "链接"),
        "item_summary": (omap.get("item_summary", "snippet"), "summary", "摘要"),
    }

    if not isinstance(raw_data, (dict, list)):
        return {
            "ok": False, "drift_detected": False, "stage": "envelope", "confidence": 0.0,
            "issues": [f"响应非 JSON 结构 (类型: {type(raw_data).__name__})"], "suggested_patch": {},
        }
    if isinstance(raw_data, dict):
        err_val = raw_data.get("error") or raw_data.get("err_msg") or raw_data.get("message")
        if (err_val and not any(raw_data.get(k) for k in ("data", "results", "items"))
                and not _find_candidate_containers(raw_data)):
            return {
                "ok": False, "drift_detected": False, "stage": "envelope", "confidence": 0.0,
                "issues": [f"上游返回业务错误封套: {err_val}"], "suggested_patch": {},
            }

    issues: list[str] = []
    container_drift: dict[str, Any] | None = None
    field_drifts: dict[str, Any] = {}
    suggested_patch: dict[str, str] = {}

    items = extract_items(raw_data, cur_items)
    if not items or not isinstance(items[0], dict):
        candidates = _find_candidate_containers(raw_data)
        if not candidates:
            return {
                "ok": False, "drift_detected": False, "stage": "container", "confidence": 0.0,
                "issues": [f"未在响应中发现任何字典列表容器 (当前 items='{cur_items}')"], "suggested_patch": {},
            }
        best_path, items, score = candidates[0]
        container_drift = {"from": cur_items, "to": best_path, "count": len(items), "score": score}
        issues.append(f"容器路径失效: '{cur_items}' 未提取到条目，候选路径 '{best_path}' (包含 {len(items)} 条)")
        suggested_patch["items"] = best_path

    sample = items[0]
    flat = _flatten_item(sample, max_depth=3)
    for map_key, (cur_key, kind, label) in cur_fields.items():
        if not cur_key or _dig(sample, cur_key):
            continue
        cand_p, cand_v, cand_s = _match_best_field(kind, flat)
        if cand_p and cand_p != cur_key:
            field_drifts[map_key] = {"from": cur_key, "to": cand_p, "sample": str(cand_v)[:50], "score": cand_s}
            issues.append(f"{label}字段失效: '{cur_key}' 为空 -> 候选 '{cand_p}' (值: {cand_v!r})")
            suggested_patch[map_key] = cand_p

    drift_detected = bool(container_drift or field_drifts)
    stage = "field" if field_drifts else ("container" if container_drift else "ok")
    confidence, shadow_verified, shadow_msg = 0.0, False, ""

    if drift_detected and suggested_patch:
        shadow_verified, shadow_msg = _shadow_check(raw_data, spec_or_output_map, suggested_patch)
        if shadow_verified:
            if container_drift:
                confidence = 0.95
            else:
                min_cand = min(info.get("score", 0.0) for info in field_drifts.values())
                anchor_ok = all(
                    k in suggested_patch or bool(_dig(sample, cur_fields[k][0]))
                    for k in ("item_title", "item_url")
                )
                confidence = 0.95 if (
                    (min_cand >= 0.90 and anchor_ok)
                    or (min_cand >= 0.85 and "item_title" in suggested_patch and "item_url" in suggested_patch)
                ) else (min_cand if min_cand < 0.95 else 0.80)
        else:
            confidence = 0.60
            issues.append(f"影子试跑未通过: {shadow_msg}")

    return {
        "ok": not drift_detected,
        "drift_detected": drift_detected,
        "stage": stage,
        "issues": issues,
        "container_drift": container_drift,
        "field_drifts": field_drifts,
        "suggested_patch": suggested_patch,
        "sample_keys": list(flat.keys())[:12],
        "confidence": confidence,
        "shadow_verified": shadow_verified,
        "shadow_msg": shadow_msg,
    }


def render_drift_report(diagnosis: dict[str, Any], engine_id: str = "") -> str:
    """渲染终端格式化的结构漂移诊断报告（只读预览）。"""
    prefix = f"[{engine_id}] " if engine_id else ""
    if not diagnosis.get("drift_detected"):
        if diagnosis.get("issues"):
            lines = [f"{prefix}诊断发现异常 (Stage: {diagnosis.get('stage')}):"]
            lines.extend(f"  • {iss}" for iss in diagnosis["issues"])
            return "\n".join(lines)
        return f"{prefix}结构正常，未检测到漂移。"

    lines = [
        f"{prefix}检测到上游响应结构漂移 (Stage: {diagnosis.get('stage')}, 置信度: {diagnosis.get('confidence', 0):.0%}):"
    ]
    lines.extend(f"  • {iss}" for iss in diagnosis.get("issues", []))
    patch = diagnosis.get("suggested_patch") or {}
    if patch:
        lines.append("  建议 Spec 补丁 (output_map):")
        lines.extend(f"    {k}: {v}" for k, v in patch.items())
    if diagnosis.get("sample_keys"):
        lines.append(f"  样本键路径: {', '.join(diagnosis['sample_keys'])}")
    return "\n".join(lines)
