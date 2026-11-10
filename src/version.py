"""项目版本号（单点定义）。

为什么要单独一个文件：版本号会出现在 FastAPI 的 OpenAPI 文档、
``/api/status`` 返回、终端启动横幅三处。写死三遍必然有一天忘了改其中一处，
然后页面显示 v0.11、文档显示 v0.12，答辩时很难解释。
"""
from __future__ import annotations

VERSION = "v0.13.1"
