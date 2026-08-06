#!/usr/bin/env python3
"""local-seek 评估体系：断言式回归测试。

把「手工验证过的行为」固化为可重复的断言集，防止后续改动破坏既有能力。
仿 netresearch/file-search-skill 的 evals 思路，但直接在真实语料上跑 seek.py。

自污染防护：eval_seek.py 自身位于语料 ~/.agents/skills 内，且用例 args 里
含有测试词，因此所有用例统一注入 --exclude eval_seek.py 排除自身；
「扩展兜底/精确关闭」类用例的夹具词（抽取清洗）须保证在语料中零命中，
扩展召回由 2-gram（清洗 等）承担。

用法：
  python3 eval_seek.py                # 全量跑
  python3 eval_seek.py --filter 扩展  # 只跑名称含「扩展」的用例
  python3 eval_seek.py --list         # 列出所有用例

断言类型：
  expect_rc       期望退出码（未命中/报错为 1，正常命中为 0）
  expect_hit      输出必须包含的片段（通常是被命中的文件路径）
  expect_no_hit   输出必须不包含的片段（回归防噪音）
  expect_mode     期望的模式标注（fast / fast+扩展 / deep / structural）
  expect_msg      输出必须包含的提示文本（错误提示/结构信息）
  elapsed_lt      耗时上限（毫秒）
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

SEEK = Path(__file__).resolve().parent / "seek.py"
CORPUS = str(Path.home() / ".agents" / "skills")
LOCAL_SEEK = str(Path.home() / ".agents" / "skills" / "argo" / "sub-skills" / "local-seek")
README = str(Path.home() / ".agents" / "skills" / "README.md")

EVALS = [
    {
        "name": "基线-markdown",
        "args": ["markdown", "--path", CORPUS, "--max", "3"],
        "expect_hit": ["SKILL.md"],
        "elapsed_lt": 2000,
    },
    {
        "name": "基线-抓取",
        "args": ["抓取", "--path", CORPUS, "--max", "3"],
        "expect_hit": ["skill"],
        "elapsed_lt": 2000,
    },
    {
        "name": "精确优先-数据抓取",
        "args": ["数据抓取", "--path", CORPUS, "--max", "3"],
        "expect_mode": "fast",
        "expect_hit": ["数据抓取"],
        "elapsed_lt": 2000,
    },
    {
        "name": "扩展兜底-抽取清洗",
        "args": ["抽取清洗", "--path", CORPUS, "--max", "3"],
        "expect_mode": "fast+扩展",
        "expect_hit": ["清洗"],
        "elapsed_lt": 2000,
    },
    {
        "name": "exact关闭-抽取清洗",
        "args": ["抽取清洗", "--path", CORPUS, "--exact", "--max", "3"],
        "expect_rc": 1,
        "expect_msg": "未找到匹配",
    },
    {
        "name": "空格噪音回归",
        # 空格不应成为 pattern：若空格被当作搜索词，rg 会命中所有含空格的行（rc=0）。
        # 用全 gram 均为零命中的稀有词验证：正确行为是精确与扩展都无命中（rc=1）。
        "args": ["樯橹 灰飞烟灭", "--path", CORPUS, "--max", "3"],
        "expect_rc": 1,
        "expect_msg": "未找到匹配",
    },
    {
        "name": "regex回退-interface",
        "args": ["interface{}", "--path", CORPUS, "--max", "2"],
        "expect_hit": ["seek.py"],
        "elapsed_lt": 2000,
    },
    {
        "name": "PCRE2提示",
        "args": ["(?<=x)y", "--path", CORPUS, "--max", "2"],
        "expect_rc": 1,
        "expect_msg": "未编译 PCRE2",
    },
    {
        "name": "outline-结构",
        "args": ["--outline", str(SEEK)],
        "expect_msg": "结构",
    },
    {
        "name": "lines-按行读取",
        "args": ["--lines", "100-105", str(SEEK)],
        "expect_msg": "第 100-105 行",
    },
    {
        "name": "结构-裸except",
        "args": ["裸except", "--structural", "--path", CORPUS, "--max", "3"],
        "expect_hit": [".py"],
        "expect_mode": "structural",
    },
    {
        "name": "结构-空catch",
        "args": ["empty-catch", "--structural", "--path", CORPUS, "--max", "3"],
        "expect_hit": [".js"],
        "expect_mode": "structural",
    },
    {
        "name": "结构-函数定义",
        "args": ["function-def", "--structural", "--path", LOCAL_SEEK, "--max", "3"],
        "expect_hit": ["seek.py"],
        "expect_mode": "structural",
    },
    {
        "name": "结构-未知语义",
        "args": ["不存在语义", "--structural", "--path", CORPUS, "--max", "3"],
        "expect_rc": 1,
        "expect_msg": "未知结构查询",
    },
    {
        "name": "git-log",
        "args": ["--git-log", README],
        "expect_msg": "提交",
    },
    {
        "name": "git-blame",
        "args": ["--git-blame", "1", README],
        "expect_msg": "第 1 行",
    },
    {
        "name": "git-非仓库文件",
        "args": ["--git-log", str(Path.home() / ".agents" / "skills" / "argo" / "sub-skills" / "local-seek" / "scripts" / "seek.py")],
        "expect_msg": "无提交历史",
    },
    {
        "name": "count-分布",
        "args": ["html", "--path", CORPUS, "--count", "--max", "3"],
        "expect_msg": "命中",
    },
    {
        "name": "无命中提示",
        "args": ["樯橹灰飞烟灭", "--path", CORPUS, "--max", "3"],
        "expect_rc": 1,
        "expect_msg": "未找到匹配",
    },
]


def run_one(ev):
    # 注入 --exclude eval_seek.py：排除评估脚本自身（它含测试词，会自污染语料）
    cmd = [sys.executable, str(SEEK), "--exclude", "eval_seek.py"] + ev["args"]
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return ev["name"], ["超时 60s"], 60000, -1
    elapsed = int((time.time() - start) * 1000)
    out = proc.stdout
    fails = []
    if "expect_rc" in ev and proc.returncode != ev["expect_rc"]:
        fails.append(f"rc={proc.returncode} 应为 {ev['expect_rc']}")
    for frag in ev.get("expect_hit", []):
        if frag not in out:
            fails.append(f"缺少命中「{frag}」")
    for frag in ev.get("expect_no_hit", []):
        if frag in out:
            fails.append(f"不应命中「{frag}」")
    if "expect_mode" in ev and f"模式 {ev['expect_mode']}" not in out:
        fails.append(f"模式非「{ev['expect_mode']}」")
    if "expect_msg" in ev and ev["expect_msg"] not in out:
        fails.append(f"缺少提示「{ev['expect_msg']}」")
    if "elapsed_lt" in ev and elapsed > ev["elapsed_lt"]:
        fails.append(f"耗时 {elapsed}ms 超限 {ev['elapsed_lt']}ms")
    return ev["name"], fails, elapsed, proc.returncode


def main():
    ap = argparse.ArgumentParser(prog="eval_seek", description="local-seek 回归评估")
    ap.add_argument("--filter", default="", help="只跑名称含此关键字的用例")
    ap.add_argument("--list", action="store_true", help="列出用例")
    args = ap.parse_args()

    evals = [e for e in EVALS if args.filter in e["name"]] if args.filter else EVALS
    if args.list:
        for e in EVALS:
            print(f"  {e['name']}")
        return

    passed = failed = 0
    for ev in evals:
        name, fails, elapsed, rc = run_one(ev)
        if fails:
            failed += 1
            print(f"✗ {name} ({elapsed}ms): {'；'.join(fails)}")
        else:
            passed += 1
            print(f"✓ {name} ({elapsed}ms)")
    print(f"\nlocal-seek eval: {passed}/{len(evals)} 通过" + ("" if not failed else f"，{failed} 失败"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
