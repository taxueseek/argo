#!/usr/bin/env python3
"""settings_page — 本地只读设置页（单文件 HTML，零依赖）+ 配置写回命令。

用法: argo settings [--out PATH] [--no-open]    # 生成只读透视页
      argo settings list                        # 纯文本透视（终端/agent）
      argo settings set <VAR>                   # 写入/覆盖（值从 stdin 读）
      argo settings unset <VAR> [<VAR>...]      # 清除（含历史裸名行）

定位（2026-11-07，issue #44 的最小兑现）：多 agent 客户端并存时各自重复填
API Key 是真实痛点，用户要的是「一份配置透视、处处可看」。本命令把配置
单一真源（config.yaml + envfile）渲染成单文件 HTML：密钥状态掩码展示、
接入片段一键复制、常用开关现值。只读——编辑走 `settings set/unset`
（env_store 唯一写路径：原子写、0600、.bak 备份、候选链合并），这里不做
写回表单、不起常驻服务、不需要鉴权（页面本身不含任何密钥明文，落在本机
state 目录，比常驻端口小一个暴露面）。

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
import env_store  # noqa: E402

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


def _runtime_candidates(var: str) -> list[str]:
    """与 engine_env.missing_env_for 同序：ARGO_ 推荐名优先、裸名兜底。"""
    if var.startswith("ARGO_"):
        return [var, var[len("ARGO_"):]]
    return [f"ARGO_{var}", var]


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
                          "status": (env_store.mask(
                              get_env(PLACEHOLDER_ALIASES.get(r) or _runtime_candidates(r))
                              or "")
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


# 两版页面（只读 / 编辑表单）共用的样式：单文件自包含，不引外部资源
_CSS = """
 body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
      max-width:860px;margin:24px auto;padding:0 16px;color:#1f2328;line-height:1.6}
 h1{font-size:20px} h2{font-size:16px;margin-top:28px;
   border-bottom:1px solid #d0d7de;padding-bottom:4px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 td,th{border:1px solid #d0d7de;padding:5px 8px;text-align:left;
       vertical-align:top}
 code{background:#f6f8fa;padding:1px 5px;border-radius:4px;font-size:12px}
 .muted{color:#656d76;font-size:12px}
 .btn{font-size:12px;padding:2px 10px;cursor:pointer}
 .ok{color:#1a7f37} .miss{color:#cf222e}
 input[type=text],input[type=password]{font-size:13px;padding:3px 6px;
   border:1px solid #d0d7de;border-radius:4px;width:220px}
"""


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
<style>{_CSS}</style></head><body>
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


def render_edit_html(data: dict[str, Any]) -> str:
    """渲染编辑表单页（--edit）。与只读页同一份数据，唯一区别：密钥行变成
    「掩码现值 + 新值输入 + 清除勾选」，页内导出变更 JSON 交给
    `argo settings apply` 写回——写回通道仍只有 env_store 一条，页面自身
    无服务、无鉴权、不携带任何现值明文。"""
    e = html.escape
    form_rows = "".join(
        "<tr><td>{eng}</td><td><code>{var}</code> {st}</td>"
        "<td><input type=\"password\" data-env=\"{var}\" "
        "placeholder=\"留空 = 不改动\"></td>"
        "<td>{unset_box}</td></tr>".format(
            eng=e(r["engine"]),
            var=e(v["name"]), st=e(v["status"]),
            unset_box=(f'<label><input type="checkbox" data-unset="{e(v["name"])}">'
                       "清除</label>" if "已配置" in v["status"] else ""))
        for r in data.get("key_rows", []) for v in r["envs"])
    if not form_rows:
        form_rows = ('<tr><td colspan="4">（未读到引擎注册表，'
                     "请检查 config.yaml 是否可解析）</td></tr>")

    # 自定义新增变量行：不在 required_env 清单里的键也能加（预留 3 行）
    new_rows = "".join(
        f'<tr><td>新增</td>'
        f'<td><input type="text" data-newname placeholder="变量名"></td>'
        f'<td><input type="password" data-newvalue placeholder="值（留空 = 跳过）"></td>'
        f'<td></td></tr>'
        for _ in range(3))

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Argo 设置 · 编辑 · {e(data.get('version', ''))}</title>
<style>{_CSS}</style></head><body>
<h1>Argo 设置 · 编辑 <span class="muted">v{e(data.get('version', ''))} ·
生成于 {e(data.get('generated_at', ''))}</span></h1>
<p class="muted">填完点「导出变更 JSON」→ 终端执行
<code>argo settings apply 变更.json</code> 写回。本页不含任何现值明文；
导出的 JSON 含新值明文，apply 成功后请删除该文件。</p>

<h2>引擎密钥（掩码现值 + 新值 + 清除）</h2>
<table>
<tr><th>引擎</th><th>变量</th><th>新值</th><th>清除</th></tr>
{form_rows}
</table>

<h2>自定义变量（可选）</h2>
<table>
<tr><th>动作</th><th>变量名</th><th>值</th><th></th></tr>
{new_rows}
</table>

<p><button class="btn" onclick="exportChanges()">导出变更 JSON</button>
<span class="muted" id="summary"></span></p>

<script>
function exportChanges(){{
  var out = {{sets: {{}}, unsets: []}}, n = 0;
  document.querySelectorAll('[data-env]').forEach(function(inp){{
    if (inp.value) {{ out.sets[inp.dataset.env] = inp.value; n++; }}
  }});
  document.querySelectorAll('[data-unset]').forEach(function(cb){{
    if (cb.checked) {{ out.unsets.push(cb.dataset.unset); n++; }}
  }});
  var names = document.querySelectorAll('[data-newname]');
  var vals = document.querySelectorAll('[data-newvalue]');
  names.forEach(function(nameInp, i){{
    var name = nameInp.value.trim(), val = vals[i] ? vals[i].value : '';
    if (name && val) {{ out.sets[name] = val; n++; }}
  }});
  if (!n) {{ document.getElementById('summary').textContent =
    '没有填写任何变更'; return; }}
  var s = JSON.stringify(out, null, 2);
  var blob = new Blob([s], {{type: 'application/json'}});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'argo-settings-changes.json';
  a.click();
  document.getElementById('summary').textContent =
    '已导出 ' + n + ' 项变更；复制备用见弹窗';
  window.prompt('变更 JSON（已触发下载，也可复制）：', s);
}}
</script>
</body></html>"""


def _run_page(args) -> int:
    """默认行为：生成设置页（--edit 为编辑表单版，其余只读透视）。"""
    data = collect()
    page = render_edit_html(data) if args.edit else render_html(data)

    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        out_path = Path(argo_paths.state_path(
            "settings-edit.html" if args.edit else "settings.html"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    page_url = out_path.resolve().as_uri()
    if not args.no_open:
        try:
            webbrowser.open(page_url)
        except Exception:
            pass  # 无桌面环境的场景：路径照常打印，agent 可转述
    print(f"设置页已生成：{page_url}")
    if args.edit:
        print("填完点「导出变更 JSON」，然后执行 argo settings apply <文件> 写回。")
    else:
        print("多客户端共享同一份配置；修改请用 argo settings set/unset。")
    return 0


def _run_list() -> int:
    """纯文本透视（与设置页同一份 collect 数据，给终端与 agent 用）。"""
    data = collect()
    counts = data.get("engine_counts", {})
    print(f"Argo v{data.get('version', '?')} · "
          f"引擎收录 {counts.get('total', 0)} · 可路由 {counts.get('routable', 0)}")
    env_in_use = (data.get("paths") or {}).get("env_file_in_use")
    if env_in_use:
        print(f"密钥文件：{env_in_use}")
    for r in data.get("key_rows", []):
        envs = "、".join(f"{v['name']} {v['status']}" for v in r["envs"])
        print(f"  {r['engine']}: {envs}")
    for s in data.get("switches", []):
        print(f"  [{s['name']}] {s['value']} — {s['desc']}")
    print("修改：echo <值> | argo settings set <变量名>；"
          "清除：argo settings unset <变量名>。")
    return 0


def _read_secret(prompt: str) -> str:
    """值只从 stdin 进来：tty 用 getpass 不回显，管道直接读一行。
    值不进 argv——进程列表与 shell history 都不会留痕。"""
    from cli_io import stdin_is_piped
    if stdin_is_piped():
        return sys.stdin.readline().rstrip("\r\n").strip()
    import getpass
    return getpass.getpass(prompt).strip()


def _run_set(args) -> int:
    try:
        value = _read_secret(f"请输入 {args.key} 的值（输入不回显）: ")
        report = env_store.apply_changes(sets={args.key: value})
    except env_store.EnvStoreError as e:
        print(f"settings set 失败：{e}", file=sys.stderr)
        return 1
    print(env_store.format_report(report))
    try:
        from engine_env import get_env
        print(f"现值：{env_store.mask(get_env(args.key) or '')}")
    except Exception:
        pass  # 回读失败不影响写回本身
    print("已生效：下一次调用即读到新值（envfile 热读，无需重启）。")
    return 0


def _run_unset(args) -> int:
    try:
        report = env_store.apply_changes(unsets=list(args.keys))
    except env_store.EnvStoreError as e:
        print(f"settings unset 失败：{e}", file=sys.stderr)
        return 1
    print(env_store.format_report(report))
    return 0


def _run_apply(args) -> int:
    """方案 B 写回口：读编辑页导出的变更 JSON（或 stdin 的 "-"）。"""
    try:
        if args.file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.file).expanduser().read_text(encoding="utf-8")
        changes = json.loads(raw)
        if not isinstance(changes, dict):
            raise env_store.EnvStoreError("JSON 顶层必须是对象")
        sets = changes.get("sets") or {}
        unsets = changes.get("unsets") or []
        if not isinstance(sets, dict) or not isinstance(unsets, list):
            raise env_store.EnvStoreError(
                '形状必须是 {"sets": {变量: 值}, "unsets": [变量]}')
        if len(sets) > 200 or len(unsets) > 200:
            raise env_store.EnvStoreError("变更条数超上限（200）")
        for k, v in sets.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise env_store.EnvStoreError(
                    f"sets 的键与值都必须是字符串：{k!r}")
        report = env_store.apply_changes(sets=sets, unsets=[str(u) for u in unsets])
    except (env_store.EnvStoreError, json.JSONDecodeError) as e:
        print(f"settings apply 失败：{e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"settings apply 失败：读不到 {args.file}：{e}", file=sys.stderr)
        return 1
    print(env_store.format_report(report))
    if args.file != "-":
        print("提醒：变更 JSON 含新值明文，确认生效后请删除该文件。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="本地只读设置页与配置写回（单文件 HTML，掩码展示密钥状态）")
    ap.add_argument("--out", default=None,
                    help="输出路径（默认 <state>/settings.html）")
    ap.add_argument("--no-open", action="store_true",
                    help="只生成不打开浏览器")
    ap.add_argument("--edit", action="store_true",
                    help="生成编辑表单页（导出变更 JSON 后用 apply 写回）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="纯文本透视：密钥状态 + 开关现值（不生成页面）")
    p_set = sub.add_parser("set", help="写入/覆盖一个环境变量（值从 stdin 读，"
                                        "不进 argv/history）")
    p_set.add_argument("key", help="变量名，如 ARGO_BOCHA_API_KEY")
    p_unset = sub.add_parser("unset", help="清除一个或多个环境变量（含历史裸名行）")
    p_unset.add_argument("keys", nargs="+", help="变量名列表")
    p_apply = sub.add_parser("apply", help="应用编辑页导出的变更 JSON（\"-\" 读 stdin）")
    p_apply.add_argument("file", help="变更 JSON 路径，或 - 表示 stdin")
    args = ap.parse_args()

    if args.cmd == "list":
        return _run_list()
    if args.cmd == "set":
        return _run_set(args)
    if args.cmd == "unset":
        return _run_unset(args)
    if args.cmd == "apply":
        return _run_apply(args)
    return _run_page(args)


if __name__ == "__main__":
    sys.exit(main())
