#!/usr/bin/env python3
"""engine_admission 契约测试：准入状态的 blocked/reason 必须自洽。

## 守的是什么缺陷

`record_validation` 曾写 `reason = reason or current.get("reason")`——把**历史**
失败原因粘滞下来。典型触发序列（批次九实测）：

  1. `engine_validate --stage health` 首次失败 → 留下 `reason=health_failed`
  2. 修好后再跑 `--stage all --admit`：health/quality 双 pass、blocked=False
  3. 但本次 reason 为空 → 回退成历史的 `health_failed`
  4. 准入记录于是自相矛盾：`blocked=false`（或旧记录里 blocked=true）
     配 `reason=health_failed`，而 `health.status=pass`

后果：24 个引擎（批次九全部 + realtime_index）在
`~/.cache/unified-search/admission/*.json` 里被标 `blocked=true` 且
`reason=health_failed`，`routable=False` — 引擎「装了没通电」：能单跑、
能被显式调用，但永远不参与自动路由。这是批次九最重要的回归。

## 判据

- 本次未 block 时，reason 必须为空（不得残留历史失败原因）
- 本次 block 时，reason 不得为空
- blocked=False 的记录，其 health.status 不得是 fail（否则状态层自相矛盾）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestAdmissionReasonConsistency(unittest.TestCase):
    """reason 必须反映本次判定，不粘滞历史。"""

    def setUp(self):
        # 隔离状态目录：准入记录写入 ~/.cache 下，测试不得污染真实状态。
        #
        # 注意：**不要** importlib.reload(engine_admission)。reload 会在
        # sys.modules 里换掉模块对象，而 engine_status / engines 等模块在
        # 导入期已持有旧对象的引用——同进程后续用例（如
        # test_engine_catalog::test_routable_only_flag_actually_filters）
        # 会看到「CLI 子进程 191 个 routable」与「本进程旧模块 192 个」
        # 不一致而失败（实测污染，已定位）。改为 monkeypatch 模块级
        # 状态目录解析函数即可，不换模块对象。
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("ARGO_STATE_DIR")
        os.environ["ARGO_STATE_DIR"] = self._tmp.name
        import engine_admission
        self.mod = engine_admission
        # 状态目录若被模块缓存过，显式失效（不 reload 模块）
        for attr in ("_STATE_DIR_CACHE", "_state_dir_cache", "_DIR_CACHE"):
            if hasattr(self.mod, attr):
                setattr(self.mod, attr, None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("ARGO_STATE_DIR", None)
        else:
            os.environ["ARGO_STATE_DIR"] = self._old
        # 清掉本测试写入的隔离记录，并让后续用例重新读到原目录
        for attr in ("_STATE_DIR_CACHE", "_state_dir_cache", "_DIR_CACHE"):
            if hasattr(self.mod, attr):
                setattr(self.mod, attr, None)
        self._tmp.cleanup()

    def test_stale_reason_is_cleared_on_success(self):
        """核心回归：先失败留 reason，再成功必须清掉。"""
        m = self.mod
        # 1) 首次 health 失败
        m.record_validation("eng_x", stages_passed=["health"],
                            blocked=True, reason="health_failed",
                            health={"ok": False, "status": "fail"})
        rec = m.load_admission("eng_x")
        self.assertTrue(rec["blocked"])
        self.assertEqual(rec["reason"], "health_failed")

        # 2) 修好后重跑：health+quality 全 pass
        m.record_validation("eng_x", stages_passed=["health", "quality"],
                            quality_score=1.0, admit=True,
                            health={"ok": True, "status": "pass"},
                            quality={"ok": True, "quality_score": 1.0})
        rec = m.load_admission("eng_x")
        self.assertFalse(rec["blocked"], "本次通过必须解 block")
        self.assertEqual(rec["reason"], "",
                         "不得残留历史 health_failed（旧实现的粘滞 bug）")
        self.assertTrue(m.is_admitted("eng_x"))

    def test_blocked_record_always_has_reason(self):
        """被 block 时 reason 不得为空（否则无法归因）。"""
        m = self.mod
        m.record_validation("eng_y", stages_passed=["health"],
                            blocked=True)
        rec = m.load_admission("eng_y")
        self.assertTrue(rec["blocked"])
        self.assertTrue(rec["reason"], "blocked 必须带原因")

    def test_explicit_reason_is_kept(self):
        """显式传入的 reason 优先，不被覆盖。"""
        m = self.mod
        rec = m.record_validation("eng_z", stages_passed=["health"],
                                  blocked=True, reason="quota_exhausted")
        self.assertEqual(rec["reason"], "quota_exhausted")

    def test_stages_passed_are_merged(self):
        """分阶段跑（先 health 后 quality）必须累积，不覆盖。"""
        m = self.mod
        m.record_validation("eng_m", stages_passed=["health"],
                            health={"ok": True, "status": "pass"})
        rec = m.record_validation("eng_m", stages_passed=["quality"],
                                  quality_score=0.9)
        self.assertIn("health", rec["stages_passed"])
        self.assertIn("quality", rec["stages_passed"])

    def test_health_fail_blocks(self):
        """health 明确失败 → 必须 block。"""
        m = self.mod
        rec = m.record_validation("eng_f", stages_passed=["health"],
                                  health={"ok": False, "status": "fail"})
        self.assertTrue(rec["blocked"])


class TestAdmissionStateNotSelfContradictory(unittest.TestCase):
    """状态自洽检查：blocked 与 health.status 不得互相矛盾。

    这条同时看守真实状态目录——若存量记录里出现「blocked=true 但
    health.status=pass」的形态，说明粘滞 bug 回归或有人手改了状态文件。
    """

    def test_no_blocked_record_with_passing_health(self):
        """始终检查**真实**状态目录（不受隔离用例的 ARGO_STATE_DIR 影响）。

        此前的实现读 `os.environ.get("ARGO_STATE_DIR", ~/.cache/...)`，
        而同文件的前序用例会临时设置该变量并在 tearDown 里清掉——本用例
        于是指向临时目录、`is_dir()` 为假而被 skip，关键守卫实际从未运行
        （skip 比 fail 更危险：它看起来是「通过」）。改为直接问模块要
        真实状态目录，不经环境变量。
        """
        import engine_admission
        from pathlib import Path as _P

        try:
            # 模块的来源函数（不受测试环境变量污染）
            adm_dir = _P(str(engine_admission.admission_dir()))
        except Exception:
            adm_dir = _P(os.path.expanduser("~/.cache/unified-search")) / "admission"
        if not adm_dir.is_dir():
            self.skipTest("无准入状态目录（干净环境）")
        bad = []
        for p in adm_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            h = d.get("health") or {}
            eid = d.get("engine_id")
            reason = str(d.get("reason") or "")
            # 指纹 1（原）：blocked=true + reason=health_failed，但 health.status=pass
            # ——粘滞 bug 的形态，会让 routable=False。
            if d.get("blocked") and h.get("ok") is True and reason.startswith("health_failed"):
                bad.append(eid)
                continue
            # 指纹 2（新增）：blocked=false 却残留 reason。
            # 不变式是「blocked=False ⇒ reason 为空」（reason 语义 = 为什么不能用）。
            # 旧实现只在成功路径写 `validation_passed`，正是漏过指纹 1 的同类矛盾：
            # 测试只锁了 health_failed 一种值，换个值就溜过去了（实测线上 64 条）。
            # 这里锁**形状**而非某个字面量，避免下次再换一个词重演。
            if not d.get("blocked") and reason.strip():
                bad.append(f"{eid}(reason={reason!r})")
        self.assertFalse(
            bad,
            f"准入记录自相矛盾（会导致 routable/可读性判读错误）：{sorted(bad)}"
            f" —— blocked=true 不得配 health.status=pass；"
            f"blocked=false 不得残留任何 reason",
        )

    def test_validate_success_path_never_writes_reason(self):
        """engine_validate 成功路径不得写 reason（不变式的调用方一侧）。

        上面那条锁的是**状态文件**；这条锁**写入方**，两道一起才完整链路：
        只锁状态文件的话，下次改回写 validation_passed 要等状态目录被
        重新生成才会暴露。
        """
        import inspect
        import engine_validate
        src = inspect.getsource(engine_validate)
        # 允许出现在注释里，不允许出现在赋值语句里
        offenders = [
            ln.strip() for ln in src.splitlines()
            if "reason" in ln and "=" in ln
            and not ln.strip().startswith("#")
            and "validation_passed" in ln
        ]
        self.assertEqual(
            offenders, [],
            f"engine_validate 在 reason 赋值处写 validation_passed，"
            f"违反 blocked=False ⇒ reason 为空：{offenders}",
        )


class TestAdmissionReadCache(unittest.TestCase):
    """读缓存契约：一次 routable 扫描不该对同一份记录读三遍（实测 668 次读盘）。

    缓存与文件是一对状态，只更新一半就是经典的自相矛盾——所以这两条一起锁：
    重复读命中缓存、写入立刻失效。TTL 是跨进程的保底，也要能关。
    """

    def setUp(self):
        import engine_admission
        self.mod = engine_admission
        self._saved_ttl = os.environ.get("ARGO_ADMISSION_TTL_S")
        os.environ.pop("ARGO_ADMISSION_TTL_S", None)
        self.mod._admission_read_cache.clear()

    def tearDown(self):
        self.mod._admission_read_cache.clear()
        if self._saved_ttl is None:
            os.environ.pop("ARGO_ADMISSION_TTL_S", None)
        else:
            os.environ["ARGO_ADMISSION_TTL_S"] = self._saved_ttl

    def test_repeated_reads_hit_cache(self):
        m = self.mod
        m.save_admission("cache_probe_a", {"stages_passed": ["health"]})
        first = m.load_admission("cache_probe_a")
        second = m.load_admission("cache_probe_a")
        self.assertIs(first, second, "重复读取没有命中缓存——热路径又去读盘了")
        self.assertTrue(m.is_blocked("cache_probe_a") is False)

    def test_write_invalidates_cache(self):
        """写完必须立刻失效：读到自己没写的旧值是最难查的一类不一致。"""
        m = self.mod
        m.save_admission("cache_probe_b", {"stages_passed": ["health"]})
        before = m.load_admission("cache_probe_b")
        m.set_blocked("cache_probe_b", True, reason="manual")
        after = m.load_admission("cache_probe_b")
        self.assertIsNot(before, after, "写入后仍返回缓存中的旧对象")
        self.assertTrue(after["blocked"], "写入后读到的仍是旧值")
        self.assertFalse(m.is_blocked("cache_probe_c"))

    def test_ttl_zero_disables_cache(self):
        m = self.mod
        m.save_admission("cache_probe_d", {"stages_passed": ["health"]})
        os.environ["ARGO_ADMISSION_TTL_S"] = "0"
        try:
            first = m.load_admission("cache_probe_d")
            second = m.load_admission("cache_probe_d")
            self.assertIsNot(first, second, "TTL=0 应关闭记忆化（逐次读盘）")
        finally:
            os.environ.pop("ARGO_ADMISSION_TTL_S", None)

    def test_write_path_sees_external_updates(self):
        """读-改-写必须看到磁盘最新状态：缓存里的旧值会把并发写入的字段吞掉。

        审计实测的丢更新：子进程写入 stages_passed 的 quality，父进程在 TTL 内
        读-改-写后只剩 health——准入记录是路由的输入，丢字段直接改变路由结果。
        """
        m = self.mod
        m.save_admission("cache_probe_e", {"stages_passed": ["health"]})
        m.load_admission("cache_probe_e")          # 故意先把旧值读进缓存
        path = m.admission_path("cache_probe_e")
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["stages_passed"] = ["health", "quality"]   # 模拟另一个进程的写入
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        out = m.record_validation("cache_probe_e", stages_passed=["search"])
        self.assertIn("quality", out["stages_passed"],
                      f"并发写入的 quality 被缓存旧值吞掉：{out['stages_passed']}")

    def test_fresh_read_refreshes_cache(self):
        """load_admission_fresh 读完要回填缓存，避免写路径每次都重读。"""
        m = self.mod
        m.save_admission("cache_probe_f", {"stages_passed": ["health"]})
        m.load_admission_fresh("cache_probe_f")
        self.assertIs(m.load_admission("cache_probe_f"),
                      m.load_admission("cache_probe_f"),
                      "fresh 读之后缓存没回填，后续读又去读盘")


class TestAdmissionExistsSet(unittest.TestCase):
    """存在集合短路：无记录的引擎不得触发文件读（IO 形状门禁）。

    2026-10-01 实测：261 引擎只有 99 份准入记录，全量清单/route 扫描逐引擎
    open，62% 注定 ENOENT。本机 SSD 全扫描 2.8ms 无感，但慢 IO 环境（沙箱/
    CI/网络盘）单次 syscall 可放大到毫秒级（同一清单在沙箱实测 4s+）。修复后
    文件读次数应随**记录数**走，而不是引擎数——这条门禁锁的就是那个形状，
    谁把 O(记录数) 改回 O(引擎数)，这里必红。锁四件事：
      1. 混合目录里扫 N 个引擎，文件读次数 == 有记录的引擎数（红绿主断言）；
      2. TTL 内第二轮扫描零读；
      3. 写入后立即可见（读己所写，不等 TTL）；
      4. TTL=0 与目录切换时集合不得越界（退回逐次精确读）。
    """

    def setUp(self):
        import engine_admission as m
        self.mod = m
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = m.DEFAULT_ADMISSION_DIR
        m.DEFAULT_ADMISSION_DIR = Path(self._tmp.name)
        self._saved_ttl = os.environ.get("ARGO_ADMISSION_TTL_S")
        os.environ["ARGO_ADMISSION_TTL_S"] = "1"
        m._admission_read_cache.clear()
        m._admission_dir_set_cache.clear()
        # load_admission 只经 _read_admission_file 触碰磁盘，包一层计数即可。
        # 不用 sys.addaudithook：审计钩子挂上就摘不掉，会污染整个测试会话。
        self._orig_read = m._read_admission_file
        self.counter = {"n": 0}

        def counting(path):
            self.counter["n"] += 1
            return self._orig_read(path)

        m._read_admission_file = counting

    def tearDown(self):
        self.mod._read_admission_file = self._orig_read
        self.mod._admission_read_cache.clear()
        self.mod._admission_dir_set_cache.clear()
        self.mod.DEFAULT_ADMISSION_DIR = self._old_dir
        if self._saved_ttl is None:
            os.environ.pop("ARGO_ADMISSION_TTL_S", None)
        else:
            os.environ["ARGO_ADMISSION_TTL_S"] = self._saved_ttl
        self._tmp.cleanup()

    def test_missing_records_do_not_open_files(self):
        m = self.mod
        have = [f"set_e{i}" for i in range(5)]
        for e in have:
            m.save_admission(e, {"stages_passed": ["health"]})
        ids = have + [f"set_x{i}" for i in range(7)]  # 12 引擎 5 记录，7 个必扑空
        for e in ids:
            m.load_admission(e)
        self.assertEqual(
            self.counter["n"], len(have),
            "无记录引擎触发了文件读——存在集合短路失效，IO 又回到 O(引擎数)",
        )
        self.counter["n"] = 0
        for e in ids:
            m.load_admission(e)
        self.assertEqual(self.counter["n"], 0, "TTL 内第二轮扫描不应有任何文件读")

    def test_saved_record_visible_without_waiting_ttl(self):
        """先扫出「无记录」再写入：必须立即可见（读己所写）。"""
        m = self.mod
        self.assertIsNone(m.load_admission("set_late"))
        m.save_admission("set_late", {"stages_passed": ["health"]})
        self.assertIsNotNone(
            m.load_admission("set_late"),
            "写入后仍按「无记录」短路——存在集合没跟上写入",
        )

    def test_ttl_zero_disables_set(self):
        """TTL=0（全量套件的默认）：退回逐次精确读，每个引擎都真实读一次。"""
        m = self.mod
        have = [f"set_t{i}" for i in range(3)]
        for e in have:
            m.save_admission(e, {"stages_passed": ["health"]})
        os.environ["ARGO_ADMISSION_TTL_S"] = "0"
        m._admission_read_cache.clear()
        m._admission_dir_set_cache.clear()
        ids = have + ["set_t_missing"]
        for e in ids:
            m.load_admission(e)
        self.assertEqual(
            self.counter["n"], len(ids),
            "TTL=0 必须逐次读盘（旧语义逐位一致），存在集合不得擅自生效",
        )

    def test_dir_switch_does_not_reuse_stale_set(self):
        """状态目录切换（测试常规手段）：集合按目录隔离，A 目录的「无记录」不得带进 B。"""
        m = self.mod
        self.assertIsNone(m.load_admission("set_moved"))  # 在空目录 A 扫出「无记录」
        second = tempfile.TemporaryDirectory()
        try:
            m.DEFAULT_ADMISSION_DIR = Path(second.name)
            m.save_admission("set_moved", {"stages_passed": ["health"]})
            self.assertIsNotNone(
                m.load_admission("set_moved"),
                "切换目录后仍沿用旧目录的存在集合",
            )
        finally:
            second.cleanup()


if __name__ == "__main__":
    unittest.main()
