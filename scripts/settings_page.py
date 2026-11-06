#!/usr/bin/env python3
"""settings_page — 本地只读设置页（单文件 HTML，零依赖）。

用法: argo settings [--out PATH] [--no-open]

定位（2026-11-07，issue #44 的最小兑现）：多 agent 客户端并存时各自重复填
API Key 是真实痛点，用户要的是「一份配置透视、处处可看」。本命令把配置
单一真源（config.yaml + envfile）渲染成单文件 HTML：密钥状态掩码展示、
接入片段一键复制、常用开关现值。只读——编辑仍走 config.yaml / envfile，
这里不做写回、不起常驻服务、不需要鉴权（页面本身不含任何密钥明文，
落在本机 state 目录，比常驻端口小一个暴露面）。

数据来源（全部现成，不新增真源）：
  - argo_paths.resolved_paths()      路径诊断（state_root / env_files）
  - engine_env.required_env_for()    各引擎密钥要求（issue #41 的痛点位）
  - engine_env.missing_env_for()     缺失判定（候选链，与 runtime 同一口径）
  - engine_env.get_env()             现值读取（掩码后只露尾 4 位）
  - engines.available_engines()      收录数 / 可路由数
输出末行恒为本地 file:// 地址——agent 对话里直接转述即可，这正是 #44
设想的「回复内容里显示设置页面地址」。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argo_paths  # noqa: E402

# 常用开关的展示清单（名 → 一句话说明）。只列有行为语义的，凑数的不进。
_KNOWN_SWITCHES = [
    ("ARGO_USAGE_LOG", "使用日志总账（stats / badcase 账本的数据源）"),
    ("ARGO_ADAPTIVE_GRACE", "对冲宽限窗自适应收窄（0 关闭，回固定窗）"),
    ("ARGO_FETCH_MOBILE", "移动 UA 分流（0 关闭）"),
    ("ARGO_ROUTE_CACHE", "路由决策缓存"),
    ("ARGO_NO_CACHE", "禁用全部结果缓存（调试用）"),
    ("ARGO_FETCH_DEADLINE_S", "单次 fetch 总耗时上限（秒，0 关闭）"),
]

# MCP / CLI 接入示例（片段不含密钥；密钥由 mcp_launch.sh 启动时注入）
_CLI_EXAMPLES = [
    ("搜索", 'argo search "查询词" --json'),
    ("抓正文", "argo fetch <url>"),
    ("公众号文章", "argo wx <mp.weixin.qq.com 链接>"),
    ("本设置页", "argo settings"),
]


def _version() -> str:
    """与 bin/argo --version 同一来源：根 package.json 的 version。"""
    try:
        root = Path(__file__).resolve().parent.parent
        with open(root / "package.json", encoding="utf-8") as f:
            return str(json.load(f).get("version") or "unknown")
    except Exception:
        return "unknown"


def _mask(value: str) -> str:
    """密钥掩码：只承认「已配置」与尾 4 位，任何长度都不回显明文。"""
    v = (value or "").strip()
    if not v:
        return "缺失"
    if len(v) >= 8:
        return f"已配置（…{v[-4:]}）"
    return "已配置"


def _effective_env(key: str) -> str:
    """开关现值：走 engine_env.get_env 同一候选链（进程 env 优先，envfile 兜底）。"""
    try:
        import engine_env
        return engine_env.get_env(key) or ""
    except Exception:
        return os.environ.get(key) or ""


def collect() -> dict[str, Any]:
    """汇总设置页数据。任何一块失败都降级为占位说明，不拖垮整页。"""
    data: dict[str, Any] = {
        "version": _version(),
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        "paths": {},
        "engine_counts": {"total": 0, "routable": 0},
        "key_rows": [],
        "switches": [],
    }
    try:
        data["paths"] = argo_paths.resolved_paths()
    except Exception as e:
        data["paths"] = {"error": f"路径诊断不可用：{type(e).__name__}"}

    try:
        from engines import available_engines
        names = available_engines()
        routable = available_engines(routable_only=True)
        data["engine_counts"] = {"total": len(names),
                                 "routable": len(routable)}
    except Exception:
        names = []

    # 密钥状态只列「声明了 required_env」的引擎——这正是 #41 里
    # 「装了 13 个引擎全 skipped」的痛点位；无密钥要求的引擎不上表。
    # 判定与 runtime 同源：缺失与否以 missing_env_for（候选链）为准，
    # 掩码值以 get_env 为准——显示层绝不自建第二套口径（#41/#42 的教训）。
    try:
        from engines import get_engine_spec
        from engine_env import (required_env_for, missing_env_for, get_env,
                                PLACEHOLDER_ALIASES)

        def _candidates(var: str) -> list[str]:
            if var in PLACEHOLDER_ALIASES:
                return list(PLACEHOLDER_ALIASES[var])
            if var.startswith("ARGO_"):
                return [var, var[len("ARGO_"):]]
            return [f"ARGO_{var}", var]

        for name in names:
            spec = get_engine_spec(name) or {}
            try:
                reqs = required_env_for(name, spec)
            except Exception:
                reqs = spec.get("required_env") or []
            if not reqs:
                continue
            try:
                missing = set(missing_env_for(name, spec))
            except Exception:
                missing = set(reqs)
            data["key_rows"].append({
                "engine": name,
                "envs": [{"name": r,
                          "status": (_mask(get_env(_candidates(r)) or "")
                                     if r not in missing else "缺失")}
                         for r in reqs],
            })
    except Exception:
        pass

    data["switches"] = [
        {"name": name, "desc": desc,
         "value": _effective_env(name) or "（默认）"}
        for name, desc in _KNOWN_SWITCHES
    ]
    try:
        data["mcp_launcher"] = str(
            Path(__file__).resolve().parent / "mcp_launch.sh")
    except Exception:
        data["mcp_launcher"] = "scripts/mcp_launch.sh"
    return data


def render_html(data: dict[str, Any]) -> str:
    """渲染单文件 HTML。所有动态值经 html.escape；本函数是唯一的拼接口。"""
    e = html.escape
    key_rows = "".join(
        "<tr><td>{eng}</td><td>{envs}</td></tr>".format(
            eng=e(r["engine"]),
            envs="、".join(
                f"<code>{e(v['name'])}</code> {e(v['status'])}"
                for v in r["envs"]))
        for r in data.get("key_rows", []))
    if not key_rows:
        key_rows = ('<tr><td colspan="2">（未读到引擎注册表，'
                    "请检查 config.yaml 是否可解析）</td></tr>")

    switches = "".join(
        f"<tr><td><code>{e(s['name'])}</code></td>"
        f"<td>{e(s['desc'])}</td><td>{e(s['value'])}</td></tr>"
        for s in data.get("switches", []))

    mcp_json = json.dumps({
        "mcpServers": {"argo": {
            "command": data.get("mcp_launcher", "scripts/mcp_launch.sh"),
            "args": [],
            "env": {"PYTHONIOENCODING": "utf-8"},
        }}}, ensure_ascii=False, indent=2)
    cli_rows = "".join(
        f"<tr><td>{e(label)}</td><td><code>{e(cmd)}</code></td></tr>"
        for label, cmd in _CLI_EXAMPLES)

    paths = data.get("paths") or {}
    path_rows = "".join(
        f"<tr><td><code>{e(k)}</code></td><td>{e(v)}</td></tr>"
        for k, v in sorted(paths.items()))

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Argo 设置 · {e(data.get('version', ''))}</title>
<style>
 body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
       max-width:860px;margin:24px auto;padding:0 16px;color:#1f2328;line-height:1.6}}
 h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;
   border-bottom:1px solid #d0d7de;padding-bottom:4px}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 td,th{{border:1px solid #d0d7de;padding:5px 8px;text-align:left;
        vertical-align:top}}
 code{{background:#f6f8fa;padding:1px 5px;border-radius:4px;font-size:12px}}
 .muted{{color:#656d76;font-size:12px}}
 .btn{{font-size:12px;padding:2px 10px;cursor:pointer}}
 .ok{{color:#1a7f37}} .miss{{color:#cf222e}}
</style></head><body>
<h1>Argo 设置 <span class="muted">v{e(data.get('version', ''))} · 只读透视 ·
生成于 {e(data.get('generated_at', ''))}</span></h1>
<p class="muted">本页由 <code>argo settings</code> 生成本地单文件，不含任何密钥明文；
修改配置请编辑 config.yaml 与密钥 env 文件（见下表 env_file_in_use 行）。</p>

<h2>收录与可用</h2>
<p>引擎收录 <b>{data.get('engine_counts', {}).get('total', 0)}</b> 个，
当前可路由 <b class="ok">{data.get('engine_counts', {}).get('routable', 0)}</b> 个
（密钥齐全、依赖就位、未被熔断/禁用）。</p>

<h2>路径真相（支持类问题的第一问）</h2>
<table>{path_rows}</table>

<h2>密钥状态（声明了 required_env 的引擎）</h2>
<table><tr><th>引擎</th><th>环境变量</th></tr>{key_rows}</table>

<h2>接入片段</h2>
<p><button class="btn" onclick="cp(this)">复制 MCP JSON</button></p>
<pre id="mcpjson" style="display:none">{e(mcp_json)}</pre>
<table><tr><th>用途</th><th>命令</th></tr>{cli_rows}</table>
<p class="muted">MCP entry 放进各客户端配置即可；command 指向
<code>mcp_launch.sh</code> 启动器，密钥在启动时注入，多客户端共享同一份 envfile。</p>

<h2>常用开关现值</h2>
<table><tr><th>变量</th><th>说明</th><th>现值</th></tr>{switches}</table>

<script>
function cp(btn){{
  var t=document.getElementById('mcpjson').textContent;
  (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject())
   .then(function(){{btn.textContent='已复制';}})
   .catch(function(){{window.prompt('手动复制：',t);}});
  setTimeout(function(){{btn.textContent='复制 MCP JSON';}},1500);
}}
</script>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成本地只读设置页（单文件 HTML，掩码展示密钥状态）")
    ap.add_argument("--out", default=None,
                    help="输出路径（默认 <state>/settings.html）")
    ap.add_argument("--no-open", action="store_true",
                    help="只生成不打开浏览器")
    args = ap.parse_args()

    data = collect()
    page = render_html(data)

    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        out_path = Path(argo_paths.state_path("settings.html"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    page_url = out_path.resolve().as_uri()
    if not args.no_open:
        try:
            webbrowser.open(page_url)
        except Exception:
            pass  # 无桌面环境的场景：路径照常打印，agent 可转述
    print(f"设置页已生成：{page_url}")
    print("多客户端共享同一份配置；修改请编辑 config.yaml 与密钥 env 文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
