#!/usr/bin/env python3
"""
engines_schema_drift.py — 声明式引擎 Spec 结构漂移诊断与保真自愈。

职责：
  - 检测三层契约漂移：Envelope (业务错误封套)、Container (items 列表)、Field (title/url/snippet)
  - 启发式匹配与置信度推断
  - 内存影子试跑回放（verify_schema_patch）
  - 保真结构化应用 Spec 补丁并执行语法/语义门禁（apply_schema_patch）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
import yaml

from engines_fields import extract_items, _dig, _format_url_template

# 声明式架构自省与结构漂移诊断（第一性原理 + MECE 原则）
# ─────────────────────────────────────────────────────────────────────────────

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
    """将嵌套字典/列表展平为点分路径键值字典（深度受控，列表展开前 2 条）。"""
    flat: dict[str, Any] = {}
    if depth > max_depth or not isinstance(item, (dict, list)):
        return flat
    if isinstance(item, dict):
        for k, v in item.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and depth < max_depth:
                flat.update(_flatten_item(v, path, depth + 1, max_depth))
            else:
                flat[path] = v
    elif isinstance(item, list):
        for i, v in enumerate(item[:2]):
            path = f"{prefix}.{i}" if prefix else str(i)
            if isinstance(v, (dict, list)) and depth < max_depth:
                flat.update(_flatten_item(v, path, depth + 1, max_depth))
            else:
                flat[path] = v
    return flat


def _find_candidate_containers(data: Any, prefix: str = "", depth: int = 0, max_depth: int = 4) -> list[tuple[str, list, float]]:
    """递归搜索全部字典列表，并按结构质量评分。"""
    candidates: list[tuple[str, list, float]] = []
    if depth > max_depth:
        return candidates
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            sample = data[0]
            score = 0.5 + min(0.3, len(data) * 0.02)
            keys = {str(k).lower() for k in sample.keys()}
            if any(k in keys for k in ("title", "name", "id", "url", "link")):
                score += 0.2
            candidates.append((prefix or ".", data, score))
        return candidates
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                sample = v[0]
                score = 0.5 + min(0.3, len(v) * 0.02)
                keys = {str(k).lower() for k in sample.keys()}
                if any(syn in keys for syn in ("title", "name", "id", "url", "link")):
                    score += 0.2
                candidates.append((path, v, score))
            elif isinstance(v, dict) and depth < max_depth:
                candidates.extend(_find_candidate_containers(v, path, depth + 1, max_depth))
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates


def _match_best_field(target_kind: str, flat: dict[str, Any]) -> tuple[str | None, Any, float]:
    """根据目标类型和扁平键树，启发式匹配最优路径。"""
    if target_kind == "title":
        for path, val in flat.items():
            if not isinstance(val, (str, int)) or not str(val).strip():
                continue
            seg = path.split(".")[-1].lower()
            if seg in ("title", "name", "headline", "artwork_title"):
                return path, val, 0.95
            if seg in _TITLE_SYNONYMS:
                return path, val, 0.85
    elif target_kind == "url":
        url_candidates: list[tuple[str, Any, float]] = []
        for path, val in flat.items():
            s = str(val or "").strip()
            if not (s.startswith("http://") or s.startswith("https://") or s.startswith("/")):
                continue
            seg = path.split(".")[-1].lower()
            score = 0.6
            if seg in _URL_SYNONYMS:
                score += 0.3
            if any(s.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                score -= 0.4
            url_candidates.append((path, val, score))
        if url_candidates:
            url_candidates.sort(key=lambda x: x[2], reverse=True)
            best = url_candidates[0]
            if best[2] >= 0.5:
                return best[0], best[1], best[2]
        for path, val in flat.items():
            seg = path.split(".")[-1].lower()
            if seg in _URL_SYNONYMS and val:
                return path, val, 0.75
    elif target_kind == "summary":
        for path, val in flat.items():
            s = str(val or "").strip()
            if not s or len(s) < 5:
                continue
            seg = path.split(".")[-1].lower()
            if seg in ("snippet", "summary", "description", "abstract"):
                return path, val, 0.90
            if seg in _SUMMARY_SYNONYMS:
                return path, val, 0.80
    return None, None, 0.0


def diagnose_schema_drift(raw_data: Any, spec_or_output_map: dict[str, Any]) -> dict[str, Any]:
    """上游数据源结构漂移自省对拍诊断（第一性原理 + MECE 原则）。

    层级覆盖：
      1. Envelope: 业务错误封套、非结构化数据判定
      2. Container: items 容器路径位移对拍与自动推荐
      3. Field: item_title / item_url / item_summary 字段更名、层级下沉同义与特征对拍
    """
    omap = spec_or_output_map.get("output_map") if isinstance(spec_or_output_map.get("output_map"), dict) else spec_or_output_map
    if not isinstance(omap, dict):
        omap = {}

    current_items_path = omap.get("items", "")
    current_title = omap.get("item_title", "title")
    current_url = omap.get("item_url", "url")
    current_summary = omap.get("item_summary", "snippet")

    issues: list[str] = []
    container_drift: dict[str, Any] | None = None
    field_drifts: dict[str, Any] = {}
    suggested_patch: dict[str, str] = {}

    # 1. Envelope 层
    if not isinstance(raw_data, (dict, list)):
        return {
            "ok": False,
            "drift_detected": False,
            "stage": "envelope",
            "issues": [f"响应非 JSON 结构 (类型: {type(raw_data).__name__})"],
            "confidence": 0.0,
            "suggested_patch": {},
        }
    if isinstance(raw_data, dict):
        err_val = raw_data.get("error") or raw_data.get("err_msg") or raw_data.get("message")
        if err_val and not raw_data.get("data") and not raw_data.get("results") and not raw_data.get("items"):
            return {
                "ok": False,
                "drift_detected": False,
                "stage": "envelope",
                "issues": [f"上游返回业务错误封套: {err_val}"],
                "confidence": 0.0,
                "suggested_patch": {},
            }

    # 2. Container 层
    items = extract_items(raw_data, current_items_path)
    if not items or not isinstance(items[0], dict):
        candidates = _find_candidate_containers(raw_data)
        if candidates:
            best_path, best_items, score = candidates[0]
            container_drift = {
                "from": current_items_path,
                "to": best_path,
                "count": len(best_items),
                "score": score,
            }
            issues.append(f"容器路径失效: '{current_items_path}' 未提取到条目，候选路径 '{best_path}' (包含 {len(best_items)} 条)")
            suggested_patch["items"] = best_path
            items = best_items
        else:
            return {
                "ok": False,
                "drift_detected": False,
                "stage": "container",
                "issues": [f"未在响应中发现任何字典列表容器 (当前 items='{current_items_path}')"],
                "confidence": 0.0,
                "suggested_patch": {},
            }

    # 3. Field 层
    sample = items[0]
    flat = _flatten_item(sample, max_depth=3)

    # 校验与对拍 title
    val_title = _dig(sample, current_title) if current_title else None
    if not val_title:
        cand_p, cand_v, cand_s = _match_best_field("title", flat)
        if cand_p and cand_p != current_title:
            field_drifts["item_title"] = {"from": current_title, "to": cand_p, "sample": str(cand_v)[:50], "score": cand_s}
            issues.append(f"标题字段失效: '{current_title}' 为空 -> 候选 '{cand_p}' (值: {cand_v!r})")
            suggested_patch["item_title"] = cand_p

    # 校验与对拍 url
    val_url = _dig(sample, current_url) if current_url else None
    if not val_url:
        cand_p, cand_v, cand_s = _match_best_field("url", flat)
        if cand_p and cand_p != current_url:
            field_drifts["item_url"] = {"from": current_url, "to": cand_p, "sample": str(cand_v)[:50], "score": cand_s}
            issues.append(f"链接字段失效: '{current_url}' 为空 -> 候选 '{cand_p}' (值: {cand_v!r})")
            suggested_patch["item_url"] = cand_p

    # 校验与对拍 summary
    if current_summary:
        val_sum = _dig(sample, current_summary)
        if not val_sum:
            cand_p, cand_v, cand_s = _match_best_field("summary", flat)
            if cand_p and cand_p != current_summary:
                field_drifts["item_summary"] = {"from": current_summary, "to": cand_p, "sample": str(cand_v)[:50], "score": cand_s}
                issues.append(f"摘要字段失效: '{current_summary}' 为空 -> 候选 '{cand_p}'")
                suggested_patch["item_summary"] = cand_p

    drift_detected = bool(container_drift or field_drifts)
    stage = "field" if field_drifts else ("container" if container_drift else "ok")
    confidence = 0.0
    shadow_verified = False
    shadow_msg = ""

    if drift_detected and suggested_patch:
        shadow_verified, shadow_msg = verify_schema_patch(raw_data, spec_or_output_map, suggested_patch)
        if shadow_verified:
            if container_drift:
                confidence = 0.95
            elif field_drifts:
                scores = [info.get("score", 0.0) for info in field_drifts.values()]
                min_cand = min(scores) if scores else 0.0
                anchor_ok = True
                if "item_title" not in suggested_patch and not _dig(sample, current_title):
                    anchor_ok = False
                if "item_url" not in suggested_patch and not _dig(sample, current_url):
                    anchor_ok = False
                if (min_cand >= 0.90 and anchor_ok) or (min_cand >= 0.85 and "item_title" in suggested_patch and "item_url" in suggested_patch):
                    confidence = 0.95
                else:
                    confidence = min_cand if min_cand < 0.95 else 0.80
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
        "confidence": confidence,
        "shadow_verified": shadow_verified,
        "shadow_msg": shadow_msg,
    }


def verify_schema_patch(raw_data: Any, spec_or_output_map: dict[str, Any], patch: dict[str, str]) -> tuple[bool, str]:
    """影子试跑回放：在内存中用补丁验证是否能正确提取合规条目。"""
    omap = spec_or_output_map.get("output_map") if isinstance(spec_or_output_map.get("output_map"), dict) else spec_or_output_map
    omap = dict(omap or {})
    omap.update(patch)

    items_path = omap.get("items", "")
    items = extract_items(raw_data, items_path)
    if not items or not isinstance(items[0], dict):
        return False, f"容器路径 '{items_path}' 未提取到字典条目"

    title_k = omap.get("item_title", "title")
    url_k = omap.get("item_url", "url")
    url_tmpl = spec_or_output_map.get("url_template") if isinstance(spec_or_output_map, dict) else None

    checked = 0
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        t = _dig(it, title_k) if title_k else None
        u = _dig(it, url_k) if url_k else None
        if url_tmpl and not u:
            u = _format_url_template(url_tmpl, it)
        if not t or not str(t).strip():
            return False, f"样本标题为空 (字段 '{title_k}')"
        if not u or not str(u).strip():
            return False, f"样本 URL 为空 (字段 '{url_k}')"
        checked += 1

    if checked == 0:
        return False, "无有效样本条目"
    return True, f"成功对 {checked} 条样本完成 title 与 url 提取验证"


def render_drift_report(diagnosis: dict[str, Any], engine_id: str = "") -> str:
    """渲染终端格式化的结构漂移诊断报告。"""
    lines: list[str] = []
    prefix = f"[{engine_id}] " if engine_id else ""
    if not diagnosis.get("drift_detected"):
        if diagnosis.get("issues"):
            lines.append(f"{prefix}诊断发现异常 (Stage: {diagnosis.get('stage')}):")
            for iss in diagnosis["issues"]:
                lines.append(f"  • {iss}")
        else:
            lines.append(f"{prefix}结构正常，未检测到漂移。")
        return "\n".join(lines)

    lines.append(f"{prefix}检测到上游响应结构漂移 (Stage: {diagnosis.get('stage')}, 置信度: {diagnosis.get('confidence', 0):.0%}):")
    for iss in diagnosis.get("issues", []):
        lines.append(f"  • {iss}")

    patch = diagnosis.get("suggested_patch") or {}
    if patch:
        lines.append("  建议 Spec 补丁 (output_map):")
        for k, v in patch.items():
            lines.append(f"    {k}: {v}")
    return "\n".join(lines)


def apply_schema_patch(spec_file: str | Path, patch: dict[str, str]) -> tuple[bool, str]:
    """保真结构化应用 Spec 补丁：保留所有注释与缩进排版，仅改写 output_map 对应键，并通过语法与语义门禁。"""
    p = Path(spec_file)
    if not p.is_file():
        return False, f"Spec 文件不存在: {p}"

    text = p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # 1. 精确结构化定位 output_map 作用域
    output_map_idx = -1
    base_indent = -1
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        clean = stripped.split("#", 1)[0].rstrip()
        if clean == "output_map:":
            output_map_idx = idx
            base_indent = len(line) - len(stripped)
            break

    if output_map_idx == -1:
        return False, "Spec 中无 output_map 节点，不支持就地修改"

    # 2. 确定 output_map 子块的作用域范围与子项缩进
    end_idx = len(lines)
    child_indent = " " * (base_indent + 2)
    found_child = False

    for idx in range(output_map_idx + 1, len(lines)):
        line = lines[idx]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        cur_indent = len(line) - len(stripped)
        if cur_indent <= base_indent:
            end_idx = idx
            break
        if not found_child:
            child_indent = line[:cur_indent]
            found_child = True

    # 3. 在作用域内更新已有键（保留行尾注释）或插入新键
    remaining_patch = dict(patch)
    new_child_lines: list[str] = []
    for idx in range(output_map_idx + 1, end_idx):
        line = lines[idx]
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            parts = stripped.split(":", 1)
            k = parts[0].strip()
            if k in remaining_patch:
                val = remaining_patch.pop(k)
                comment = ""
                if len(parts) > 1 and "#" in parts[1]:
                    comment = "  #" + parts[1].split("#", 1)[1].rstrip()
                line_indent = line[:len(line) - len(stripped)]
                line = f"{line_indent}{k}: {val}{comment}\n"
        new_child_lines.append(line)

    for k, val in remaining_patch.items():
        new_child_lines.append(f"{child_indent}{k}: {val}\n")

    full_lines = lines[:output_map_idx + 1] + new_child_lines + lines[end_idx:]
    new_text = "".join(full_lines)

    # 4. 严格结构与语法门禁（Double-check validation）
    try:
        before = yaml.safe_load(text)
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        return False, f"YAML 语法校验未通过: {exc}"

    if not isinstance(parsed, dict) or "output_map" not in parsed:
        return False, "校验失败: output_map 丢失或根节点非字典"

    omap = parsed.get("output_map")
    if not isinstance(omap, dict):
        return False, "校验失败: output_map 非字典结构"

    # 4.1 差分门禁：补丁只允许改 output_map 内被点名的键。
    # 结构化定位一旦缩进判断出错，整块可能被挪出 output_map；只校验目标键
    # 存在并不足以发现「其它键被连坐改写」，这里对整棵文档做等价性对拍。
    if not isinstance(before, dict):
        return False, "校验失败: 原文件根节点非字典"
    for k in set(before) | set(parsed):
        if k == "output_map":
            continue
        if before.get(k) != parsed.get(k):
            return False, f"校验失败: output_map 之外的键 {k!r} 被改动"
    before_omap = before.get("output_map")
    if not isinstance(before_omap, dict):
        return False, "校验失败: 原 output_map 非字典结构"
    for k, val in before_omap.items():
        if k in patch:
            continue
        if omap.get(k) != val:
            return False, f"校验失败: 未点名的键 {k!r} 被改动"

    for k, val in patch.items():
        if omap.get(k) != val:
            return False, f"校验失败: 键 \x27{k}\x27 未正确写入 output_map"

    # 5. 安全写盘：写前生成**不覆盖**的时间戳备份；备份失败即中止。
    # 固定名 `spec.yaml.bak` 只作「最近一次」镜像（兼容既有查看习惯），
    # 历史由时间戳文件保留，连续修复不会把原始内容冲掉。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = p.with_name(f"{p.name}.{stamp}.bak")
    seq = 1
    while bak.exists():
        bak = p.with_name(f"{p.name}.{stamp}-{seq}.bak")
        seq += 1
    try:
        bak.write_text(text, encoding="utf-8")
    except OSError as exc:
        return False, f"备份失败，已中止写入: {exc}"

    latest = p.with_suffix(p.suffix + ".bak")
    try:
        latest.write_text(text, encoding="utf-8")
    except OSError:
        # 镜像只是便利入口，时间戳备份已落盘，不影响回滚凭据。
        pass

    p.write_text(new_text, encoding="utf-8")
    return True, f"已安全更新 {p.name} 中的 {len(patch)} 个字段（备份至 {bak.name}）"
