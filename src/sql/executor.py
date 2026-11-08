"""安全执行与报错自纠（对应开题报告「4.1（3）（4）」的执行部分）。

完整链路：生成 -> 三重校验 -> 只读执行 -> 失败回喂重试（最多两次）-> 兜底。

两个容易被忽略的设计点：
1. **危险语句不重试**。validator 返回 denied=True 时立即停止。把「改写这条
   DELETE」回喂给模型，等于替攻击者重新包装意图，这是安全设计而非容错设计。
2. **失败信息原样回喂**。数据库返回的「no such column: xxx」是最直接的
   反馈信号，比自己概括错误信息有效得多；重试次数上限由 max_retry 控制，
   避免过度重试拖高延迟与成本（开题报告「关键问题三」）。

所有执行都走 db.core.connect(readonly=True)，即文件级只读 + query_only，
即使校验层被绕过，数据库层面也写不进去。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import settings
from ..db.core import connect, introspect
from .generator import generate_sql
from .validator import validate


@dataclass
class RunResult:
    """单轮查询的完整结果。

    除了结果集，还把 attempts / checks / feedbacks / elapsed_ms 都带出来，
    因为这些都是评估体系的输入：重试次数反映自纠效果，checks 反映校验层
    是否生效，耗时与成本用于第 4.2 节的量化目标表。
    """

    question: str
    sql: str = ""
    source: str = ""
    category: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    error: str = ""                       # 最后一次失败原因；成功时为空
    denied: bool = False                  # 是否被安全层拦截
    attempts: int = 0                     # 实际尝试次数（含首次）
    checks: list[str] = field(default_factory=list)     # 校验层逐关记录
    tables: list[str] = field(default_factory=list)     # 生成 SQL 引用的表
    feedbacks: list[str] = field(default_factory=list)  # 每次回喂的错误信息
    elapsed_ms: float = 0.0               # 累计执行耗时（不含生成耗时）


def execute(sql: str, params: tuple = ()) -> tuple[list[str], list[tuple], str, float]:
    """只读执行一条 SQL。

    Args:
        sql: 已通过校验的规范化 SQL。
        params: 参数化查询的实参。**必须**用参数绑定，不要拼字符串——
                归因链路里要把教室名传进来，拼接会引入注入风险。

    Returns:
        ``(列名列表, 行元组列表, 错误文本, 耗时毫秒)``。
        出错时前两项为空、第三项为错误信息；**不抛异常**，
        因为错误信息要被回喂给生成层，抛出去就断了重试链路。

    超时实现：SQLite 没有直接的语句超时参数，用 set_progress_handler
    每 1000 条 VM 指令回调一次，超过 settings.query_timeout 返回非零中断执行。
    这样一条失控的全表扫描不会把整个演示卡死。
    """
    conn = connect(readonly=True)
    t0 = time.perf_counter()
    try:
        deadline = settings.query_timeout

        def _watchdog() -> int:
            """返回非 0 会让 SQLite 中断当前语句。"""
            return 1 if time.perf_counter() - t0 > deadline else 0

        conn.set_progress_handler(_watchdog, 1000)
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        # fetchmany 而不是 fetchall：双重保险，防止 LIMIT 被绕过时撑爆内存
        rows = [tuple(r) for r in cur.fetchmany(settings.row_limit)]
        return cols, rows, "", (time.perf_counter() - t0) * 1000
    except Exception as exc:
        return [], [], str(exc), (time.perf_counter() - t0) * 1000
    finally:
        conn.close()


def run_with_retry(question: str, schema_context: str,
                   schema_info: dict[str, list[str]] | None = None,
                   max_retry: int = 2) -> RunResult:
    """单轮查询主流程：生成、校验、执行、失败自纠。

    Args:
        question: 用户问题。
        schema_context: Schema 检索得到的 DDL 片段，注入提示词。
        schema_info: 表/列白名单；为 None 时自动自省数据库（多一次连接）。
        max_retry: **额外**重试次数。总尝试次数 = max_retry + 1，
                   默认 2 对应开题报告「最多重试两次」。

    循环里两类失败要区分：
        - 校验失败且 denied=True → 危险语句，break，绝不重试
        - 校验失败（字段不存在等）或执行报错 → 把原因放进 feedback，继续下一轮

    循环自然结束仍未成功时，保留最后一次生成的 SQL 并返回带 error 的
    RunResult，由上层 runtime 决定是否回落单轮模式，保证系统永远有输出。
    """
    # 消融开关：关掉自纠错时把重试次数归零，只保留首次生成。
    # 这样「报错回喂重试」这一层可以单独开关，量化它带来的百分点。
    if not settings.ablation_self_correction:
        max_retry = 0

    if schema_info is None:
        conn = connect(readonly=True)
        try:
            schema_info = introspect(conn)
        finally:
            conn.close()

    result = RunResult(question=question)
    feedback: str | None = None   # 传给 generate_sql 的上一轮错误信息
    last_sql = ""

    for attempt in range(1, max_retry + 2):
        result.attempts = attempt
        # 第 2 次起提示词里会附带错误信息，要求模型输出修正后的完整 SQL
        gen = generate_sql(question, schema_context, feedback)
        last_sql = gen.sql
        v = validate(gen.sql, schema_info)
        # 无论成败都记录，评估脚本要统计校验层命中情况
        result.checks = v.checks
        result.tables = v.tables
        result.sql = gen.sql
        result.source = gen.source
        result.category = gen.category

        if not v.ok:
            result.error = v.reason
            if v.denied:
                result.denied = True
                break  # 危险语句不重试（安全设计）
            feedback = v.reason
            result.feedbacks.append(feedback)
            continue

        cols, rows, err, elapsed = execute(v.normalized)
        result.elapsed_ms += elapsed
        if err:
            result.error = err
            feedback = err
            result.feedbacks.append(err)
            continue
        # 成功：用规范化后的 SQL 覆盖原始 SQL，方便用户核对实际执行了什么
        result.sql = v.normalized
        result.columns, result.rows = cols, rows
        result.error = ""
        return result

    result.sql = last_sql
    return result
