"""SQL 链路（研究模块③④）：生成、三重校验、安全执行与报错自纠。

- generator.py：提示构造 + mock/real 双模式生成，支持错误信息回喂。
- validator.py：安全层 / 语法层 / 结构层 / 语义层四关，输出 denied 标志。
- executor.py：只读执行 + 超时中断 + 最多两次自纠，危险语句绝不重试。

调用顺序固定为 generator -> validator -> executor，不要绕过校验直接执行。
"""
