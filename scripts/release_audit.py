"""release_audit.py — 发版范围覆盖、机械红线门禁与单源派生（P0/P1）。

借鉴 plugin-fast-adapt 方法论：
1. 范围覆盖（P0）：从上一公开 tag 至今的 git log 自动归类草稿，消除遗漏；
2. 机械红线（P0）：仅扫描新版本发布文案，拦截本机路径、内部脚本/私有符号、
   底层算法/表结构细节，宁少不滥、零误报；
3. 单源派生（P1）：以 docs/RELEASE_NOTES_v<ver>.md 为唯一语义真源，幂等派生
   CHANGELOG.md 与五语 README 的更新段及发布记录表，并支持 check --json。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

# 历史已发布版本（<= 2.18.0）的既有文案保留原样，不溯及既往
_LEGACY_MAX_VER = (2, 18, 0)

_PATH_LEAK_RE = re.compile(
    r"(?:/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|"
    r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+|/private/var/folders/)"
)
_IMPL_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("内部脚本路径", re.compile(r"(?:scripts|sub-skills/[A-Za-z0-9_-]+/scripts)/[A-Za-z0-9_]+\.py")),
    ("内部插件源码路径", re.compile(r"packages/dsh-plugin/[A-Za-z0-9_/-]+\.[mc]?js")),
    ("内部私有符号", re.compile(r"`_[a-z][a-z0-9_]{2,}`")),
    ("底层实现参数/表结构", re.compile(r"\b(?:bm25\(content,|PRAGMA\s+table_info\b|path_terms\b|damping\s*=\s*0\.\d+\b)")),
)


def _ver_tuple(ver: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", ver.strip())
    if not m:
        return (0, 0, 0)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _latest_tag(root: Path) -> str:
    r = _git(root, "describe", "--tags", "--abbrev=0", "--match", "v*")
    return r.stdout.strip() if r.returncode == 0 else ""


def _classify_commit(subj: str) -> str:
    low = subj.lower()
    if "local-seek" in low or "local" in low or "table" in low:
        return "本地搜索与分析"
    if "dsh-plugin" in low or "workbench" in low or "bin/argo" in low:
        return "插件与跨平台启动"
    if "release" in low or "readme" in low or "changelog" in low:
        return "文档与发版"
    return "公网搜索与 CLI"


def collect_commits_since_last_tag(root: Path) -> dict[str, Any]:
    """收集自上一公开 tag 以来的提交清单并按领域归类（供草稿生成与覆盖度账本）。"""
    tag = _latest_tag(root)
    rev_range = f"{tag}..HEAD" if tag else "HEAD~15..HEAD"
    r = _git(root, "log", rev_range, "--no-merges", "--pretty=format:%h|%s")
    if r.returncode != 0 or not r.stdout.strip():
        return {"base_tag": tag, "commit_count": 0, "groups": {}}
    groups: dict[str, list[str]] = {}
    count = 0
    for line in r.stdout.splitlines():
        if "|" not in line:
            continue
        sha, subj = line.split("|", 1)
        subj = subj.strip()
        if not subj or subj.startswith("docs(release):"):
            continue
        cat = _classify_commit(subj)
        groups.setdefault(cat, []).append(f"{sha} {subj}")
        count += 1
    return {"base_tag": tag, "commit_count": count, "groups": groups}


def render_commit_draft(info: dict[str, Any]) -> str:
    """将收集到的提交格式化为发布说明草稿清单（提示改写为实测收益）。"""
    if not info.get("commit_count"):
        return ""
    base = info.get("base_tag") or "近期提交"
    lines = [
        f"<!-- 草稿清单（基于 {base}..HEAD 共 {info['commit_count']} 笔提交）："
        "请改写为面向用户的实测收益与开启方式，勿保留内部脚本路径或算法细节 -->",
    ]
    for cat, items in (info.get("groups") or {}).items():
        lines.append(f"<!-- [{cat}]")
        for it in items:
            lines.append(f"  - {it}")
        lines.append("-->")
    return "\n".join(lines) + "\n\n"


def scan_text_leakage(text: str, rel: str) -> list[str]:
    """对单份发布文本执行脱敏与实现细节泄露扫描。"""
    hits: list[str] = []
    for idx, line in enumerate(text.splitlines(), 1):
        # 跳过 HTML 注释里的草稿提示
        if line.lstrip().startswith("<!--"):
            continue
        if _PATH_LEAK_RE.search(line):
            hits.append(f"{rel}:{idx}: 含本机绝对路径或用户名，请脱敏")
        for label, pat in _IMPL_LEAK_PATTERNS:
            m = pat.search(line)
            if m:
                hits.append(f"{rel}:{idx}: 泄露{label}（{m.group(0)}），请改写为面向用户的效果描述")
    return hits


def scan_release_leakage(root: Path, ver: str, readme_langs: tuple[str, ...]) -> list[str]:
    """机械红线门禁：仅针对新版本（> v2.18.0）发布文案做路径脱敏与细节拦截。"""
    if _ver_tuple(ver) <= _LEGACY_MAX_VER:
        return []
    problems: list[str] = []
    notes = root / f"docs/RELEASE_NOTES_v{ver}.md"
    if notes.is_file():
        problems.extend(scan_text_leakage(notes.read_text(encoding="utf-8"), f"docs/RELEASE_NOTES_v{ver}.md"))

    changelog = root / "docs/CHANGELOG.md"
    if changelog.is_file():
        cl_text = changelog.read_text(encoding="utf-8")
        m = re.search(rf"(?ms)^## v{re.escape(ver)}\n(.*?)(?=^## v|\Z)", cl_text)
        if m:
            problems.extend(scan_text_leakage(m.group(1), f"docs/CHANGELOG.md#v{ver}"))

    for rel in readme_langs:
        p = root / rel
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8")
        m = re.search(rf"(?ms)^### v{re.escape(ver)}[^\n]*\n(.*?)(?=^### |\Z)", txt)
        if m:
            problems.extend(scan_text_leakage(m.group(1), f"{rel}#v{ver}"))
    return problems


def _extract_notes_summary(notes_text: str) -> tuple[str, list[str], str]:
    """从 RELEASE_NOTES_v<ver>.md 提取 (英文摘要, 中文要点列表, 中文定位)。"""
    loc = ""
    m_loc = re.search(r"^\*\*定位\*\*[：:]\s*(.+)$", notes_text, re.M)
    if m_loc:
        loc = m_loc.group(1).strip()

    en_summary = ""
    m_en = re.search(r"(?ms)^## Highlights.*?\n(.*?)(?=^---|\n# )", notes_text)
    if m_en:
        bullets = [ln.strip("- ").strip() for ln in m_en.group(1).splitlines() if ln.strip().startswith("- ")]
        if bullets:
            en_summary = bullets[0][:180]

    zh_bullets: list[str] = []
    m_zh = re.search(r"(?ms)^## 这次更新有什么.*?\n(.*?)(?=^## |\Z)", notes_text)
    if m_zh:
        for ln in m_zh.group(1).splitlines():
            s = ln.strip()
            if s.startswith("- "):
                zh_bullets.append(s)
            if len(zh_bullets) >= 6:
                break
    return en_summary, zh_bullets, loc


def sync_from_release_notes(
    root: Path,
    ver: str,
    readme_langs: tuple[str, ...],
    *,
    dry: bool = False,
) -> list[str]:
    """P1 单源派生：从 RELEASE_NOTES_v<ver>.md 幂等同步 CHANGELOG 与五语 README。"""
    notes_path = root / f"docs/RELEASE_NOTES_v{ver}.md"
    if not notes_path.is_file():
        return [f"docs/RELEASE_NOTES_v{ver}.md 不存在，跳过派生"]

    notes_text = notes_path.read_text(encoding="utf-8")
    en_summary, zh_bullets, loc = _extract_notes_summary(notes_text)
    actions: list[str] = []

    # 1. 派生 docs/CHANGELOG.md
    cl_path = root / "docs/CHANGELOG.md"
    if cl_path.is_file():
        cl_text = cl_path.read_text(encoding="utf-8")
        if f"## v{ver}" not in cl_text:
            body_lines = [f"## v{ver}\n"]
            body_lines.append(f"**定位**：{loc or f'v{ver} 版本更新'}\n")
            if zh_bullets:
                body_lines.append("**关键变更**：")
                body_lines.extend(zh_bullets)
                body_lines.append("")
            body_lines.append("---\n")
            entry = "\n".join(body_lines) + "\n"
            m_first = re.search(r"(?m)^## v\d+\.\d+\.\d+", cl_text)
            if m_first:
                new_cl = cl_text[:m_first.start()] + entry + cl_text[m_first.start():]
                if not dry:
                    cl_path.write_text(new_cl, encoding="utf-8", newline="\n")
                actions.append(f"docs/CHANGELOG.md: 派生插入 ## v{ver}")
        else:
            actions.append(f"docs/CHANGELOG.md: 已含 ## v{ver}，跳过")

    # 2. 派生五语 README 的「最近更新」段与发布记录表行（仅在缺失时幂等补齐）
    for rel in readme_langs:
        p = root / rel
        if not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8")
        changed = False
        is_zh = rel == "README.zh.md"
        headline = loc if (is_zh and loc) else (en_summary or f"v{ver} updates")

        if not re.search(rf"(?m)^### v{re.escape(ver)}", txt):
            m_prev = re.search(r"(?m)^### v\d+\.\d+\.\d+", txt)
            if m_prev:
                see_line = (
                    f"详见 [docs/RELEASE_NOTES_v{ver}.md](docs/RELEASE_NOTES_v{ver}.md) 与 [docs/CHANGELOG.md](docs/CHANGELOG.md)。"
                    if is_zh
                    else f"See [docs/RELEASE_NOTES_v{ver}.md](docs/RELEASE_NOTES_v{ver}.md) and [docs/CHANGELOG.md](docs/CHANGELOG.md)."
                )
                sec = f"### v{ver}: {headline[:80]}\n\n{see_line}\n\n"
                txt = txt[:m_prev.start()] + sec + txt[m_prev.start():]
                changed = True

        if f"| **v{ver}** |" not in txt:
            m_row = re.search(r"(?m)^\| \*\*v\d+\.\d+\.\d+\*\* \|", txt)
            if m_row:
                row = f"| **v{ver}** | {headline[:68]} |\n"
                txt = txt[:m_row.start()] + row + txt[m_row.start():]
                changed = True

        if changed:
            if not dry:
                p.write_text(txt, encoding="utf-8", newline="\n")
            actions.append(f"{rel}: 派生更新 v{ver} 摘要与记录行")
        else:
            actions.append(f"{rel}: 已含 v{ver}，跳过")

    return actions


def build_check_report(
    root: Path,
    ver: str,
    problems: list[str],
    *,
    catalog_ok: bool,
    art_ok: bool,
    leakage_hits: list[str],
) -> dict[str, Any]:
    """生成 release.py check --json 统一结构化审计报告。"""
    cov = collect_commits_since_last_tag(root)
    return {
        "version": ver,
        "ok": len(problems) == 0,
        "problem_count": len(problems),
        "problems": problems,
        "coverage": {
            "base_tag": cov["base_tag"],
            "commits_since_tag": cov["commit_count"],
            "groups": {k: len(v) for k, v in cov["groups"].items()},
        },
        "gates": {
            "catalog_ok": catalog_ok,
            "art_ok": art_ok,
            "leakage_count": len(leakage_hits),
        },
    }
