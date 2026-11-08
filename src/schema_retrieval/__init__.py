"""Schema 检索（研究模块②）：数据字典 + 混合检索。

当前实现 = 中文别名打分 + 外键一跳扩展；语义向量接口留在
catalog.hybrid_score()，是消融实验的扩展点，接入 Chroma / pgvector
时不需要改动调用方。

对外主要接口：retrieve() / build_schema_context() / TABLE_META。
"""
