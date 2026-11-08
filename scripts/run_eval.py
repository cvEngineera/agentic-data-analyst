"""评估入口：``python scripts/run_eval.py [--compare 上次汇总.json]``

薄封装，实际逻辑在 src/evaluation/runner.py。拆成脚本是为了让
「一条命令重跑全套」这件事成为可写进 CI 的固定动作：
每完成一层开发跑一次，把 eval_summary.json 另存为 baseline_summary.json，
下次用 --compare 看分层 delta，这些累积记录就是论文的实验数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.runner import main  # noqa: E402

if __name__ == "__main__":
    main()
