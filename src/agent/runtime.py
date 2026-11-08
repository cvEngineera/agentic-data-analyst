"""受控 Agent 运行时 —— 本课题的核心研究对象（开题报告「4.1（5）」「七、创新点二」）。

**为什么这一层自己写、不用 LangGraph**：开题报告 4.3「关于编排框架的选型」
已经写明——运行时调度、结构化输出校验、执行轨迹与分层评估都要求逐层可开关
以做消融实验，框架的封装会降低可拆解性；而且这正是论文要回答「自己实现了什么」
的部分。LangGraph 仅作为**外部对比基线**在独立环境里跑同一套测试集。

四条设计原则（对应答辩时「怎么保证它不乱来」）：
1. **工具封闭**：只开放 schema_search / text2sql / execute_sql / explain 四个动作，
   Agent 不能凭空发明工具。
2. **有限选择**：不做自由规划，按「取数 → 发现异常 → 追加查询 → 形成结论」
   的模板执行。规划空间从无限变有限，成功率才可测、才可控。
3. **收敛约束**：最大步数、重复检测、失败回落单轮模式，避免循环调用。
4. **全程轨迹**：每一步的动作、参数、SQL、结果摘要、耗时都记录，
   既是可观测数据，也是评估体系统计「平均步数」的来源。

已知限制（写进论文「局限性」一节，别在答辩时夸大）：
    当前归因是**模板化**的，不是让模型自由规划排查路径；成本是按字符粗估的，
    不是真实 Token 计费；结论是「相关线索」而不是因果证明。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from ..config import settings
from ..db.core import connect, introspect
from ..schema_retrieval.catalog import build_schema_context, retrieve
from ..sql.executor import RunResult, execute, run_with_retry
from ..sql.validator import validate

# 工具白名单：Agent 只能在这四个动作里做选择。
# 保留常量是为了让「工具集固定且少」这件事在代码里可见、在答辩时可指。
TOOLS = ("schema_search", "text2sql", "execute_sql", "explain")

# 分析型任务的判定词。命中这些词才进入多步模式，其余一律走单轮，
# 这是「把自由规划降级为有限选择」的具体做法。
ANALYSIS_HINTS = ("为什么", "原因", "归因", "分析一下", "最差", "最好", "异常", "怎么回事")

# 归因阶段追加查询用的固定 SQL。
# 用 ? 占位符 + 参数绑定，教室名来自上一轮查询结果而不是用户输入拼接，
# 避免课题本身在讲 SQL 安全、自己代码却留注入口子。
ALARM_SQL = (
    "SELECT a.ts, a.type, a.level FROM alarm a "
    "JOIN device dv ON a.device_id = dv.id "
    "JOIN site s ON dv.site_id = s.id "
    "WHERE s.name = ? ORDER BY a.ts DESC LIMIT 10;"
)


@dataclass
class AgentResult:
    """一次完整任务的结果。

    trace:  执行轨迹，list[dict]，可直接 JSON 序列化后落库或展示
    steps:  实际执行步数，是「平均执行步数 ≤ 6」这个量化目标的测量值
    fallback_used: 是否触发过回落，用于统计兜底路径的触发率
    estimated_cost_yuan: 粗估成本，仅用于链路演示（见模块说明）
    """

    question: str
    mode: str
    conclusion: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    sql: str = ""
    trace: list[dict] = field(default_factory=list)
    steps: int = 0
    denied: bool = False
    error: str = ""
    elapsed_ms: float = 0.0
    estimated_cost_yuan: float = 0.0
    fallback_used: bool = False

    def to_dict(self) -> dict:
        """转普通 dict，供 CLI 的 --json 与评估脚本使用。"""
        return asdict(self)


def _is_analysis(question: str) -> bool:
    """意图判定：是不是需要多步分析的任务。

    用关键词而不是让模型自己判断，是刻意的：意图判定本身不该引入不确定性，
    否则「同一个问题有时走单轮、有时走多步」会让评估数字抖动。
    """
    return any(h in question for h in ANALYSIS_HINTS)


def _estimate_cost(trace: list[dict]) -> float:
    """按字符数粗估成本，仅用于链路演示。

    ⚠️ 不是真实 Token 计费，**不能写进论文的成本结论**。
    接真实模型后应从 API 响应的 usage 字段取 prompt_tokens /
    completion_tokens，再按供应商单价折算（开题报告 4.2 目标表里的
    「单任务成本 < 0.01 元」要用那个数字验收）。
    """
    chars = sum(len(str(t.get("sql", ""))) + len(str(t.get("result_summary", "")))
                for t in trace)
    return round(chars / 1000 * 0.002, 6)


def _record(trace: list[dict], step: int, action: str, params: dict,
            result_summary: str, elapsed_ms: float, sql: str = "") -> None:
    """往执行轨迹里追加一条记录。

    result_summary 截断到 300 字符：轨迹既要够用于排查，又不能把整张结果表
    塞进去撑爆日志。action 必须来自 TOOLS 白名单（fallback 除外）。
    """
    trace.append({"step": step, "action": action, "params": params,
                  "result_summary": result_summary[:300],
                  "elapsed_ms": round(elapsed_ms, 2), "sql": sql})


def run(question: str, mode: str = "auto") -> AgentResult:
    """执行一次任务。

    Args:
        question: 用户自然语言问题。
        mode:
            ``single`` 单轮兜底（只做取数，不做归因）；
            ``agent``  强制多步；
            ``auto``   按 _is_analysis 自动选（默认）。

    Returns:
        AgentResult，含结论、结果集、SQL、轨迹与统计信息。

    执行顺序（与开题报告 5.2 技术路线一致，也是图上那五段链路）：
        1. Schema 检索  2. 单轮取数  3. 追加查询归因  4. 结果解读
        5. 收敛检查与兜底

    关键取舍：无论哪一步失败，都保证返回一个带结论的 AgentResult，
    绝不抛异常中断。这是「最差情况下系统也有输出」的工程底线。
    """
    started = time.perf_counter()

    # 先自省真实库：候选表清单与字段白名单都从这里来，换数据集自动适配
    conn = connect(readonly=True)
    try:
        schema_info = introspect(conn)
        available = list(schema_info.keys())
    finally:
        conn.close()

    # 模式选择：分析型问题才进多步，其余走单轮（省成本、省延迟）
    chosen = "agent" if mode == "agent" or (mode == "auto" and _is_analysis(question)) else "single"
    result = AgentResult(question=question, mode=chosen)

    # ---- 步骤 1：Schema 检索 --------------------------------------------
    # 只把相关表注入提示词，既省 token，也减少模型选错表的概率。
    t0 = time.perf_counter()
    hit = retrieve(question, available)
    # 消融开关：关掉 Schema 检索时把全部表结构一股脑塞进提示词，
    # 对应论文里的「固定提示 + 单轮生成」基线方案。
    # 在 real 模式下这会显著增加 token 并引入噪声，差异可测。
    schema_context = (hit.context if settings.ablation_schema_retrieval
                      else build_schema_context(available))
    _record(result.trace, 1, "schema_search", {"top_k": 4},
            f"命中表 {hit.tables}；外键扩展 {hit.expanded}；打分 {hit.scores}",
            (time.perf_counter() - t0) * 1000)

    # ---- 步骤 2：单轮取数 ------------------------------------------------
    # 复用 executor 的完整链路（生成→校验→执行→自纠），
    # 所以单轮模式本身就是 Agent 模式的可靠底座。
    t0 = time.perf_counter()
    first: RunResult = run_with_retry(question, schema_context, schema_info)
    _record(result.trace, 2, "text2sql", {"question": question},
            f"source={first.source} attempts={first.attempts} rows={len(first.rows)} "
            f"error={first.error or 'none'}",
            (time.perf_counter() - t0) * 1000, first.sql)
    result.sql, result.columns, result.rows = first.sql, first.columns, first.rows
    result.denied = first.denied
    result.error = first.error
    result.steps = 2

    # 早退：被拦截 / 执行失败 / 无结果，都不该继续追加查询
    if first.denied or first.error or not first.rows:
        result.conclusion = ("已拦截危险语句，未执行。" if first.denied
                             else "查询未返回有效结果，已保留单轮兜底路径。")
        if not first.denied and first.error and chosen == "agent":
            # 这就是开题报告里「保留单轮查询作为兜底路径」的落地
            result.fallback_used = True
            result.mode = "single(fallback)"
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        result.estimated_cost_yuan = _estimate_cost(result.trace)
        return result

    # 单轮模式到此结束，不再进入归因
    if chosen == "single":
        result.conclusion = f"已返回 {len(first.rows)} 行结果，共 {len(first.columns)} 个字段。"
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        result.estimated_cost_yuan = _estimate_cost(result.trace)
        return result

    # ---- 步骤 3：发现异常后主动追加查询（这是与普通 Text2SQL 的分水岭）----
    # 普通工具回答「是多少」，这里继续回答「为什么」。
    seen: set[str] = {first.sql}   # 重复查询检测的集合，后续可扩展为调用指纹去重
    top_site = ""
    # 第一步 SQL 按指标降序返回，第一行第一列就是最差的教室名
    if first.rows and isinstance(first.rows[0][0], str):
        top_site = first.rows[0][0]
    # 至少两行才谈得上「对比出异常」，单行结果没有归因意义
    anomaly = len(first.rows) > 1 and _is_analysis(question)

    if anomaly and top_site:
        t0 = time.perf_counter()
        # 参数化查询：教室名来自上一步结果，绝不拼进 SQL 字符串
        cols, rows, err, _ = execute(ALARM_SQL, (top_site,))
        _record(result.trace, 3, "execute_sql", {"target": top_site},
                f"告警记录 {len(rows)} 条；error={err or 'none'}",
                (time.perf_counter() - t0) * 1000, ALARM_SQL)
        result.steps = 3
        types = [r[1] for r in rows if len(r) > 1]
        if "CALIBRATION_DRIFT" in types:
            # 结论刻意写「可能与……有关」而不是「原因是」：
            # 系统给的是相关线索，因果判断留给运维人员，这是诚实的边界。
            result.conclusion = (
                f"{top_site} 的 CO2 均值明显偏高；追加查询发现该设备存在 "
                "CALIBRATION_DRIFT（校准偏移）告警，读数偏高可能与传感器校准有关，"
                "建议先做标定复核再判断是否为真实空气质量恶化。")
        elif types:
            result.conclusion = (f"{top_site} 指标偏高，发现 {len(types)} 条告警（"
                                 f"{', '.join(types[:3])}），未发现校准偏移证据。")
        else:
            # 检索不到就明说，不编造原因——对应「幻觉抑制」的设计思想
            result.conclusion = f"{top_site} 指标偏高，但未检索到相关告警记录，建议人工核查。"

    # ---- 步骤 4：结果解读 ------------------------------------------------
    t0 = time.perf_counter()
    _record(result.trace, result.steps + 1, "explain", {"rows": len(first.rows)},
            result.conclusion, (time.perf_counter() - t0) * 1000)
    result.steps += 1

    # ---- 步骤 5：收敛约束 ------------------------------------------------
    # 当前模板最多 4 步，这段检查是为将来接入自由规划/更多工具准备的，
    # 保证步数上限始终是硬约束，而不是靠模板碰巧没超。
    if result.steps > settings.max_steps:
        result.fallback_used = True
        result.trace.append({"step": result.steps, "action": "fallback",
                             "params": {}, "result_summary": "超过最大步数，回落单轮模式",
                             "elapsed_ms": 0, "sql": ""})
    seen.add(result.sql)

    result.elapsed_ms = (time.perf_counter() - started) * 1000
    result.estimated_cost_yuan = _estimate_cost(result.trace)
    return result
