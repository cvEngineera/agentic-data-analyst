"""命令行入口：第一周目标就是「输入一句话 -> 输出一行查询结果」。

刻意把界面做薄：所有逻辑都在 agent.runtime / sql / presentation 里，
CLI 只负责解析参数、调用、把结果打印出来。这样换成 Streamlit、
FastAPI 或论文里的批量脚本时，核心链路一行都不用改。

用法：
    python -m src.cli "3号教室昨天的平均温度是多少"
    python -m src.cli "这周哪个教室空气质量最差，为什么" --mode agent --show-trace
    python -m src.cli "删除所有设备记录"
    python -m src.cli "各教室 CO2 均值排名" --llm real --json
"""
from __future__ import annotations

import argparse
import json

from .agent.runtime import run
from .config import settings
from .presentation.render import choose_chart, format_table, save_chart


def main() -> None:
    """解析命令行参数，执行一次查询并输出。

    输出内容刻意包含**生成的 SQL**：让用户能核对系统到底查了什么，
    这是开题报告里「SQL 透明可核对」那条功能要求，也是防范
    「语法对但口径错」这类静默错误的人工兜底。
    """
    ap = argparse.ArgumentParser(description="数据分析 Agent 命令行入口")
    ap.add_argument("question", help="自然语言问题")
    ap.add_argument("--mode", choices=("auto", "single", "agent"), default="auto",
                    help="auto 按意图选，single 单轮兜底，agent 多步归因")
    ap.add_argument("--llm", choices=("mock", "real"), default=None,
                    help="覆盖 .env 里的 LLM_MODE")
    ap.add_argument("--show-trace", action="store_true", help="打印执行轨迹")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本调用")
    ap.add_argument("--chart", action="store_true", help="保存图表到 data/reports/")
    args = ap.parse_args()

    # CLI 覆盖优先级最高：直接改全局 settings 实例，其余模块立即生效
    if args.llm:
        settings.llm_mode = args.llm
    # 真实模型不可用时明确告知并回落，绝不悄悄用 mock 冒充真实结果
    if settings.llm_mode == "real" and not settings.real_mode_available:
        if not args.json:  # JSON 模式只输出纯 JSON，不能混入提示文本
            print("[提示] 真实模型不可用（缺少 LLM_API_KEY），"
                  "本次自动回落到 mock 模式。")
        settings.llm_mode = "mock"

    result = run(args.question, mode=args.mode)

    if args.json:
        # 结构化输出给脚本/前端消费；ensure_ascii=False 保证中文不被转义
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return

    print(f"问题      {result.question}")
    print(f"模式      {result.mode}  |  LLM {settings.llm_mode}  |  步数 {result.steps}")
    print(f"生成的 SQL\n  {result.sql or '(无)'}")
    # 三种收尾状态分开呈现，避免把「被拦截」误显示成「查不到数据」
    if result.denied:
        print(f"\n[安全拦截] {result.error}")
    elif result.error:
        print(f"\n[执行失败] {result.error}")
    else:
        print(f"\n结果\n{format_table(result.columns, result.rows)}")
    print(f"\n结论      {result.conclusion}")
    # 意图闸门直接回复时没有任何结果集，不该再给「建议图表 table」——
    # 那会让人误以为系统其实查了库。把统计与配图建议分开输出。
    stats = f"耗时 {result.elapsed_ms:.1f} ms | 估算成本 {result.estimated_cost_yuan} 元"
    if result.mode != "gate":
        stats += f" | 建议图表 {choose_chart(result.question, result.columns, result.rows)}"
    print(stats)
    if args.chart:
        # save_chart 未装 matplotlib 时返回空串，这里静默跳过即可
        path = save_chart(result.columns, result.rows,
                          choose_chart(result.question, result.columns, result.rows))
        if path:
            print(f"图表已保存 {path}")
    if args.show_trace:
        # 执行轨迹是答辩演示与消融分析最直观的证据
        print("\n执行轨迹")
        for t in result.trace:
            print(f"  [{t['step']}] {t['action']:<14} {t['result_summary']}")


if __name__ == "__main__":
    main()
