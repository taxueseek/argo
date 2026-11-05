#!/usr/bin/env python3
"""job.py 求职搜索测试：地区展开、三级判定、白名单校验、时效标记。

单元测试无网络依赖；live 集成测试在 API key 存在时执行（用于回归「精确率」
声明，避免 SKILL.md 中的实测数据不可复现）。
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "scripts"))
import job  # noqa: E402


# ── 地区展开 ────────────────────────────────────────────────────────────

class TestRegionWords:
    def test_district_expansion(self):
        """昆山（县级市）→ 昆山 + 苏州 + 江苏。"""
        w = job.region_words("昆山")
        assert "昆山" in w and "苏州" in w and "江苏" in w

    def test_city_expansion(self):
        """成都（地级市）→ 含各区县。"""
        w = job.region_words("成都")
        assert "成都" in w and "武侯" in w and "四川" in w

    def test_province_expansion(self):
        """四川（省）→ 覆盖各地市。"""
        w = job.region_words("四川")
        assert "四川" in w and "成都" in w

    def test_overseas_country(self):
        """新加坡 → 中英文展开。"""
        w = job.region_words("新加坡")
        assert "新加坡" in w and "Singapore" in w

    def test_overseas_city(self):
        """Tokyo → 东京 + 日本。"""
        w = job.region_words("Tokyo")
        assert "Tokyo" in w and "日本" in w

    def test_single_char_filtered(self):
        """单字词过滤（避免「东」误判其他地名）。"""
        assert all(len(x) >= 2 for x in job.region_words("昆山"))


class TestIsOverseas:
    def test_country(self):
        assert job.is_overseas("新加坡") is True
        assert job.is_overseas("日本") is True

    def test_domestic(self):
        assert job.is_overseas("昆山") is False
        assert job.is_overseas("苏州") is False


# ── 三级判定 ────────────────────────────────────────────────────────────

class TestJudge:
    WORDS = ["昆山", "苏州", "江苏"]

    def test_title_hit_l1(self):
        item = {"title": "工艺工程师-昆山沪光汽车电器招聘", "url": "https://www.zhipin.com/job_detail/x", "snippet": ""}
        assert job.judge(item, self.WORDS)[0] == 1

    def test_gov_source_l2(self):
        """政府源：无标题命中时给 L2 而非剔除。"""
        item = {"title": "工艺工程师-职位详细", "url": "https://hrss.suzhou.gov.cn/szxyyc/job/show-1.html", "snippet": ""}
        assert job.judge(item, self.WORDS)[0] == 2

    def test_gov_source_title_l1(self):
        item = {"title": "昆山招聘-苏州人社局", "url": "https://hrss.suzhou.gov.cn/job/1.html", "snippet": ""}
        assert job.judge(item, self.WORDS)[0] == 1

    def test_snippet_head_l2(self):
        """摘要头部命中（职位信息块）→ L2 保留。"""
        item = {"title": "工艺工程师", "url": "https://www.zhipin.com/job_detail/x",
                "snippet": "20-25K 工艺工程师 昆山 3-5年 本科 电路设计"}
        assert job.judge(item, self.WORDS)[0] == 2

    def test_snippet_tail_l3_strict_dropped(self):
        """公司简介/福利文本含地区词（v1 假阳性场景）→ L3，严格模式剔除。"""
        item = {"title": "工艺工程师", "url": "https://www.zhipin.com/job_detail/x",
                "snippet": "五险一金 节日福利 带薪年假 弹性工作 团队建设 定期体检 员工培训 "
                            "包吃包住 补充医疗保险 绩效奖金 年终奖 交通补贴 住房补贴 通讯补贴 "
                            "加班补助 高温补贴 采暖补贴 餐饮补贴 全勤奖 工龄奖 季度奖 半年奖 "
                            "十三薪 十四薪 员工旅游 生日福利 节日礼品 免费班车 免费食堂 免费停车。"
                            "公司是昆山半导体行业协会会长单位，总部位于上海。"}
        level, _ = job.judge(item, self.WORDS)
        assert level == 3

    def test_no_hit_l0(self):
        item = {"title": "工艺工程师", "url": "https://www.zhipin.com/job_detail/x",
                "snippet": "负责新产品导入，工艺优化。"}
        assert job.judge(item, self.WORDS)[0] == 0

    def test_no_city_all_l1(self):
        """无 --city 时全部 L1。"""
        item = {"title": "anything", "url": "https://x.com/1", "snippet": ""}
        assert job.judge(item, [])[0] == 1


# ── 白名单后置校验 ─────────────────────────────────────────────────────

class TestNorm:
    def test_core_platform(self):
        r = job._norm({"title": "t", "url": "https://www.zhipin.com/job_detail/x", "snippet": ""})
        assert r is not None and r["platform"] == "BOSS直聘"

    def test_extra_domain(self):
        r = job._norm({"title": "t", "url": "https://www.jobcn.com/job/1", "snippet": ""})
        assert r is not None and r["platform"] == "卓博人才网"

    def test_yingjiesheng_domain(self):
        """应届生求职网（校园招聘）白名单。"""
        r = job._norm({"title": "t", "url": "https://m.yingjiesheng.com/jobdetail/170536434", "snippet": ""})
        assert r is not None and r["platform"] == "应届生求职网"

    def test_gov_domain_in_whitelist(self):
        """白名单内政府域 → 具体标签优先。"""
        r = job._norm({"title": "t", "url": "https://hrss.suzhou.gov.cn/job/1", "snippet": ""})
        assert r is not None and r["platform"] == "苏州人社局"

    def test_gov_domain_generic(self):
        """白名单外政府域 → 通用人社局标签。"""
        r = job._norm({"title": "t", "url": "https://hrss.guangzhou.gov.cn/job/1", "snippet": ""})
        assert r is not None and r["platform"] == "人社局/政府"

    def test_free_domain(self):
        r = job._norm({"title": "t", "url": "https://remotive.com/remote-jobs/sales/1", "snippet": ""})
        assert r is not None and r["platform"] == "Remotive"

    def test_ashby_domain(self):
        r = job._norm({"title": "t", "url": "https://jobs.ashbyhq.com/notion/abc123", "snippet": ""})
        assert r is not None and r["platform"] == "Ashby"

    def test_unknown_domain_dropped(self):
        """非白名单 URL（v1 byted site: 混入场景）→ 剔除。"""
        assert job._norm({"title": "t", "url": "https://m.qcc.com/jobdetail/1", "snippet": ""}) is None
        assert job._norm({"title": "t", "url": "https://jy.scu.edu.cn/job/1", "snippet": ""}) is None

    def test_remoteok_uppercase_domain(self):
        """remoteOK.com 大写域名（实测）大小写不敏感匹配。"""
        r = job._norm({"title": "t", "url": "https://remoteOK.com/remote-jobs/x", "snippet": ""})
        assert r is not None and r["platform"] == "RemoteOK"

    def test_trusted_skips_whitelist(self):
        """trusted 直连源（SimplifyJobs/JobSpy/mcp-jobs）跳过白名单校验。"""
        r = job._norm({"title": "t", "url": "https://job-boards.greenhouse.io/captivation/jobs/1",
                       "snippet": "", "trusted": True, "_platform": "SimplifyJobs"})
        assert r is not None and r["platform"] == "SimplifyJobs"

    def test_new_domains_2026(self):
        """v4 新白名单：军队人才网/教育部公招/人社部（实测可达）。"""
        for url, label in [
                ("https://81rc.81.cn/job/1", "军队人才网"),
                ("https://jybzp.chsi.com.cn/zp/1", "教育部直属单位公招"),
                ("https://www.mohrss.gov.cn/x/1", "人社部事业单位招聘")]:
            r = job._norm({"title": "t", "url": url, "snippet": ""})
            assert r is not None and r["platform"] == label

    def test_date_extracted(self):
        r = job._norm({"title": "t", "url": "https://www.zhipin.com/job_detail/x",
                       "snippet": "发布时间：2026-08-01 岗位职责"})
        assert r["date"] == "2026-08-01"


# ── 时效 ────────────────────────────────────────────────────────────────

class TestFreshness:
    def test_extract_date_field(self):
        assert job.extract_date({"date": "2026-08-06"}) == "2026-08-06"
        assert job.extract_date({"publishedDate": "2026-08-06T07:15:58Z"}) == "2026-08-06"

    def test_extract_date_snippet(self):
        assert job.extract_date({"snippet": "更新时间 2026-7-1 电子工程师"}) == "2026-07-01"

    def test_no_date(self):
        assert job.extract_date({"snippet": "无日期"}) == ""

    def test_ts2date(self):
        """unix 时间戳 → 日期；非时间戳原样截断。"""
        assert job._ts2date("1750000000") == "2025-06-15"
        assert job._ts2date("not-a-ts") == "not-a-ts"

    def test_stale(self):
        assert job.is_stale("2020-01-01") is True
        assert job.is_stale("2026-08-01") is False
        assert job.is_stale("") is False


# ── SimplifyJobs 解析（v4 聚合源）───────────────────────────────────────

class TestSimplify:
    ROW = ('<tr><td>Company A</td><td><a href="https://x.com/job/1">Engineer Role</a></td>'
           '<td>Remote</td><td><a href="https://x.com/job/1">Apply</a></td><td>Sep 01, 2025</td></tr>')

    def test_row_parse(self):
        row = job._simplify_row(job._SIMPLIFY_TD.findall(self.ROW))
        assert row is not None
        assert row["company"] == "Company A"
        assert row["url"] == "https://x.com/job/1"
        assert row["date"] == "2025-09-01"

    def test_relative_date(self):
        assert job._simplify_date("0d") == date.today().isoformat()
        assert job._simplify_date("30d") == (date.today() - timedelta(days=30)).isoformat()

    def test_empty_row_skipped(self):
        assert job._simplify_row([]) is None
        assert job._simplify_row(["-", "-", "-", "-", "-"]) is None


# ── 结构化字段 ─────────────────────────────────────────────────────────

class TestParseFields:
    def test_salary_edu_exp_from_snippet(self):
        item = {"title": "电子工程师招聘_深圳某公司招聘",
                "snippet": "20-25K 电子工程师 深圳 5-10年 本科 电路设计"}
        f = job.parse_fields(item)
        assert f["salary"] == "20-25K"
        assert f["education"] == "本科"
        assert f["experience"] == "5-10年"

    def test_company_from_title(self):
        f = job.parse_fields({"title": "工艺工程师招聘_立臻科技(昆山)有限公司招聘"})
        assert "立臻科技" in f.get("company", "")

    def test_year_not_experience(self):
        """20xx 年份不误判为经验。"""
        f = job.parse_fields({"title": "x", "snippet": "发布时间 2004 年 岗位职责"})
        assert "experience" not in f

    def test_empty(self):
        assert job.parse_fields({"title": "普通标题", "snippet": ""}) == {}


# ── 指纹去重 ────────────────────────────────────────────────────────────

class TestFingerprint:
    def test_salary_normalized(self):
        a = {"title": "工艺工程师9000-14000元/月", "fields": {}, "url": "x"}
        b = {"title": "工艺工程师", "fields": {}, "url": "y"}
        assert job.fingerprint(a) == job.fingerprint(b)

    def test_cross_url_same_job(self):
        a = {"title": "工艺工程师- 昆山沪光汽车电器招聘", "url": "https://www.zhipin.com/a",
             "fields": {"company": "昆山沪光汽车电器"}}
        b = {"title": "工艺工程师- 昆山沪光汽车电器招聘", "url": "https://m.zhipin.com/b",
             "fields": {"company": "昆山沪光汽车电器"}}
        assert job.fingerprint(a) == job.fingerprint(b)


# ── 快照（watch 增量）───────────────────────────────────────────────────

class TestSnapshot:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(job, "JOBS_DIR", str(tmp_path))
        p = job.snapshot_path("工艺工程师", "昆山")
        job.save_snapshot(p, {"query": "q", "jobs": [{"fingerprint": "f1"}]})
        d = job.load_snapshot(p)
        assert d["jobs"][0]["fingerprint"] == "f1"

    def test_snapshot_path_stable(self):
        assert job.snapshot_path("a", "b") == job.snapshot_path("a", "b")

    def test_load_missing(self):
        assert job.load_snapshot("/nonexistent/x.json") is None


# ── 已知不可用源的分流（2026-09-16：skipped/errors 分账 + 配额状态机接线）──
#
# 这段逻辑专门处理「源这会儿用不了」与「本次调用失败」的区分：bocha 欠费时
# 它曾每次都真发请求、把 403 当硬错误写进 errors，调用方据此判成整体失败，
# 而实际上另外十来家源都正常返回。回归点有三：
#   1. 出发前已知不可用（配额耗尽/熔断打开）→ skipped，不进 errors，不真发请求；
#   2. 只有「源端明说配额耗尽」才交给配额状态机——403 反爬/429 限流/401 失凭
#      各有归属（熔断器自愈），误标的代价是按月级排除且 route 用同一状态筛
#      combo，30 天少一个源；
#   3. 熔断 half_open 保留探测资格（与 route._filter_breaker_blocked 同语义），
#      探测态若被当死源跳过，半开态没有终结者，永不收敛。


class _FakeQuotaManager:
    def __init__(self, hard_down=False):
        self._hard_down = hard_down
        self.marked = []

    def is_hard_down(self, name):
        return self._hard_down

    def mark_remote_exhausted(self, name, reason="", category=None):
        self.marked.append((name, reason))


class _FakeBreaker:
    def __init__(self, state="closed", cooldown_remain=0):
        self._state = state
        self._cooldown = cooldown_remain

    def status(self, name):
        return {"state": self._state, "cooldown_remain": self._cooldown}


def _patch_states(monkeypatch, hard_down=False, breaker=None):
    """把配额/熔断两处状态源换成假实现；默认全部放行（closed + 不欠费）。"""
    import quota
    import circuit_breaker
    qm = _FakeQuotaManager(hard_down=hard_down)
    bk = breaker or _FakeBreaker()
    monkeypatch.setattr(quota, "get_quota_manager", lambda: qm)
    monkeypatch.setattr(circuit_breaker, "get_breaker", lambda: bk)
    return qm


class TestBackendDown:
    """_backend_down：出发前的可用性判据，六态逐一锁定。"""

    def test_quota_hard_down_skips(self, monkeypatch):
        _patch_states(monkeypatch, hard_down=True)
        assert job._backend_down("bocha") is True

    def test_breaker_disabled_skips(self, monkeypatch):
        _patch_states(monkeypatch, breaker=_FakeBreaker(state="disabled"))
        assert job._backend_down("bocha") is True

    def test_breaker_open_with_cooldown_skips(self, monkeypatch):
        _patch_states(monkeypatch, breaker=_FakeBreaker(state="open", cooldown_remain=30))
        assert job._backend_down("bocha") is True

    def test_breaker_open_cooldown_expired_runs(self, monkeypatch):
        """冷却已过：允许再试一次，跳过会把自愈通道一并堵死。"""
        _patch_states(monkeypatch, breaker=_FakeBreaker(state="open", cooldown_remain=0))
        assert job._backend_down("bocha") is False

    def test_breaker_half_open_keeps_probe_eligibility(self, monkeypatch):
        """half_open 是探测态，不是死态——跳过它探测就永不收敛。"""
        _patch_states(monkeypatch, breaker=_FakeBreaker(state="half_open", cooldown_remain=99))
        assert job._backend_down("bocha") is False

    def test_breaker_unavailable_fails_open(self, monkeypatch):
        """状态层读不到时不设卡：少跑一个源比全量误跳过代价小。"""
        _patch_states(monkeypatch)  # 配额层照常放行，隔离真实状态对断言的干扰
        import circuit_breaker
        def _boom():
            raise RuntimeError("state dir missing")
        monkeypatch.setattr(circuit_breaker, "get_breaker", _boom)
        assert job._backend_down("bocha") is False


class TestQuotaExhaustedClassification:
    """_is_quota_exhausted：只认配额耗尽，泛化的 403/429/401 不接手。"""

    def test_real_quota_message_hits(self):
        assert job._is_quota_exhausted(
            RuntimeError("403 You do not have enough money or package quota")) is True

    def test_cloudflare_block_is_not_quota(self):
        """同是 403：Cloudflare 拦截与欠费不同因。误标代价=月级少一个源。"""
        assert job._is_quota_exhausted(
            RuntimeError("403 Sorry, you have been blocked")) is False

    def test_rate_limit_is_not_quota(self):
        assert job._is_quota_exhausted(RuntimeError("429 Too Many Requests")) is False

    def test_auth_failure_is_not_quota(self):
        assert job._is_quota_exhausted(RuntimeError("401 Unauthorized")) is False


class TestMarkSourceDown:
    def test_marks_with_truncated_reason(self, monkeypatch):
        qm = _patch_states(monkeypatch)
        job._mark_source_down("bocha", "x" * 500)
        assert qm.marked and qm.marked[0][0] == "bocha"
        assert len(qm.marked[0][1]) == 160  # reason 截断，防长错误体进状态文件

    def test_state_failure_swallowed(self, monkeypatch):
        """记账失败不得反噬主流程：搜索结果比状态簿记重要。"""
        import quota
        def _boom():
            raise RuntimeError("state dir missing")
        monkeypatch.setattr(quota, "get_quota_manager", _boom)
        job._mark_source_down("bocha", "detail")  # 不抛即通过


class TestSearchSkippedSemantics:
    """search() 集成：跳过与失败分账，调用方能分清「没岗位」与「源挂了」。

    search() 的 engine 参数不切逗号（all/free/单名三态），多后端场景用
    engine="all" + 补丁 DEFAULT_ENGINES 表达。
    """

    @staticmethod
    def _ok_backend(query, num):
        return [{"title": "岗位", "url": "https://www.zhipin.com/job_detail/abc.html",
                 "snippet": "职责", "publish_date": "2026-09-01"}]

    def test_down_backend_reported_in_skipped_not_errors(self, monkeypatch):
        _patch_states(monkeypatch)
        monkeypatch.setattr(job, "DEFAULT_ENGINES", ["bad", "good"])
        monkeypatch.setattr(job, "ALL_BACKENDS",
                            {"bad": self._ok_backend, "good": self._ok_backend})
        monkeypatch.setattr(job, "_backend_down", lambda name: name == "bad")
        r = job.search("电商运营", engine="all", num=2)
        assert r["skipped"] == ["bad"]
        assert r["errors"] == []
        assert r["total"] >= 1

    def test_quota_exhausted_failure_not_in_errors(self, monkeypatch):
        """真欠费：标记状态机、不写 errors——它不是本次失败，是周期内不可用。"""
        qm = _patch_states(monkeypatch)

        def dead(query, num):
            raise RuntimeError("You do not have enough money or package quota")

        monkeypatch.setattr(job, "DEFAULT_ENGINES", ["dead", "alive"])
        monkeypatch.setattr(job, "ALL_BACKENDS",
                            {"dead": dead, "alive": self._ok_backend})
        r = job.search("电商运营", engine="all", num=2)
        assert [m[0] for m in qm.marked] == ["dead"]
        assert r["errors"] == []
        assert r["total"] >= 1

    def test_regular_failure_still_lands_in_errors(self, monkeypatch):
        """普通失败（如反爬）如实进 errors：调用方必须看得见。"""
        _patch_states(monkeypatch)

        def blocked(query, num):
            raise RuntimeError("403 Sorry, you have been blocked")

        monkeypatch.setattr(job, "DEFAULT_ENGINES", ["flaky"])
        monkeypatch.setattr(job, "ALL_BACKENDS", {"flaky": blocked})
        r = job.search("电商运营", engine="all", num=2)
        assert r["errors"] and "blocked" in r["errors"][0]
        assert r["skipped"] == []


# ── live 集成（有 key 时执行，回归精确率声明）──────────────────────────

def _has_keys():
    return all(os.environ.get(k) for k in
               ("EXA_API_KEY", "TAVILY_API_KEY", "WEB_SEARCH_API_KEY",
                "BOCHA_API_KEY", "OCTEN_API_KEY"))


@pytest.mark.skipif(not _has_keys(), reason="需要 API key")
class TestLivePrecision:
    """三城市严格模式：L1/L2 占比 ≥ 90%，L3 必须为 0（严格模式已剔除）。"""

    CASES = [("工艺工程师", "昆山"), ("焊工", "上海"), ("会计", "新加坡")]

    @pytest.mark.parametrize("query,city", CASES)
    def test_precision(self, query, city):
        import json
        import subprocess
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                              "scripts", "job.py")
        out = subprocess.run([sys.executable, script, query, "--city", city, "--json"],
                             capture_output=True, text=True, timeout=180)
        d = json.loads(out.stdout)
        # 允许外部源偶发 5xx（如 himalayas 服务端故障），其余错误不可接受
        hard_errors = [e for e in d["errors"] if "HTTP Error 5" not in e]
        assert hard_errors == [], f"后端错误: {d['errors']}"
        assert d["total"] > 0, f"{city} 无结果"
        l1l2 = sum(1 for r in d["results"] if r["hit_level"] in (1, 2))
        assert l1l2 / d["total"] >= 0.9, f"{city} L1/L2 占比不足"
        assert all(r["hit_level"] in (1, 2) for r in d["results"]), f"{city} 有 L3 混入"
