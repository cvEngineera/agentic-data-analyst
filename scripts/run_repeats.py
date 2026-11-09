"""多次重复实验：``LLM_MODE=real python scripts/run_repeats.py --runs 3``

为什么要跑多轮（对应论文「非确定性」一节）
------------------------------------------
v0.6 实测发现：消融里的 full 组 64.6%（93/144）与单独全量评估 65.3%（94/144）
只差 1 题，但温度已经置 0。LLM 应用的输出天然带抖动，只报一次最好看的数
在学术上站不住。本脚本在**同一评估集、同一配置**下重复跑 N 轮，给出总体与
分层的均值、样本标准差、最小/最大值，作为论文里「多次运行取均值并报告方差」
的原始数据。

设计取舍：
- 直接复用 ``evaluation.runner.evaluate()``，不另写一套判定逻辑，避免两处口径漂移；
- ``evaluate()`` 只返回数据不写报告，所以多轮之间不会互相覆盖；
- 每轮结果连同耗时、步数、成本一起留存，便于后续算置信区间；
- 报告写到 ``eval/real_repeats.json``（入库），逐题明细仍在本地 data/reports/。

成本提示：每轮 144 次左右模型调用，3 轮约 9 分钟，跑之前确认额度。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.evaluation.runner import evaluate, load_cases  # noqa: E402

OUT = ROOT / "eval" / "real_repeats.json"


def _stats(values: list[float]) -> dict:
    """一组准确率的描述统计；样本标准差在 n<2 时记 0，避免抛异常。"""
    return {
        "mean": round(statistics.fmean(values), 4),
        "std": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "range_pp": round((max(values) - min(values)) * 100, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="重复轮数，默认 3")
    ap.add_argument("--cases", default="src/evaluation/cases_eval.jsonl",
                    help="评估集路径，默认 144 条正式集")
    args = ap.parse_args()

    if settings.llm_mode != "real":
        print("需要 LLM_MODE=real；当前是 mock（离线），重复实验没有意义。")
        sys.exit(1)

    cases = load_cases(Path(args.cases))
    print(f"重复实验：{args.runs} 轮 × {len(cases)} 题 | 模式 real | {settings.model}", flush=True)

    rounds: list[dict] = []
    per_cat: dict[str, list[float]] = {}
    for i in range(1, args.runs + 1):
        t0 = time.time()
        _, summary = evaluate(cases)
        elapsed = round(time.time() - t0, 1)
        acc = summary["accuracy"]
        rounds.append({
            "round": i, "passed": summary["passed"], "total": summary["total"],
            "accuracy": acc, "elapsed_s": elapsed,
            "avg_steps": summary.get("avg_steps"),
            "by_category": {k: v["accuracy"] for k, v in summary["by_category"].items()},
        })
        for cat, v in summary["by_category"].items():
            per_cat.setdefault(cat, []).append(v["accuracy"])
        print(f"  第 {i} 轮：{summary['passed']}/{summary['total']} = {acc:.1%}  "
              f"({elapsed}s)", flush=True)

    report = {
        "dataset": "cases_eval.jsonl（144 条独立人工标注）",
        "mode": "real", "model": settings.model,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "runs": len(rounds),
        "overall": _stats([r["accuracy"] for r in rounds]),
        "by_category": {cat: _stats(vals) for cat, vals in sorted(per_cat.items())},
        "rounds": rounds,
        "note": ("样本标准差；温度置 0 仍存在抖动。论文应报告均值 ± 标准差，"
                 "不要只引用单轮结果。v0.6 的单独评估 65.3% 与消融 full 组 64.6% "
                 "可作为历史参照，但不纳入本报告的统计量。"),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    o = report["overall"]
    print(f"\n汇总：{o['mean']:.1%} ± {o['std']:.1%} "
          f"（min {o['min']:.1%} / max {o['max']:.1%}，极差 {o['range_pp']}pp）")
    print(f"报告：{OUT}")


if __name__ == "__main__":
    main()
