"""消融实验入口：``python scripts/run_ablation.py [--cases 路径]``

薄封装，逻辑在 src/evaluation/ablation.py。五组配置跑完会在
data/reports/ablation_summary.json 留下分层准确率与 delta，
这些累积报告就是论文第七章消融实验的原始数据。

接真实模型后（.env 里 LLM_MODE=real）重跑一次，delta 才有学术意义；
mock 模式下主要用于验证管道可运行、配置可还原。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.evaluation.ablation import main  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="逐层消融实验")
    ap.add_argument("--cases", type=Path, default=None,
                    help="测试集 JSONL 路径，默认 104 条正式评估集")
    args = ap.parse_args()
    main(args.cases)
