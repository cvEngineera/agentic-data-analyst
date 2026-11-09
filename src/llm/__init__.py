"""模型调用层包（v0.12）。

对外只暴露一个函数 ``complete(prompt)``，实现见 ``langchain_client``。
上层（sql/generator.py）不关心底层是 LangChain 还是标准库 urllib，
切换由 ``settings.llm_backend`` 决定。
"""
from .langchain_client import complete, is_available, reset

__all__ = ["complete", "is_available", "reset"]
