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


def _run_sqlite_sa(sql: str, params: tuple,
                   timeout: float) -> tuple[list[str], list[tuple], str, float]:
    """SQLite 分支（v0.12.1 起）：走 SQLAlchemy Engine + text()。

    为什么要从裸 sqlite3 换到 SQLAlchemy：申报表里写的数据访问层是
    SQLAlchemy，如果评估与演示真正执行 SQL 的地方仍是 ``sqlite3``，
    那这一项技术就只是「摆着看的适配代码」，答辩时经不起追问。
    换成 Engine 之后，两条后端（SQLite / MySQL）走的是**同一套连接管理**，
    区别只在 URL 与少量会话参数。

    与裸 sqlite3 的语义对齐（不能变的地方）：
        - 只读：URL 用 ``mode=ro&uri=true``，文件级只读，比 PRAGMA 更早生效；
        - 超时：从 ``dbapi_connection`` 拿回原生 sqlite3 连接，挂
          ``set_progress_handler`` 看门狗，失控 SQL 同样会被中断；
        - 参数：仍是真正的绑定（``:p0`` 命名参数），不是字符串拼接；
        - 返回值：text() 不带类型信息，取回的就是驱动原始值（文本/数值/NULL），
          与 sqlite3 的 ``description`` + ``fetchmany`` 一致，
          所以历史评估数字不会因为换驱动而漂移。

    连接池用 NullPool（见 ``_readonly_sqlite_engine``）：建库脚本会整文件替换
    ``bishe.db``，池里留着旧文件句柄会读到已删除的旧库，这一点在 Windows 上
    尤其明显。
    """
    from sqlalchemy import text

    eng = _readonly_sqlite_engine()   # 取引擎失败时抛出，交给上层降级到 sqlite3
    t0 = time.perf_counter()
    try:
        with eng.connect() as conn:
            # 拿回原生 sqlite3 连接挂看门狗；SQLAlchemy 本身没有语句超时概念
            raw = conn.connection.dbapi_connection
            raw.set_progress_handler(
                lambda: 1 if time.perf_counter() - t0 > timeout else 0, 1000)
            bound_sql, named = _convert_params(sql, params)
            result = conn.execute(text(bound_sql), named)
            cols = list(result.keys()) if result.returns_rows else []
            rows = ([tuple(r) for r in result.fetchmany(settings.row_limit)]
                    if cols else [])
        return cols, rows, "", (time.perf_counter() - t0) * 1000
    except Exception as exc:
        return [], [], str(exc), (time.perf_counter() - t0) * 1000


def _run_sqlite_raw(sql: str, params: tuple, readonly: bool,
                    timeout: float) -> tuple[list[str], list[tuple], str, float]:
    """SQLite 分支（裸 sqlite3）：只在 SQLAlchemy 不可用时使用。

    v0.2~v0.11 一直是这条路径，保留它是为了「装不上 SQLAlchemy 也能跑评估」——
    零依赖是这个工程的兜底承诺，不能因为补技术栈把兜底砍掉。
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


def _run_sqlite(sql: str, params: tuple, readonly: bool,
                timeout: float) -> tuple[list[str], list[tuple], str, float]:
    """SQLite 分支调度：优先 SQLAlchemy，不可用时退回 sqlite3。

    降级是**显式**的：实际用了哪个驱动记在 ``_DRIVER`` 里，由
    ``sqlite_driver()`` 暴露给状态接口，页面上能直接看到，
    不会出现「文档写 SQLAlchemy、实际跑 sqlite3」的两套说法。
    """
    if readonly:
        try:
            out = _run_sqlite_sa(sql, params, timeout)
        except Exception:
            # 引擎建不起来（没装 SQLAlchemy / URI 不被支持）→ 退回裸驱动
            _DRIVER["name"] = "sqlite3"
            return _run_sqlite_raw(sql, params, readonly, timeout)
        _DRIVER["name"] = "sqlalchemy"
        return out
    return _run_sqlite_raw(sql, params, readonly, timeout)


# 实际生效的驱动名，供 /api/status 如实上报（"未探测" 表示本次进程还没查过库）
_DRIVER: dict[str, str] = {"name": "未探测"}
_SA_ENGINE = None


def _readonly_sqlite_engine():
    """构造（并缓存）只读 SQLite 引擎。

    URL 里的 ``mode=ro`` 由 sqlite3 驱动的 URI 参数生效，比
    ``PRAGMA query_only`` 更早——连接建立时文件就是只读打开的，
    连 PRAGMA 都改不了，这是「数据库层面兜底」最硬的一层。

    NullPool：每次 connect 新开文件句柄，用完即关。理由见 _run_sqlite_sa
    的注释（建库脚本会整文件替换库文件）。
    """
    global _SA_ENGINE
    if _SA_ENGINE is not None:
        return _SA_ENGINE
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from ..config import DB_PATH

    url = f"sqlite:///file:{DB_PATH.as_posix()}?mode=ro&uri=true"
    _SA_ENGINE = create_engine(
        url, future=True, poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    return _SA_ENGINE


def sqlite_driver() -> str:
    """返回 SQLite 分支实际使用的驱动名：``sqlalchemy`` / ``sqlite3``。

    状态接口在**没有执行过任何查询**时调用它，所以这里做一次轻量探测：
    只建引擎、不执行 SQL（建引擎不会打开文件，第一次 connect 才打开）。
    """
    if _DRIVER["name"] != "未探测":
        return _DRIVER["name"]
    try:
        _readonly_sqlite_engine()
        return "sqlalchemy"
    except Exception:
        return "sqlite3"


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
        _DRIVER["name"] = "sqlalchemy"
        return _run_mysql(sql, params, t)
    return _run_sqlite(sql, params, readonly, t)
