"""SQL 三重校验与安全白名单（对应开题报告「4.1（3）（4）」）。

校验按顺序分四关，任何一关失败都不进入执行：
    1. 安全层：单语句、只允许 SELECT、危险关键字黑名单
    2. 语法层：sqlglot 解析成 AST，解析不了直接拒绝
    3. 结构层：AST 里出现的表名/字段名必须在白名单内
    4. 语义层：聚合与分组一致性粗检；最后强制补 LIMIT

为什么不用正则一把梭：正则无法区分「字符串里出现 DELETE」和「真的 DELETE 语句」，
也看不出 ``SELECT foo FROM unknown_table`` 里的表是否存在。AST 级校验才靠得住。
sqlglot 是可选依赖，没装时降级为只做安全层与行数限制，并在 checks 里留痕，
保证基线在任何环境都能跑。

**关键安全语义**：``denied=True`` 表示这是危险语句，调用方必须**立即停止、不重试**。
不能把「请改写这条 DELETE」回喂给模型——那等于把攻击意图重新包装一次。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import settings
from ..db.core import ALLOWED_TABLES

# 危险关键字：写操作、DDL、附加数据库、执行外部命令等。
# 用词边界正则匹配，避免把 ``update_time`` 这类列名误判成 UPDATE。
FORBIDDEN = (
    "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE", "REPLACE",
    "TRUNCATE", "PRAGMA", "ATTACH", "DETACH", "VACUUM", "EXEC", "GRANT",
)


@dataclass
class ValidationResult:
    """一次校验的结果。

    ok / normalized:  是否通过、以及规范化后真正可执行的 SQL（已补 LIMIT）
    reason:           失败原因，会被当作 error_feedback 回喂给生成层
    checks:           每一关的通过/跳过记录，写进执行轨迹，供消融与审计
    tables:           AST 里引用的表，供 Schema 检索效果分析
    denied:           是否为危险语句。True 时调用方不得重试（见模块说明）
    """

    ok: bool
    normalized: str = ""
    reason: str = ""
    checks: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    denied: bool = False


def _strip_sql(sql: str) -> str:
    """去掉行注释、块注释与末尾分号，避免注释干扰关键字检查。

    例：``SELECT 1 -- DROP TABLE x`` 不该被判成危险语句。
    注意这里只做文本清理，**不构成安全保证**，真正的判定靠后面的 AST 与白名单。
    """
    s = re.sub(r"--[^\n]*", " ", sql)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    return s.strip().rstrip(";").strip()


def _read_dialect() -> str:
    """当前校验应使用的 SQL 方言（v0.12）。

    为什么需要它：v0.12 起支持 MySQL 作为演示后端，而 sqlglot 的
    ``parse_one(read=...)`` 必须给对方言，否则 ``DATE_SUB(CURDATE(),
    INTERVAL 1 DAY)`` 这类 MySQL 写法会被 SQLite 方言解析失败，
    正确的 SQL 被判成语法错误。

    取值跟随 ``settings.db_backend``：
        sqlite → "sqlite"（默认，评估口径）
        mysql  → "mysql"
    """
    return "mysql" if settings.db_backend == "mysql" else "sqlite"


def validate(sql: str, schema_info: dict[str, list[str]]) -> ValidationResult:
    """执行四关校验。

    Args:
        sql: 生成层输出的原始 SQL（可能带注释、分号、Markdown 残留）。
        schema_info: ``{表名: [列名]}``，由 db.core.introspect 从真实库读出。
                     从库里读而不是写死，换数据集后白名单自动跟着变。

    Returns:
        ValidationResult。调用方只应执行 ``normalized`` 字段，不要执行原始 sql。
    """
    checks: list[str] = []
    s = _strip_sql(sql)
    if not s:
        return ValidationResult(False, reason="空语句", checks=["empty"])

    # ---- 第 1 关：安全层 -------------------------------------------------
    body = s.rstrip(";")
    if ";" in body:
        # 多语句是典型的注入手法：SELECT 1; DROP TABLE ...
        return ValidationResult(False, reason="检测到多条语句，禁止执行",
                               checks=["single_statement:fail"], denied=True)
    if not re.match(r"^\s*(SELECT|WITH)\b", body, re.I):
        # 只允许 SELECT / WITH（CTE）。CTE 也要放行，因为嵌套子查询会用到。
        return ValidationResult(False, reason="只允许 SELECT 查询，写操作已拦截",
                               checks=["readonly:fail"], denied=True)
    upper = body.upper()
    for word in FORBIDDEN:
        if re.search(r"\b" + word + r"\b", upper):
            return ValidationResult(False, reason=f"危险关键字 {word} 已拦截",
                                   checks=[f"forbidden:{word}"], denied=True)
    checks.append("readonly:pass")

    # ---- 第 2/3 关：语法层 + 结构层（sqlglot AST）------------------------
    tables: list[str] = []
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(body, read=_read_dialect())  # 指定方言，保证日期函数解析正确
        if tree is None:
            raise ValueError("parse returned None")
        tables = [t.name for t in tree.find_all(exp.Table)]
        # SELECT 里的别名（AS avg_co2）也会被 find_all(Column) 抓到，
        # 但它们不是「引用的列」，必须先收进允许集合，否则 ORDER BY 别名会误判。
        aliases = {a.alias for a in tree.find_all(exp.Alias) if a.alias}
        allowed_cols = {c for cols in schema_info.values() for c in cols} | aliases
        bad_tables = [t for t in tables if t not in ALLOWED_TABLES]
        if bad_tables:
            return ValidationResult(False, reason=f"引用了不存在的表：{bad_tables}",
                                   checks=["syntax:pass", "table_whitelist:fail"], tables=tables)
        bad_cols = sorted({c.name for c in tree.find_all(exp.Column)
                           if c.name not in allowed_cols and c.name != "*"})
        if bad_cols:
            # 字段不存在是最常见的模型幻觉，错误信息会原样回喂给模型重试
            return ValidationResult(False, reason=f"引用了不存在的字段：{bad_cols}",
                                   checks=["syntax:pass", "table_whitelist:pass",
                                           "column_whitelist:fail"], tables=tables)
        checks += ["syntax:pass", "table_whitelist:pass", "column_whitelist:pass"]

        # ---- 第 4 关前半：语义层粗检 ------------------------------------
        # 「SELECT a, AVG(b) FROM t」在 SQLite 里能执行，但结果往往不是用户想要的。
        # 这里只标记 warning 不拦截：拦截过严会误伤正常查询，属于安全与可用性的平衡。
        has_agg = any(isinstance(n, (exp.Avg, exp.Sum, exp.Max, exp.Min, exp.Count))
                      for n in tree.walk())
        plain_cols = [c.name for c in tree.find_all(exp.Column)
                      if c.name not in aliases and c.name != "*"]
        has_group = tree.args.get("group") is not None
        if has_agg and not has_group and len(plain_cols) > 1:
            checks.append("semantic:warn-mixed-aggregate")
        else:
            checks.append("semantic:pass")
    except ImportError:
        # 没装 sqlglot：只保留安全层与行数限制，并在轨迹里明确标注「跳过」
        checks += ["syntax:skip(no sqlglot)", "semantic:skip"]
    except Exception as exc:
        # 解析异常当作语法错误，同样回喂生成层重试
        return ValidationResult(False, reason=f"SQL 语法解析失败：{exc}",
                               checks=["syntax:fail"], tables=tables)

    # ---- 第 4 关后半：执行层保护 ----------------------------------------
    # 没有 LIMIT 就自动补，防止模型生成全表扫描把演示拖死。
    # 已有 LIMIT 时尊重原值（评估集里有些题目明确 LIMIT 5/50）。
    if not re.search(r"\bLIMIT\s+\d+", upper):
        body = f"{body} LIMIT {settings.row_limit}"
    checks.append("row_limit:pass")
    return ValidationResult(True, normalized=body, checks=checks, tables=tables)
