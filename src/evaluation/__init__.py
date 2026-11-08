"""评估体系（研究模块⑦）：分层测试集、批量运行、回归对比。

- cases.jsonl：一行一题，含 category / question / expected_sql 等字段，
  可逐周追加；安全类用 expect_deny，归因类用 expect_conclusion。
- runner.py：执行结果比对 + 分层准确率 + JSONL 明细 + --compare 回归 delta。

这是论文第七章实验数据的来源，也是 CI 回归门禁的雏形。
"""
