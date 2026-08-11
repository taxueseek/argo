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
