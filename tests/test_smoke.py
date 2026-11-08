"""烟雾测试：保证基线链路随时可跑通。

运行：``cd bishe-agent && python -m unittest discover -s tests -v``

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
