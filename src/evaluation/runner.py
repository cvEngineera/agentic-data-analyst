"""分层评估与回归门禁（对应开题报告「4.1（7）评估体系」「七、创新点三」）。

一条命令重跑全套测试集，产出两份文件：
    - ``data/reports/eval_detail.jsonl``  逐题明细（SQL、判定理由、耗时、步数）
    - ``data/reports/eval_summary.json``  分层准确率与汇总指标

加 ``--compare <上次 summary.json>`` 会输出分层 delta，作为 CI 回归门禁的雏形：
改了提示词、换了模型、加了检索模块之后，效果到底变好还是变差，用数字说话。

三类判定方式（对应不同题型，不能一刀切）：
    1. 普通查询：**执行结果比对**。跑标准 SQL 与生成 SQL，比较结果集是否一致。
       这比字符串比对 SQL 更客观——两条写法不同的 SQL 可能完全等价。
    2. 安全类：只看 ``denied``，因为根本不该有「结果」。
    3. 归因类：看结论是否命中预设原因关键词（当前是「校准」）。

⚠️ 已知偏差：mock 模式下测试集的标准 SQL 与生成模板同源，属于「自己考自己」，
跑出 100% 只证明工程链路通畅。真实准确率必须在 real 模式下、用重新设计的
测试集测量；评估集扩充到 100 条以上时也要重新设计判定口径。
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..agent.runtime import AgentResult, run as agent_run
from ..config import CASES_PATH, REPORT_DIR, ensure_dirs, settings
from ..db.core import connect, introspect
from ..sql.executor import execute
from ..sql.validator import validate


def load_cases(path: Path | None = None) -> list[dict]:
    """读取 JSONL 测试集。

    每行一个 JSON 对象；空行与以 # 开头的行跳过。
    用 JSONL 而不是 CSV：题目里常带逗号、引号和中文，JSONL 不会出现转义坑，
    而且可以一行一条地往里追加（README 建议每周补 20 条）。
    """
    p = path or CASES_PATH
    cases = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def _rows_equal(expected: list[tuple] | None, actual: list[tuple],
                abs_tol: float = 0.01) -> bool:
    """结果集比对：形状一致，数值按两位小数容差比较，其余值严格相等。

    为什么不能直接用 ``==``（v0.6 修正的测量偏差）：
        正式评估集的标准 SQL 普遍写 ``ROUND(AVG(...), 2)``，而模型生成的
        语义等价 SQL 常直接返回 ``AVG(...)`` 的完整浮点数。实测中
        ``22.58`` 与 ``22.575416666666666`` 被判成失败，导致明明正确的
        SQL 被系统性误判，真实准确率被严重低估。

    容差取 0.01 正好对齐标准答案自身的舍入精度，不是放水：
        - 字符串、日期、行数、列顺序仍然严格比较；
        - 只有数值列允许 ≤0.01 的差异，差 0.5 度的错误查询照样判错；
        - 容差来源可追溯（标准 SQL 的 ROUND 位数），论文里能写清楚。
    """
    if expected is None or len(expected) != len(actual):
        return False
    for exp_row, act_row in zip(expected, actual):
        if len(exp_row) != len(act_row):
            return False
        for a, b in zip(exp_row, act_row):
            a_num = isinstance(a, (int, float)) and not isinstance(a, bool)
            b_num = isinstance(b, (int, float)) and not isinstance(b, bool)
            if a_num and b_num:
                if abs(float(a) - float(b)) > abs_tol:
                    return False
            elif a != b:
                return False
    return True


def _run_expected(case: dict) -> list[tuple] | None:
    """执行标准 SQL，拿到「正确答案」的结果集。

    Returns:
        行元组列表；没有标准 SQL（安全类/归因类）或执行报错时返回 None。

    注意这里走 executor.execute（只读连接 + 超时），与被测链路用同一套
    执行环境，避免两边数据库状态不一致造成假阴性。
    """
    sql = case.get("expected_sql", "")
    if not sql:
        return None
    _, rows, err, _ = execute(sql)
    if err:
        return None
    return rows


def evaluate(cases: list[dict]) -> tuple[list[dict], dict]:
    """批量跑测试集并分层统计。

    Returns:
        ``(逐题明细列表, 汇总字典)``。

    汇总里除了总准确率，还保留 by_category 的分组准确率——这是论文里
    「按问题类型分层统计、定位失败集中在哪类任务」那张表的直接数据来源。
    avg_steps / estimated_cost_yuan 对应开题报告 4.2 目标表的两个指标。
    """
    details: list[dict] = []
    by_cat: dict[str, dict] = defaultdict(lambda: {"total": 0, "passed": 0})
    total_ms = 0.0
    total_steps = 0
    total_cost = 0.0

    # ---- 评估口径钉死 SQLite（v0.12）------------------------------------
    # 起因：v0.12 起演示链路可以切到 MySQL（DB_BACKEND=mysql），但 144 条
    # 用例的 expected_sql 全是 SQLite 方言（strftime / datetime('now','-1 day')），
    # 在 MySQL 上会整片语法错误，准确率会从 64.6% 掉到个位数——那不是模型退化，
    # 是量错了对象。所以评估入口强制把后端切回 sqlite，跑完恢复。
    # 这也解释了论文里那句「评估环境冻结 SQLite，MySQL 只承担部署」。
    saved_backend = settings.db_backend
    saved_vector = settings.enable_vector_retrieval
    saved_semantic = settings.enable_semantic_retrieval
    settings.db_backend = "sqlite"
    # v0.14：演示服务可以带 ENABLE_VECTOR_RETRIEVAL=1 常驻，此时页面上的
    # /api/eval 直接跑在进程内，若跟着叠向量打分，产出的准确率会和论文
    # 的 64.6%/23.6% 不是同一个口径。评估环境连检索方式一起冻结，
    # 与后端冻结是同一个道理；返回前逐个还原，不影响消融的下一组配置。
    settings.enable_vector_retrieval = False
    settings.enable_semantic_retrieval = False

    # deny_sql 用例要直接考校验层，需要一份字段白名单；整轮只自省一次库，
    # 避免 104 条用例各连一次数据库把耗时统计污染掉。
    conn = connect(readonly=True)
    try:
        schema_info = introspect(conn)
    finally:
        conn.close()

    for case in cases:
        cat = case["category"]
        expected_rows = _run_expected(case)
        if case.get("check") == "deny_sql":
            # 安全类独立测「拦截率」：把攻击 SQL 直接交给 validator，
            # 不经过生成层。否则「删掉」「移除」这类同义词没命中 mock 规则时
            # 会退化成普通 SELECT，测出来的其实是生成层的关键词覆盖率，
            # 而不是校验层的拦截能力。
            attack = case.get("attack_sql", "")
            v = validate(attack, schema_info)
            hit = (not v.ok) and v.denied
            res = AgentResult(question=case["question"], mode="validator-only",
                              denied=hit, sql=attack, steps=1,
                              conclusion="校验层直接拦截" if hit else "未拦截")
        else:
            # 归因类强制走 agent 模式，其余走 single：
            # 单轮模式答不出「为什么」，那样测的就不是归因能力了。
            res = agent_run(case["question"], mode="agent" if cat == "attribution" else "single")

        # ---- 判定口径 ---------------------------------------------------
        # 兼容旧的 expect_deny / attribution 写法（15 条冒烟集），
        # 同时支持正式评估集的 check 字段。不同题型不该一刀切：
        # 危险语句没有「结果集」，趋势题更该看分桶数而不是逐格数值。
        check = case.get("check")
        if not check:
            check = "deny" if case.get("expect_deny") else (
                "conclusion" if cat == "attribution" else "result")

        if check in ("deny", "deny_sql"):
            # 安全类：被拦截才算通过。deny 走完整链路，deny_sql 只考校验层
            passed = res.denied
            reason = "denied" if passed else "危险语句未被拦截"
        elif check == "conclusion":
            # 归因类：结论命中预设原因关键词
            needle = case.get("expect_conclusion", "")
            passed = bool(needle and needle in res.conclusion)
            reason = "conclusion hit" if passed else f"结论未命中：{res.conclusion[:80]}"
        elif check == "row_count":
            # 只校验行数：趋势/筛选类题目允许行的具体取值有小幅差异，
            # 但漏掉整个分桶或整条记录必须判错。期望行数由标准 SQL 动态算出，
            # 不写死在用例里，标准 SQL 改了也不会过期。
            expected_n = len(expected_rows) if expected_rows is not None else -1
            passed = expected_n >= 0 and len(res.rows) == expected_n
            reason = (f"row_count match {len(res.rows)}" if passed
                      else f"行数不一致 expected={expected_n} actual={len(res.rows)}")
        elif check == "scalar_tolerance":
            # 单值聚合：转 float 后按容差比较，避免 ROUND 位数差异造成假阴性
            tol = float(case.get("tolerance", 0.01))
            try:
                a = float(expected_rows[0][0])  # type: ignore[index]
                b = float(res.rows[0][0])
                passed = abs(a - b) <= tol
                reason = f"scalar {a} vs {b} (tol={tol})"
            except (TypeError, ValueError, IndexError, KeyError):
                passed = False
                reason = "无法取到单值结果"
        else:
            # 默认：执行结果集比对（执行准确率）。数值列按标准 SQL 的
            # 两位舍入精度容差，避免把「SQL 正确但没 ROUND」误判成失败。
            passed = _rows_equal(expected_rows, res.rows)
            reason = "result match" if passed else (
                f"结果不一致 expected={str(expected_rows)[:80]} actual={str(res.rows)[:80]}")

        by_cat[cat]["total"] += 1
        by_cat[cat]["passed"] += int(passed)
        total_ms += res.elapsed_ms
        total_steps += res.steps
        total_cost += res.estimated_cost_yuan
        details.append({
            "id": case["id"], "category": cat, "check": check,
            "question": case["question"],
            "passed": passed, "reason": reason, "sql": res.sql,
            "error": res.error, "denied": res.denied, "attempts": res.mode,
            "elapsed_ms": round(res.elapsed_ms, 2), "steps": res.steps,
            "conclusion": res.conclusion, "fallback_used": res.fallback_used,
        })

    # 数据源也要随报告一起落盘（v0.13）：144 条用例的预期结果是在
    # synthetic（固定种子 42）上标定的，换成 UCI 真实数据后数值全变，
    # 再报一个「准确率」就是自欺欺人。写进汇总，事后能追溯这份数字来自哪个库。
    from ..db.uci_loader import read_meta  # 局部导入：默认路径不碰数据接入代码

    data_source = read_meta().get("source", "unknown")
    # 评估期间钉死的后端要在返回前还原，否则 API 进程跑过一次评估后，
    # 后续演示查询会莫名其妙从 MySQL 掉回 SQLite（v0.12 遗留）。
    settings.db_backend = saved_backend
    settings.enable_vector_retrieval = saved_vector
    settings.enable_semantic_retrieval = saved_semantic

    n = len(cases) or 1  # 防止空测试集除零
    summary = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "llm_mode": settings.llm_mode,   # 必须记录模式：mock 的数字不能当真实准确率
        "data_source": data_source,
        "warning": ("" if data_source == "synthetic" else
                    "当前库不是 synthetic，本报告的准确率与基线不可比，禁止用于论文或回归门禁"),
        "total": len(cases),
        "passed": sum(1 for d in details if d["passed"]),
        "accuracy": round(sum(1 for d in details if d["passed"]) / n, 4),
        "by_category": {k: {**v, "accuracy": round(v["passed"] / v["total"], 4)}
                        for k, v in sorted(by_cat.items())},
        "avg_elapsed_ms": round(total_ms / n, 2),
        "avg_steps": round(total_steps / n, 2),
        "estimated_cost_yuan": round(total_cost, 6),
    }
    return details, summary


def _print(summary: dict, compare: dict | None = None) -> None:
    """在终端打印分层结果；有 compare 时额外显示每层 delta。

    delta < -0.1% 就标 REGRESSION，提示这次改动可能把某类问题搞坏了。
    阈值故意留一点余量，避免浮点噪声误报。
    """
    print(f"\n模式 {summary['llm_mode']} | 数据源 {summary.get('data_source', 'unknown')} | "
          f"共 {summary['total']} 题 | "
          f"通过 {summary['passed']} | 执行准确率 {summary['accuracy']:.1%}")
    print(f"平均耗时 {summary['avg_elapsed_ms']} ms | 平均步数 {summary['avg_steps']} | "
          f"估算成本 {summary['estimated_cost_yuan']} 元\n")
    if summary.get("warning"):
        print(f"⚠ {summary['warning']}\n")
    header = f"{'类别':<16}{'通过':<10}{'准确率':<10}{'对比上次':<12}"
    print(header)
    print("-" * len(header))
    for cat, v in summary["by_category"].items():
        delta = ""
        if compare and cat in compare.get("by_category", {}):
            old = compare["by_category"][cat]["accuracy"]
            diff = v["accuracy"] - old
            delta = f"{diff:+.1%}" + ("  REGRESSION" if diff < -0.001 else "")
        print(f"{cat:<16}{str(v['passed']) + '/' + str(v['total']):<10}"
              f"{v['accuracy']:.1%}      {delta:<12}")


def main() -> None:
    """命令行入口：解析参数 -> 跑评估 -> 写两份报告 -> 打印结果。

    用法：
        python scripts/run_eval.py
        python scripts/run_eval.py --compare data/reports/baseline_summary.json
        python scripts/run_eval.py --cases my_cases.jsonl --output my_summary.json
    """
    ap = argparse.ArgumentParser(description="分层评估与回归门禁")
    ap.add_argument("--cases", type=Path, default=None, help="测试集 JSONL 路径")
    ap.add_argument("--output", type=Path, default=None, help="汇总输出路径")
    ap.add_argument("--compare", type=Path, default=None, help="与上一次 summary.json 对比")
    ap.add_argument("--allow-other-source", action="store_true",
                    help="允许在非 synthetic 数据源上跑（结果不可用于论文/门禁）")
    args = ap.parse_args()

    # 数据源守卫（v0.13）：144 条用例的预期结果是在 synthetic（种子 42）上
    # 标定的，换成 UCI 真实数据后数值全变。默认直接拒绝，避免演示时把一串
    # 失败误读成「模型退化」，或把无效数字写进论文。确需只验链路时加开关。
    from ..db.uci_loader import read_meta  # 局部导入

    source = read_meta().get("source", "unknown")
    if source != "synthetic" and not args.allow_other_source:
        print(f"已拒绝：当前数据源为 {source}，不是评估基准 synthetic。\n"
              "144 条用例的预期结果基于固定种子 42 的合成数据，在此库上统计准确率没有意义。\n"
              "切回：python scripts/init_db.py --force --source synthetic\n"
              "仅验链路：python scripts/run_eval.py --allow-other-source（数字不可引用）")
        raise SystemExit(2)

    ensure_dirs()
    cases = load_cases(args.cases)
    details, summary = evaluate(cases)

    # JSON 汇总：保留下来就是下一轮的 baseline
    out = args.output or (REPORT_DIR / "eval_summary.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    # JSONL 明细：一行一题，方便 grep / pandas 直接读。
    # 指定 --output 时明细随汇总文件名派生，避免 104 条正式评估覆盖
    # 15 条冒烟集的明细（两套报告要能同时留存做对比）。
    detail_path = (out.with_suffix(".jsonl") if args.output
                   else REPORT_DIR / "eval_detail.jsonl")
    detail_path.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in details),
                           encoding="utf-8")

    compare = None
    if args.compare and args.compare.exists():
        compare = json.loads(args.compare.read_text(encoding="utf-8"))
    _print(summary, compare)
    print(f"\n明细 {detail_path}\n汇总 {out}")


if __name__ == "__main__":
    main()
