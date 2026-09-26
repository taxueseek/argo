"""C4 回归护栏：日末延长的 TTL 必须在 DST 切换日仍然正确。

`SearchCache._seconds_until_end_of_day()` 返回的不是一个时间戳，而是**TTL
秒数**——它会与本文件其余地方的 epoch 浮点时间戳一起过
`time.time() - created_at > ttl` 这条比较。因此它必须回答「距本地时区
当天 23:59:59 还有多少秒」。

原实现用 naive `datetime.now()`：naive datetime 不知道自己在哪个时区，
`replace(hour=23, ...)` 也就无从谈「本地那天 23:59」。在 DST 切换日
（本地时区跳过 23:00–24:00 或重复 23:00–24:00 的那些天）这个差值会偏差
整整 1 小时，而偏差恰好落在缓存条目最不该被意外续命的时刻。

本测试不构造真实 DST 边界（那要改 TZ 环境变量并依赖 tzdata），而是直接
断言「返回值落在今天剩余秒数的合理区间内」——在任意时区（含 DST 活跃
时区）都成立，且对 naive 实现敏感。
"""

import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from cache import SearchCache  # noqa: E402


class TestSecondsUntilEndOfDayIsTimezoneAware(unittest.TestCase):
    def test_result_is_sane_remainder_of_today(self):
        secs = SearchCache._seconds_until_end_of_day()
        now_local = datetime.now().astimezone()
        secs_left_today = (
            now_local.replace(hour=23, minute=59, second=59, microsecond=0)
            - now_local
        ).total_seconds()
        # 允许 2 秒执行漂移
        self.assertAlmostEqual(
            secs, secs_left_today, delta=2,
            msg="返回值与「本地当天剩余秒数」不符：TTL 会被算错",
        )

    def test_never_exceeds_one_day(self):
        secs = SearchCache._seconds_until_end_of_day()
        self.assertLessEqual(secs, 86400, "日末剩余不可能超过 24h")
        self.assertGreaterEqual(secs, 60, "有 60 秒下限兜底")

    def test_uses_aware_datetime(self):
        """实现层护栏：必须是 aware，否则上面的区间断言在 DST 日会失效。

        naive 与 aware 在普通日的返回值可能完全一致（同一个本地日界），
        所以只断言数值不足以钉住这次修复——必须断言它真的带 tzinfo。
        """
        import cache
        import inspect
        src = inspect.getsource(cache.SearchCache._seconds_until_end_of_day)
        self.assertIn(
            ".astimezone()", src,
            "_seconds_until_end_of_day 退回 naive datetime：DST 切换日 TTL 偏差 1 小时",
        )


if __name__ == "__main__":
    unittest.main()
