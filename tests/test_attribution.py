"""归因链路回归测试（v0.14.1）。

背景：真实模型问「这周哪个教室空气质量最差，为什么」时，常直接生成
``LIMIT 1`` 的 SQL，只返回一行。旧版 runtime 用 ``len(rows) > 1`` 当
归因开关，单行结果会静默跳过追加查询，``conclusion`` 保持空字符串 ——
页面上就只剩一张表，看不到任何「为什么」。

这组测试钉住两件事：
1. 教室名能被正确挑出（不要求在第一列、不要求多行）；
2. 单行结果 + 分析型问题必须进入 ``execute_sql`` 追加查询并给出非空结论。

只用标准库 unittest 与 mock，不联网、不调模型。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.runtime import _pick_top_site, run  # noqa: E402
from src.config import settings  # noqa: E402
from src.sql.executor import RunResult  # noqa: E402

# 与 test_smoke.py 一致：测试钉死 mock，开发机 .env 写 real 也不受影响
settings.llm_mode = "mock"

QUESTION = "这周哪个教室空气质量最差，为什么"


class PickTopSiteTest(unittest.TestCase):
    """教室名定位：不依赖「第一列」「多行」这两个隐含假设。"""

    def test_single_row_name_in_first_column(self):
        cols = ["site_name", "avg_pm25", "avg_co2", "avg_tvoc", "sample_count"]
        rows = [("3号教室", None, 1385.06, None, 84)]
        self.assertEqual(_pick_top_site(cols, rows), "3号教室")

    def test_name_not_in_first_column(self):
        # 模型把 AVG(co2) 放在首列时也该找到教室名
        cols = ["avg_co2", "site_name", "sample_count"]
        rows = [(1385.06, "3号教室", 84)]
        self.assertEqual(_pick_top_site(cols, rows), "3号教室")

    def test_multi_row_still_works(self):
        cols = ["site_name", "avg_co2"]
        rows = [("3号教室", 1385.06), ("1号教室", 688.1)]
        self.assertEqual(_pick_top_site(cols, rows), "3号教室")

    def test_no_site_returns_empty(self):
        self.assertEqual(_pick_top_site(["avg_co2", "n"], [(560.0, 12)]), "")
        self.assertEqual(_pick_top_site([], []), "")


class SingleRowAttributionTest(unittest.TestCase):
    """核心回归：单行结果也必须回答「为什么」。"""

    def test_single_row_triggers_alarm_lookup(self):
        fake = RunResult(
            question=QUESTION,
            sql="SELECT s.name AS site_name, AVG(sd.co2) AS avg_co2 "
                "FROM sensor_data sd JOIN device d ON sd.device_id = d.id "
                "JOIN site s ON d.site_id = s.id GROUP BY s.name "
                "ORDER BY avg_co2 DESC LIMIT 1",
            source="mock",
            category="attribution",
            columns=["site_name", "avg_pm25", "avg_co2", "avg_tvoc", "sample_count"],
            rows=[("3号教室", None, 1385.06, None, 84)],
            attempts=1,
        )
        with patch("src.agent.runtime.run_with_retry", return_value=fake):
            result = run(QUESTION, mode="agent")

        actions = [t["action"] for t in result.trace]
        self.assertIn("execute_sql", actions,
                      "单行结果没有触发追加告警查询，归因链路退化了")
        self.assertTrue(result.conclusion.strip(),
                        "结论为空，页面上只会显示一张表")
        self.assertIn("校准", result.conclusion)
        self.assertGreaterEqual(result.steps, 4)

    def test_no_site_still_gives_non_empty_conclusion(self):
        """定位不到教室名时如实说明，不能静默返回空字符串。"""
        fake = RunResult(
            question=QUESTION,
            sql="SELECT AVG(co2) FROM sensor_data",
            source="mock",
            columns=["avg_co2", "n"],
            rows=[(560.0, 1680)],
            attempts=1,
        )
        with patch("src.agent.runtime.run_with_retry", return_value=fake):
            result = run(QUESTION, mode="agent")
        self.assertTrue(result.conclusion.strip())
        self.assertIn("未定位到具体教室", result.conclusion)


if __name__ == "__main__":
    unittest.main()
