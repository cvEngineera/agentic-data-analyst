"""烟雾测试：保证基线链路随时可跑通。

运行：``cd agentic-data-analyst && python -m unittest discover -s tests -v``

九个用例按「从底层到端到端」排列，任何一个挂了都能快速定位是哪一层：
    表结构 / Schema 检索 -> 校验层（危险语句、写操作、正常 SELECT、未知字段）
    -> 单轮查询 -> 多步归因 -> 端到端安全拦截

刻意只用标准库 unittest，不引入 pytest；也不依赖网络与 API Key，
因此在离线环境、CI 里都能直接跑。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 把项目根目录加进搜索路径，让 `python -m unittest discover -s tests`
# 在任意工作目录下都能 import src.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.runtime import run  # noqa: E402  （须在 sys.path 调整之后导入）
from src.db.core import connect, init_database, introspect  # noqa: E402
from src.schema_retrieval.catalog import retrieve  # noqa: E402
from src.sql.validator import validate  # noqa: E402


class TestBaseline(unittest.TestCase):
    """所有用例共享同一个已初始化的数据库，避免每条用例重复建库。"""
    @classmethod
    def setUpClass(cls) -> None:
        init_database()
        conn = connect(readonly=True)
        try:
            cls.schema = introspect(conn)
            cls.tables = list(cls.schema.keys())
        finally:
            conn.close()

    def test_tables_exist(self) -> None:
        for t in ("site", "device", "sensor_data", "alarm"):
            self.assertIn(t, self.tables)

    def test_schema_retrieval(self) -> None:
        hit = retrieve("3号教室昨天的平均温度是多少", self.tables)
        self.assertIn("sensor_data", hit.tables)
        self.assertTrue(hit.expanded)  # 外键一跳扩展应补上维表

    def test_dangerous_sql_denied(self) -> None:
        v = validate("DELETE FROM device;", self.schema)
        self.assertFalse(v.ok)
        self.assertTrue(v.denied)

    def test_write_sql_denied(self) -> None:
        v = validate("UPDATE sensor_data SET temperature = 0;", self.schema)
        self.assertTrue(v.denied)

    def test_normal_select_pass(self) -> None:
        v = validate("SELECT AVG(temperature) FROM sensor_data;", self.schema)
        self.assertTrue(v.ok, v.reason)
        self.assertIn("LIMIT", v.normalized.upper())

    def test_unknown_column_rejected(self) -> None:
        v = validate("SELECT not_a_column FROM sensor_data;", self.schema)
        self.assertFalse(v.ok)

    def test_single_round_returns_rows(self) -> None:
        res = run("3号教室昨天的平均温度是多少", mode="single")
        self.assertFalse(res.denied, res.error)
        self.assertEqual(res.error, "")
        self.assertTrue(res.rows)

    def test_attribution_finds_calibration(self) -> None:
        res = run("这周哪个教室空气质量最差，为什么", mode="agent")
        self.assertIn("校准", res.conclusion, res.conclusion)
        self.assertGreaterEqual(res.steps, 3)

    def test_dangerous_question_blocked_end_to_end(self) -> None:
        res = run("删除所有设备记录", mode="single")
        self.assertTrue(res.denied)


class TestUciAdapter(unittest.TestCase):
    """UCI 适配器的纯函数测试（v0.4）。

    刻意只测 resample / build_rows 这类不碰网络的纯函数：CI 里没有数据 zip，
    也不该为了跑单测去联网下载。完整数据源链路验证交给
    ``scripts/check_uci.py``，那条是显式执行的运维脚本。
    """

    @staticmethod
    def _records(n: int, co2: float = 600.0) -> list[dict]:
        """构造形状与 UCI 解析结果一致的假记录，数值固定便于断言。"""
        return [{
            "date": f"2015-02-02 00:{i % 60:02d}:00",
            "temperature": 20.0 + i / 1000,
            "humidity": 30.0,
            "light": 100.0,
            "co2": co2,
        } for i in range(n)]

    def test_resample_keeps_order_and_length(self) -> None:
        from src.db.uci_loader import resample
        recs = self._records(200)
        out = resample(recs, 50)
        self.assertEqual(len(out), 50)
        self.assertEqual(out[0]["date"], recs[0]["date"])
        self.assertEqual(out[-1]["date"], recs[-1]["date"])
        # 等间隔抽样必须保持时间顺序，否则趋势类问题会变成噪声
        self.assertEqual([r["temperature"] for r in out],
                         sorted(r["temperature"] for r in out))

    def test_resample_short_input_returns_all(self) -> None:
        from src.db.uci_loader import resample
        recs = self._records(10)
        self.assertEqual(resample(recs, 50), recs)

    def test_build_rows_shape_nulls_and_injected_anomaly(self) -> None:
        from datetime import datetime
        from src.db.uci_loader import EXPECTED_ROWS, build_rows
        rows, now = build_rows(self._records(4000), now=datetime(2026, 11, 9, 16, 0, 0))
        self.assertEqual(len(rows), EXPECTED_ROWS)
        self.assertEqual(now, datetime(2026, 11, 9, 16, 0, 0))
        # UCI 没有 tvoc/pm25，必须保持 NULL，不能推算填充
        for r in rows:
            self.assertIsNone(r[6])
            self.assertIsNone(r[7])
        # 3 号教室最近两天应有注入式异常；同一时刻其他教室保持原值
        # 注入阈值是 now-2days 的精确时刻（16:00），字符串比较必须带时分秒；
        # 只写日期会把 11-07 当天 00:00–15:30 尚未注入的行也算进来。
        cutoff = "2026-11-07 16:00:00"
        recent3 = [r for r in rows if r[1] == 3 and r[2] >= cutoff]
        recent_other = [r for r in rows if r[1] == 1 and r[2] >= cutoff]
        self.assertTrue(recent3)
        self.assertTrue(all(r[5] == 1070.0 for r in recent3))      # 600 + 470
        self.assertTrue(all(r[5] == 600.0 for r in recent_other))  # 其他教室不受影响


class TestSemanticRetrieval(unittest.TestCase):
    """轻量语义打分的纯函数测试（v0.7）。

    不联网、不依赖 chromadb / numpy：故意只测「范围、确定性、开关生效」，
    不预设某张表一定排第一——字符 n-gram 相似度的排序能力由对照实验
    （scripts/compare_semantic.py）在真实模型上量化，单测不做主观断言。
    """

    def test_score_range_and_determinism(self) -> None:
        from src.schema_retrieval.catalog import TABLE_META
        from src.schema_retrieval.semantic import semantic_score
        q = "3号教室昨天的平均温度是多少"
        for table in TABLE_META:
            s = semantic_score(q, table)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0)
            self.assertEqual(s, semantic_score(q, table))  # 缓存不改变结果

    def test_hybrid_score_toggle(self) -> None:
        from src.config import settings
        from src.schema_retrieval.catalog import hybrid_score
        q = "二氧化碳浓度偏高的时段"
        original = settings.enable_semantic_retrieval
        try:
            settings.enable_semantic_retrieval = False
            off = hybrid_score(q, "sensor_data")
            settings.enable_semantic_retrieval = True
            on = hybrid_score(q, "sensor_data")
            self.assertGreaterEqual(on, off)  # 语义分非负，开启不会拉低
        finally:
            settings.enable_semantic_retrieval = original


class TestLiveFeed(unittest.TestCase):
    """公开数据采集的纯函数测试（v0.8，不联网）。

    故意只测字段映射与时区转换：真实网络请求交给 refresh_live.py，
    单测在任何离线环境、CI 里都必须能跑，不能依赖外部 API 可用性。
    """

    def test_map_row_no_fabricated_columns(self) -> None:
        from src.db.live_feed import map_row
        obs = {"time": "2026-11-09T12:00", "temperature": 11.7,
               "humidity": 38.0, "pm25": 6.3}
        row = map_row(3, obs)
        self.assertEqual(row[0], 3)
        self.assertEqual(row[1], "2026-11-09 20:00:00")  # UTC -> UTC+8
        self.assertEqual(row[2], 11.7)
        self.assertEqual(row[3], 38.0)
        self.assertIsNone(row[4])   # co2：公开源没有，保持 NULL
        self.assertIsNone(row[5])   # tvoc：同上，绝不推算
        self.assertEqual(row[6], 6.3)

    def test_to_local_handles_midnight(self) -> None:
        from src.db.live_feed import _to_local
        self.assertEqual(_to_local("2026-11-09T16:30"), "2026-11-10 00:30:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
