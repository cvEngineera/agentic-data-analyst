"""结果呈现（研究模块⑥）：表格、自动配图建议、一句话结论。

render.py 只做展示，不含业务判断；choose_chart() 返回图表类型建议，
由命令行或 Streamlit 决定是否真正出图。matplotlib 为可选依赖。

对外接口：format_table() / choose_chart() / conclusion_text() / save_chart()。
"""
