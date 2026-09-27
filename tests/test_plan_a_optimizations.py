#!/usr/bin/env python
"""路由预热的针对性测试。

历史沿革（2026-09-27）：本文件原有第二组用例 `TestConfigCachePickleCompat`，
把「config 磁盘缓存从 JSON 换成 pickle（schema 3→4）」当作既定优化来锁定。
该优化已被回退——实测 pickle 只省 0.3ms（dumps 0.70→0.51ms / loads 0.84→0.55ms），
却用「反序列化=执行任意代码」换掉了 `_json_round_trip_safe` 这条
「缓存不得改变语义」的安全守卫，且 0.3ms 在 80ms 固定开销里是噪声。
守卫一个已撤销的改动，等于让下一次想重新引入它的人拿到虚假的安全感，
故随改动一并删除，而不是留着让它红。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


# ── 1. 路由预热：已删除，改为「不许再塞回来」的门 ──────────────────────────────

# 上一版守卫的是「预热线程能在 500ms 内跑完」——那锁的是一个已被两次推翻的设计
# （daemon 线程版 → 同步版 → 删除）。与本文档头的 pickle 用例同一处置原则：
# 守卫一个已撤销的改动，等于让下一次想重新引入它的人拿到虚假的安全感。

_PREWARM_NEEDLE = 'match_domains("argo-prewarm"'


def _has_startup_prewarm(src: str) -> bool:
    """源码里是否又出现了启动期无条件预热。"""
    return _PREWARM_NEEDLE in src


class TestNoUnconditionalPrewarm:
    """启动期无条件预热必须保持删除状态（2026-09-27 实测）。

    删除理由（逐条都是本机实测，不是推断）：

    1. **route-cache 命中时 route 根本不编译域正则**。生产默认路径就是命中，
       交替 5 组实测：route 阶段有无预热都是 4.2ms，整次调用中位墙上时间
       107.8ms → 68.9ms。省下的 38.9ms（36%）正是这笔支出本体，即 100% 白付。
    2. **未命中时也预付错了对象**：profile 显示 route_query 首次调用贵在懒加载
       import 与构建 249 个引擎的注册表，先预热之后 route 阶段实测仍要 47–121ms。
    3. **未命中路径总额不变**：编译挪回 route 内部，仍然只付一次。

    所以任何「预热」要能通过的门槛是：证明它预热的正是 route 会在同一进程、
    同一路径上付的那一笔。
    """

    def test_search_cli_has_no_startup_prewarm(self):
        src = (SCRIPT_DIR / "search_cli.py").read_text(encoding="utf-8")
        assert not _has_startup_prewarm(src), (
            "search_cli 又出现了启动期预热。它在 route-cache 命中时是纯支出，"
            "未命中时 route 自己会付（见本类 docstring 的三条实测）")

    def test_gate_has_teeth(self):
        """变异：把预热塞回源码，判据必须报红——否则这门是摆设。"""
        mutated = ('def main():\n'
                   '    route.match_domains("argo-prewarm", get_domains(cfg))\n')
        assert _has_startup_prewarm(mutated), "造错样本没被抓住，静态判据失效"
        assert not _has_startup_prewarm("def main():\n    pass\n"), (
            "判据对正常源码误报")
