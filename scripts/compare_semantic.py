"""语义检索开/关对照实验：``LLM_MODE=real python scripts/compare_semantic.py``

背景（对应开题报告 4.2「Schema 混合检索」）
------------------------------------------
v0.6 之前 ``hybrid_score()`` 里的向量分支只是注释，实际跑的是纯关键词 +
数据字典打分。v0.7 用 ``src/schema_retrieval/semantic.py`` 把语义打分真正
接进来（字符 n-gram + TF-IDF 余弦，零依赖）。本脚本在**同一套 144 条评估集、
同一个真实模型**下分别跑「关闭 / 开启」两组，量化这一层到底贡献多少。

为什么默认关闭、只在实验里开启：
    v0.6 已固化的 65.3%、五组消融、重复实验都是纯关键词口径。悄悄改默认
    会让那些数字失去可比性；把语义层作为可选增强单独报告，结论更干净。
    实验结束用 finally 把开关还原，不污染后续任何一次运行。

诚实边界：这里的语义打分不是 Chroma 的神经网络 embedding，效果上限低于
BGE / text-embedding。论文应写「因环境无法安装 chromadb，改用零依赖的
轻量语义匹配，接口与向量库一致，可替换」，不能写成已部署 Chroma。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.evaluation.runner import evaluate, load_cases  # noqa: E402

OUT = ROOT / "eval" / "semantic_compare.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="src/evaluation/cases_eval.jsonl",
                    help="评估集路径，默认 144 条正式集")
    args = ap.parse_args()

    if settings.llm_mode != "real":
        print("需要 LLM_MODE=real；mock 模式不消费 Schema 上下文，对比无意义。")
        sys.exit(1)

    cases = load_cases(Path(args.cases))
    original = settings.enable_semantic_retrieval
    results: dict[str, dict] = {}

    print(f"语义检索对照：2 组 × {len(cases)} 题 | 模式 real | {settings.model}", flush=True)
    try:
        for label, flag in (("keyword_only", False), ("with_semantic", True)):
            settings.enable_semantic_retrieval = flag
            t0 = time.time()
            _, summary = evaluate(cases)
            elapsed = round(time.time() - t0, 1)
            results[label] = {
                "passed": summary["passed"], "total": summary["total"],
                "accuracy": summary["accuracy"],
                "by_category": {k: v["accuracy"] for k, v in summary["by_category"].items()},
                "elapsed_s": elapsed,
            }
            print(f"  {label:<16} {summary['passed']}/{summary['total']} = "
                  f"{summary['accuracy']:.1%}  ({elapsed}s)", flush=True)
    finally:
        # 无论中途是否异常，都把开关还原成默认值，避免污染后续实验
        settings.enable_semantic_retrieval = original

    off, on = results["keyword_only"], results["with_semantic"]
    delta_pp = round((on["accuracy"] - off["accuracy"]) * 100, 2)
    by_cat_delta = {
        cat: round((on["by_category"].get(cat, 0) - v) * 100, 2)
        for cat, v in off["by_category"].items()
    }
    report = {
        "dataset": "cases_eval.jsonl（144 条独立人工标注）",
        "mode": "real", "model": settings.model,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "delta_overall_pp": delta_pp,
        "delta_by_category_pp": by_cat_delta,
        "configs": results,
        "note": ("语义打分 = 字符 n-gram + TF-IDF 余弦（零依赖），不是 Chroma 的"
                 "神经网络 embedding；接口一致，装得上向量库可直接替换。"
                 "若 delta ≤ 0，说明该轻量实现在本数据集上无增益，应如实报告，"
                 "不能删掉这组实验。"),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n语义层贡献：{delta_pp:+.2f}pp")
    print(f"报告：{OUT}")


if __name__ == "__main__":
    main()
