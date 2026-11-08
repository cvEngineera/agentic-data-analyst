"""Streamlit 交互界面（可选增强，对应开题报告「4.3 交互层」）。

运行：``streamlit run src/app.py``（需先 pip install streamlit）

设计取舍：
- 界面以满足功能演示为度，核心逻辑全部复用 ``agent.runtime.run``，
  这里不做任何业务判断，避免「界面里藏了一套逻辑」导致评估结果对不上。
- Streamlit 是**可选依赖**：try/except ImportError 包住导入，未安装时
  打印命令行替代方案并正常退出，不会让整个项目起不来。
- 显示生成的 SQL 与执行轨迹，对应开题报告「SQL 透明可核对」「轨迹可追溯」。

注意：Streamlit 每次交互会重跑脚本，所以本项目把状态都放在
``run()`` 的返回值里，不依赖会话级全局变量；后续要接多轮对话时，
再引入 st.session_state 管理上下文。
"""
from __future__ import annotations

import sys

try:
    import streamlit as st
except ImportError:
    # 优雅降级：基线阶段没有前端也能完成全部验收
    print("未安装 Streamlit。请执行 pip install streamlit，或先用命令行：")
    print("  python -m src.cli \"3号教室昨天的平均温度是多少\"")
    sys.exit(0)

from .agent.runtime import run  # noqa: E402  （必须在 st 导入成功后）
from .presentation.render import choose_chart, format_table  # noqa: E402

st.set_page_config(page_title="数据分析 Agent", layout="wide")
st.title("物联网监测数据分析 Agent")
st.caption("自然语言提问 -> 自动生成 SQL -> 校验 -> 只读执行 -> 表格/图表/结论")

with st.sidebar:
    # 三个模式对应 runtime.run 的 mode 参数，方便现场对比单轮与多步的差异
    mode = st.selectbox("执行模式", ["auto", "single", "agent"])
    show_sql = st.checkbox("显示生成的 SQL", value=True)
    show_trace = st.checkbox("显示执行轨迹", value=False)
    st.markdown("---")
    st.caption("当前为基线版本，模型模式由 .env 的 LLM_MODE 控制")

question = st.chat_input("例如：这周哪个教室空气质量最差，为什么？")
if question:
    with st.spinner("分析中..."):
        result = run(question, mode=mode)

    st.subheader("结论")
    if result.denied:
        # 安全拦截用 error 样式，视觉上和「查询无结果」区分开
        st.error(f"安全拦截：{result.error}")
    else:
        st.write(result.conclusion)

    # SQL 透明可核对：懂行的人可以当场判断查询口径对不对
    if show_sql and result.sql:
        st.code(result.sql, language="sql")

    if result.rows:
        # 把 (columns, rows) 转成 list[dict]，交给 Streamlit 的 dataframe 渲染
        st.dataframe([dict(zip(result.columns, r)) for r in result.rows])
        kind = choose_chart(question, result.columns, result.rows)
        if kind != "table" and len(result.rows) > 1:
            st.caption(f"建议图表类型：{kind}（可在命令行用 --chart 导出）")

    # 量化指标常驻页面：步数、耗时、成本正好对应开题报告 4.2 的目标表
    st.caption(f"模式 {result.mode} | 步数 {result.steps} | 耗时 {result.elapsed_ms:.1f} ms "
               f"| 估算成本 {result.estimated_cost_yuan} 元")
    if show_trace:
        st.json(result.trace)
