"""消融实验框架（对应开题报告「5.1 实验法」「七、创新点三」）。

目的：把系统的每一层做成**可独立开关**，用同一套测试集逐组跑，
量化「混合检索 / 外键一跳扩展 / 报错自纠」各贡献多少个百分点。
这正是开题报告里说的「采用控制变量方式，逐项分析增益来源」。

五组配置：
    full                 全部开启（完整方案，作为对照基准）
    no_fk_expansion      关掉外键一跳扩展
    no_retrieval         关掉 Schema 检索，把全部表塞进提示词
    no_self_correction   关掉报错回喂重试
    minimal              三层全关，等价于「固定提示 + 单轮生成」基线

⚠️ 关于 mock 模式下的结果（必须如实说明）：
    mock 生成用的是规则模板，**不读 schema_context、也不会生成错误 SQL**，
    因此这三个开关对最终准确率几乎没有影响，各组数字会相同或接近。
    这说明的不是「模块没用」，而是「规则模板绕过了这些模块」。
    本轮跑通的是消融**管道**；真实贡献必须在 ``LLM_MODE=real`` 下重跑，
    那时 no_retrieval 会引入噪声、no_self_correction 会让报错无法恢复，
    差异才会显现。报告里会带上 interpretation 字段提醒这一点。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..config import CASES_PATH, REPORT_DIR, ensure_dirs, settings
from ..db.core import connect, introspect
from ..schema_retrieval.catalog import build_schema_context, retrieve
from .runner import evaluate, load_cases

# 正式评估集（104 条），比 15 条冒烟集更适合做分组统计
EVAL_CASES = CASES_PATH.parent / "cases_eval.jsonl"

CONFIGS: dict[str, dict[str, bool]] = {
    "full": {"fk": True, "retrieval": True, "correction": True},
    "no_fk_expansion": {"fk": False, "retrieval": True, "correction": True},
    "no_retrieval": {"fk": True, "retrieval": False, "correction": True},
    "no_self_correction": {"fk": True, "retrieval": True, "correction": False},
    "minimal": {"fk": False, "retrieval": False, "correction": False},
}


def _snapshot() -> dict[str, bool]:
    """保存当前三个开关的值，实验结束后要恢复，避免污染后续评估。"""
    return {"fk": settings.ablation_fk_expansion,
            "retrieval": settings.ablation_schema_retrieval,
            "correction": settings.ablation_self_correction}


def _apply(flags: dict[str, bool]) -> None:
    """把一组开关写到全局 settings 实例上（各模块读的是同一个对象）。"""
    settings.ablation_fk_expansion = flags["fk"]
    settings.ablation_schema_retrieval = flags["retrieval"]
    settings.ablation_self_correction = flags["correction"]


def run_ablation(cases_path: Path | None = None) -> dict:
    """依次跑五组配置，返回含分层结果与 delta 的报告字典。

    Args:
        cases_path: 测试集路径；默认用 104 条正式评估集。

    Returns:
        报告字典，可由调用方写成 JSON。不直接写文件，便于单测复用。
    """
    cases = load_cases(cases_path or EVAL_CASES)
    original = _snapshot()
    results: dict[str, dict] = {}

    try:
        for name, flags in CONFIGS.items():
            _apply(flags)
            # 过程探针：用第一道题量一下「注入提示词的表结构有多长」。
            # no_retrieval 会变成全表 DDL、no_fk_expansion 会少掉扩展表，
            # 即使 mock 下准确率不变，也能证明开关确实作用到了链路上。
            conn = connect(readonly=True)
            try:
                probe_tables = list(introspect(conn))
            finally:
                conn.close()
            probe_hit = retrieve(cases[0]["question"] if cases else "", probe_tables)
            probe_ctx = (probe_hit.context if settings.ablation_schema_retrieval
                         else build_schema_context(probe_tables))
            # evaluate 只返回数据、不写文件，五组之间不会互相覆盖报告
            details, summary = evaluate(cases)
            results[name] = {
                "schema_context_chars": len(probe_ctx),
                "flags": flags,
                "total": summary["total"],
                "passed": summary["passed"],
                "accuracy": summary["accuracy"],
                "by_category": {k: v["accuracy"]
                                for k, v in summary["by_category"].items()},
                "avg_steps": summary["avg_steps"],
                "avg_elapsed_ms": summary["avg_elapsed_ms"],
                # 过程指标：即使 mock 下准确率无差异，也能看到开关确实生效
                "denied_count": sum(1 for d in details if d["denied"]),
                "fallback_count": sum(1 for d in details if d["fallback_used"]),
            }
    finally:
        # 无论中途是否异常，都要把配置还原，否则后续 CLI/评估行为会变
        _apply(original)

    base = results["full"]["accuracy"]
    delta: dict[str, dict] = {}
    for name, res in results.items():
        if name == "full":
            continue
        delta[name] = {
            "overall_pp": round((res["accuracy"] - base) * 100, 2),
            "by_category_pp": {
                cat: round((acc - results["full"]["by_category"].get(cat, 0)) * 100, 2)
                for cat, acc in res["by_category"].items()
            },
        }

    mock_note = (
        "当前为 mock 模式：规则模板不读 schema_context、不产生错误 SQL，"
        "因此各开关对准确率影响有限，本轮结果用于验证消融管道可运行、可复现。"
        "接真实模型后请把 LLM_MODE 改为 real 重跑，那时的 delta 才是论文可用的消融结论。"
    )
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "llm_mode": settings.llm_mode,
        "cases_file": str(cases_path or EVAL_CASES),
        "cases_count": len(cases),
        "configs": results,
        "delta_vs_full": delta,
        "interpretation": mock_note if settings.llm_mode == "mock" else
        "真实模型模式：delta 为各模块相对完整方案的准确率变化（百分点）。",
    }


def print_report(report: dict) -> None:
    """终端输出对比表：配置 / 准确率 / 相对 full 的百分点 / 过程指标。"""
    print(f"\n消融实验 | 模式 {report['llm_mode']} | {report['cases_count']} 题\n")
    header = (f"{'配置':<22}{'通过':<10}{'准确率':<10}{'Δ vs full':<14}"
              f"{'上下文字符':<12}{'平均步数':<10}{'拦截':<8}{'兜底':<8}")
    print(header)
    print("-" * len(header))
    for name, res in report["configs"].items():
        d = report["delta_vs_full"].get(name, {}).get("overall_pp")
        delta = "-" if d is None else f"{d:+.2f}pp"
        print(f"{name:<22}{str(res['passed']) + '/' + str(res['total']):<10}"
              f"{res['accuracy']:.1%}      {delta:<14}"
              f"{res['schema_context_chars']:<12}{res['avg_steps']:<10}"
              f"{res['denied_count']:<8}{res['fallback_count']:<8}")
    print("\n" + report["interpretation"])


def main(cases_path: Path | None = None) -> dict:
    """跑完整流程并写报告文件；返回报告字典。"""
    ensure_dirs()
    report = run_ablation(cases_path)
    out = REPORT_DIR / "ablation_summary.json"
    out.write_text(json_dumps(report), encoding="utf-8")
    print_report(report)
    print(f"\n报告 {out}")
    return report


def json_dumps(obj) -> str:
    """单独抽出来是为了避免在文件顶部再多一个 import。"""
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)
