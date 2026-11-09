"""统一查询执行层（v0.12）：SQLite 与 MySQL 走同一个函数签名。

为什么要有这一层
----------------
执行链路（``sql/executor.py``）只关心「给我列名、行、错误、耗时」，
不该关心底层是 sqlite3 还是 SQLAlchemy。把这份差异收在一个文件里，
换库时只改这里，校验层、Agent 运行时、评估脚本一行不动。

两个后端的安全语义必须对齐
--------------------------
+----------------+---------------------------+-------------------------------+
| 后端           | 只读手段                  | 超时手段                      |
+================+===========================+===============================+
| SQLite         | mode=ro + query_only      | set_progress_handler 看门狗   |
| MySQL          | SESSION TRANSACTION READ  | SET SESSION max_execution_time|
|                | ONLY                      |                               |
+----------------+---------------------------+-------------------------------+

两者都是「数据库层面的兜底」，而不是「检测 SQL 字符串里有没有 DELETE」。
即使上层校验被绕过，数据库也写不进去——这是论文里安全设计那一节的论点。

参数绑定的差异
--------------
SQLite 用 ``?`` 占位符，SQLAlchemy 用 ``:name``。这里做一次占位符改写，
**跳过字符串字面量内部**的 ``?``（例如 ``WHERE name = '?'``），
避免把字面量里的问号误当占位符。改写后参数以字典传入，
底层仍然是真正的绑定，不是字符串拼接。
"""
from __future__ import annotations

import time
from decimal import Decimal

from ..config import settings

# 一次查询最多回传的行数由 settings.row_limit 控制；
# 这里不重复限制，交给 executor 的 fetchmany 处理。


def _convert_params(sql: str, params: tuple) -> tuple[str, dict]:
    """把 ``?`` 占位符改写成 SQLAlchemy 的 ``:p0, :p1 ...``。

    Args:
        sql: 带 ``?`` 占位符的 SQL。
        params: 位置参数元组。

    Returns:
        ``(改写后的 SQL, 命名参数字典)``。无参数时原样返回。

    实现要点：逐字符扫描并跟踪单/双引号状态，只有**不在引号内**的 ``?``
    才算占位符。不能用 ``sql.replace("?", ...)``，那会把
    ``WHERE note = '?'`` 里的字面量也改掉。
    """
    if not params:
        return sql, {}

    out: list[str] = []
    named: dict[str, object] = {}
    in_single = in_double = False
    idx = 0
    for ch in sql:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == "?" and not in_single and not in_double:
            key = f"p{idx}"
            out.append(f":{key}")
            named[key] = params[idx]
            idx += 1
        else:
            out.append(ch)
    return "".join(out), named


def _normalize_value(value):
    """把驱动返回的 Python 对象转成「能直接 JSON 序列化」的形态。

    为什么需要：MySQL 驱动会把 DATETIME 返回成 ``datetime``、把
    DECIMAL 返回成 ``Decimal``，而 SQLite 返回的是字符串和浮点。
    同一道题在两个后端输出不同类型，会让前端渲染和评估比对出现
    「明明数值一样却判不等」的假阴性。这里统一成字符串/浮点。
    """
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "strftime"):  # datetime / date
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def _run_sqlite(sql: str, params: tuple, readonly: bool,
                timeout: float) -> tuple[list[str], list[tuple], str, float]:
    """SQLite 分支：沿用 v0.2 起的只读连接与看门狗实现。

    评估结果全部来自这条路径，**不要改动其行为**，否则历史数字不可比。
    """
    from .core import connect

    conn = connect(readonly=readonly)
    t0 = time.perf_counter()
    try:
        def _watchdog() -> int:
            """返回非 0 会让 SQLite 中断当前语句。"""
            return 1 if time.perf_counter() - t0 > timeout else 0

        conn.set_progress_handler(_watchdog, 1000)
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [tuple(r) for r in cur.fetchmany(settings.row_limit)]
        return cols, rows, "", (time.perf_counter() - t0) * 1000
    except Exception as exc:
        return [], [], str(exc), (time.perf_counter() - t0) * 1000
    finally:
        conn.close()


def _run_mysql(sql: str, params: tuple, timeout: float) -> tuple[list[str], list[tuple], str, float]:
    """MySQL 分支：SQLAlchemy + pymysql，只读会话 + 语句超时。"""
    from sqlalchemy import text

    from .orm import read_connection

    t0 = time.perf_counter()
    try:
        with read_connection(timeout=timeout) as conn:
            bound_sql, named = _convert_params(sql, params)
            result = conn.execute(text(bound_sql), named)
            cols = list(result.keys())
            rows = [tuple(_normalize_value(v) for v in r)
                    for r in result.fetchmany(settings.row_limit)]
            # 只读会话不需要提交；显式 rollback 让连接干净地回到池里
            conn.rollback()
        return cols, rows, "", (time.perf_counter() - t0) * 1000
    except Exception as exc:
        return [], [], str(exc), (time.perf_counter() - t0) * 1000


def run_sql(sql: str, params: tuple = (), readonly: bool = True,
            timeout: float | None = None,
            backend: str | None = None) -> tuple[list[str], list[tuple], str, float]:
    """执行一条 SQL，返回 ``(列名, 行, 错误文本, 耗时毫秒)``。

    Args:
        sql: 待执行 SQL（调用方保证已过校验层）。
        params: 位置参数，禁止拼接字符串。
        readonly: 是否只读。业务链路恒为 True。
        timeout: 单条语句超时秒数，默认取 settings.query_timeout。
        backend: 强制指定后端（测试用）；None 时跟随 settings.db_backend。

    Returns:
        出错时前两项为空、第三项为错误信息，**不抛异常**——
        错误文本要被回喂给模型做自纠，抛出去就断了重试链路。
    """
    be = backend or settings.db_backend
    t = timeout if timeout is not None else settings.query_timeout
    if be == "mysql":
        return _run_mysql(sql, params, t)
    return _run_sqlite(sql, params, readonly, t)
