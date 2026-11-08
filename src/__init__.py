"""基于大语言模型的物联网监测数据分析 Agent 系统。

包结构与开题报告七个研究模块的对应关系：
    config               全局配置（模型供应商、步数上限、超时、行数上限）
    db                   ① 数据层：DDL、只读连接、可复现合成数据
    schema_retrieval     ② Schema 混合检索与数据字典
    sql                  ③④ SQL 生成、三重校验、安全执行与自纠
    agent                ⑤ 自研受控 Agent 运行时（本课题研究对象）
    presentation         ⑥ 表格、图表建议、结论
    evaluation           ⑦ 分层测试集、批量评估、回归门禁
    cli / app            命令行与可选 Streamlit 交互层

基线阶段仅依赖 Python 标准库 + sqlglot；openai / pydantic / pandas /
matplotlib / streamlit 均为可选增强，缺任何一个都不影响主链路运行。
"""
__version__ = "0.1.0-baseline"
