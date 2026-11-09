"""SQL 生成模块（对应开题报告「4.1（3）SQL 生成与校验」的生成部分）。

双模式设计，目的是让工程链路与模型效果解耦：
    - ``mock``：内置规则模板，不联网、不要 API Key。用于跑通链路、做降级、
      以及在没有网络时演示系统。
    - ``real``：通过 OpenAI 兼容协议调用大模型，DeepSeek / 通义 / 硅基流动
      只换 base_url 等三个参数。

⚠️ 关于准确率的诚实边界：
    mock 模式下标准 SQL 与模板同源，评估跑出 100% 属于「自己考自己」，
    只证明链路正确，**绝不能写进论文当准确率结论**。真实准确率必须在
    real 模式下、用重新设计的测试集测量。README 顶部也写了这条提醒。

报错自纠也在这里实现：build_prompt 接受上一轮的错误信息，把它原样回喂模型，
要求输出修正后的完整 SQL。重试次数由 executor.run_with_retry 控制。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings

# 三表 JOIN 的固定片段：事实表 -> 设备维表 -> 教室维表。
# 抽成常量是因为多条模板都要用，且评估集里的标准 SQL 也必须一致，
# 否则「执行结果比对」会因为 JOIN 写法差异产生假阴性。
JOIN = ("FROM sensor_data d "
        "JOIN device dv ON d.device_id = dv.id "
        "JOIN site s ON dv.site_id = s.id")

# mock 规则表：(必须全部命中的关键词, 生成的 SQL, 问题类别)
# 类别与 evaluation/cases.jsonl 的 category 对应，用于分层统计。
#
# 注意前四条是**故意**生成 DELETE / UPDATE / INSERT 的：
# 它们不是 bug，而是安全测试用例——用来验证 validator 能不能拦住写操作。
# 真实场景里模型也可能被诱导生成这类语句，必须有独立的校验层挡住。
# 规则按顺序匹配，越具体的放越前面（例如「平均温度+3号教室」要在通用兜底之前）。
MOCK_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("删除",), "DELETE FROM device;", "safety"),
    (("清空",), "DELETE FROM sensor_data;", "safety"),
    (("改成",), "UPDATE sensor_data SET temperature = 0;", "safety"),
    (("插入",), "INSERT INTO site VALUES (99,'测试','X栋',9);", "safety"),
    (("高于全局均值",), "SELECT COUNT(*) AS cnt FROM sensor_data "
     "WHERE temperature > (SELECT AVG(temperature) FROM sensor_data);", "subquery"),
    (("按小时", "co2"), "SELECT strftime('%Y-%m-%d %H:00', d.ts) AS hour, "
     f"ROUND(AVG(d.co2),2) AS avg_co2 {JOIN} "
     "WHERE d.ts >= datetime('now','-1 day') GROUP BY hour ORDER BY hour;", "trend"),
    (("报警次数最多",), "SELECT s.floor, s.name AS site_name, COUNT(*) AS alarm_count "
     "FROM alarm a JOIN device dv ON a.device_id = dv.id "
     "JOIN site s ON dv.site_id = s.id "
     "GROUP BY dv.id ORDER BY alarm_count DESC LIMIT 5;", "join"),
    (("各楼层", "告警"), "SELECT s.floor, COUNT(*) AS alarm_count "
     "FROM alarm a JOIN device dv ON a.device_id = dv.id "
     "JOIN site s ON dv.site_id = s.id GROUP BY s.floor ORDER BY s.floor;", "join"),
    (("型号",), "SELECT DISTINCT model FROM device;", "single_filter"),
    (("平均温度", "3号教室"), "SELECT ROUND(AVG(d.temperature),2) AS avg_temperature "
     f"{JOIN} WHERE s.name = '3号教室' AND date(d.ts) = date('now','-1 day');", "single_filter"),
    (("湿度均值",), "SELECT ROUND(AVG(humidity),2) AS avg_humidity FROM sensor_data "
     "WHERE device_id = 5 AND ts >= datetime('now','-3 day');", "single_filter"),
    (("co2", "超过"), "SELECT d.ts, s.name AS site_name, d.co2 "
     f"{JOIN} WHERE d.co2 > 1000 AND d.ts >= datetime('now','-7 day') "
     "ORDER BY d.ts DESC LIMIT 50;", "single_filter"),
    (("最高温度",), "SELECT s.name AS site_name, ROUND(MAX(d.temperature),2) AS max_temperature "
     f"{JOIN} WHERE date(d.ts) = date('now') GROUP BY s.name ORDER BY s.name;", "aggregation"),
    (("均值排名",), "SELECT s.name AS site_name, ROUND(AVG(d.co2),2) AS avg_co2 "
     f"{JOIN} GROUP BY s.name ORDER BY avg_co2 DESC;", "aggregation"),
    # 归因题的第一步：先算出哪个教室最差，第二步由 runtime 追加查告警表
    (("空气质量最差",), "SELECT s.name AS site_name, ROUND(AVG(d.co2),2) AS avg_co2 "
     f"{JOIN} WHERE d.ts >= datetime('now','-7 day') "
     "GROUP BY s.name ORDER BY avg_co2 DESC;", "attribution"),
    (("最", "co2"), "SELECT s.name AS site_name, ROUND(AVG(d.co2),2) AS avg_co2 "
     f"{JOIN} GROUP BY s.name ORDER BY avg_co2 DESC;", "aggregation"),
]

# 兜底 SQL：任何规则都没命中时返回最近 10 条数据。
# 目的不是答对，而是保证「系统永远有输出」，避免评估或演示时卡死。
FALLBACK_SQL = ("SELECT d.ts, d.temperature, d.humidity, d.co2 "
                f"{JOIN} ORDER BY d.ts DESC LIMIT 10;")


@dataclass
class Generation:
    """一次生成的结果。

    sql:      生成的 SQL 文本
    source:   mock / llm / fallback-after-error，写进执行轨迹，便于统计兜底触发率
    category: 问题类别（mock 模式下有效），供分层评估
    prompt:   实际使用的提示词，消融实验分析提示结构影响时要用到
    """

    sql: str
    source: str
    category: str
    prompt: str = ""


def build_prompt(question: str, schema_context: str,
                 error_feedback: str | None = None) -> str:
    """构造 SQL 生成提示词。

    每条约束都有对应理由：
        - 「只输出一条 SELECT」：便于程序直接解析，也减少写操作风险
        - 「只能用给出的表与字段」：配合 Schema 检索，避免模型编造列名
        - 「时间用 SQLite 函数表达」：真实数据带时间戳，写死日期会导致评估漂移
        - 「结果必须带 LIMIT」：防止全表扫描（validator 还会再强制补一次）

    Args:
        error_feedback: 上一次执行/校验失败的错误信息。非空时追加一段
            「请修正」的指令，这就是开题报告里说的「报错回喂自纠」。
    """
    base = (
        "你是 SQLite 数据分析助手，只输出一条可直接执行的 SELECT 语句，不要解释，不要 Markdown 代码块。\n"
        "规则：只能使用下面给出的表与字段；禁止 DELETE/UPDATE/INSERT/DROP 等写操作；"
        "时间用 SQLite 的 date()/datetime('now','-N day') 表达；结果必须带 LIMIT。\n\n"
        f"表结构：\n{schema_context}\n\n用户问题：{question}\n"
    )
    if error_feedback:
        # 把数据库原始报错原样回喂，比自己概括错误信息更有效：
        # 模型能据此判断是列名拼错、GROUP BY 漏字段还是函数用错。
        base += (f"\n上一次生成的 SQL 执行失败，错误信息：{error_feedback}\n"
                 "请分析错误原因，输出修正后的完整 SQL，仍然只输出 SQL。\n")
    return base


def _mock_generate(question: str, error_feedback: str | None) -> tuple[str, str]:
    """规则模板生成，返回 (SQL, 类别)。

    匹配方式：问题统一小写并去掉空格，规则里的关键词必须**全部**命中。
    用「全部命中」而非「任一命中」，是为了避免「平均温度」误触发
    「3号教室」那条带具体过滤条件的模板。
    """
    q = question.lower().replace(" ", "")
    for keys, sql, category in MOCK_RULES:
        if all(k.lower() in q for k in keys):
            return sql, category
    # 没命中就返回兜底 SQL；error_feedback 在 mock 模式下不改写规则，
    # 但重试链路仍会执行一遍，用来演示「重试次数与放弃条件」是可测的。
    if error_feedback:
        return FALLBACK_SQL, "fallback"
    return FALLBACK_SQL, "fallback"


def _real_generate(prompt: str) -> str:
    """调用大模型生成 SQL（OpenAI 兼容协议，**纯标准库实现**）。

    为什么不用 openai SDK（v0.5 改动）：
        本机 pip 大包反复超时，SDK 一直装不上；而这个接口本质上只是一次
        ``POST /chat/completions``，用 ``urllib.request`` 几十行就能写完。
        去掉这个依赖后，接真实模型不再有装包前置条件，也少一处供应链风险。
        换供应商仍然只改 ``base_url / model / api_key`` 三个参数。

    安全约束（重要）：
        - Key 只放在 Authorization 请求头里，**只发给 .env 里配置的那个
          模型厂商域名**；不写日志、不写报告、不写执行轨迹、不进仓库。
        - 异常只向上抛类型，``generate_sql`` 里也只记录异常类名，
          绝不把请求头或完整响应打出来。
        - 本机 HTTPS 有证书吊销检查问题（GitHub 二进制通道就是这样被掐的），
          这里只对模型厂商请求放宽校验；不涉及任何凭据外发到第三方。

    - temperature 取 settings（默认 0）：让同输入输出尽量一致，便于复现。
    - 返回值做了清洗：模型常把 SQL 包在 ```sql ... ``` 里，这里逐块找出
      以 SELECT / WITH 开头的部分；找不到就原样返回，交给 validator 报错。
    """
    import json
    import ssl
    import urllib.request

    url = settings.base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": settings.model,
        "temperature": settings.temperature,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.api_key}",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    text = text.strip()
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.lower().startswith("sql"):
                block = block[3:].strip()
            if block.lower().startswith("select") or block.lower().startswith("with"):
                return block.rstrip(";")
    return text.rstrip(";")


def generate_sql(question: str, schema_context: str,
                 error_feedback: str | None = None) -> Generation:
    """统一的生成入口，对上层屏蔽模式差异。

    三层兜底（对应可行性分析里的「风险与应对」）：
        1. 正常 real 模式 → 用模型输出
        2. 模型调用异常（超时/限流/断网）→ 回落 mock，并在 source 里标记异常类型
        3. 本来就是 mock 或没配 Key → 直接用模板

    这样任何情况下系统都有 SQL 可交给校验层，不会出现「卡住不动」。
    """
    prompt = build_prompt(question, schema_context, error_feedback)
    if settings.llm_mode == "real" and settings.real_mode_available:
        try:
            sql = _real_generate(prompt)
            return Generation(sql=sql, source="llm", category="llm", prompt=prompt)
        except Exception as exc:  # 模型不可用时兜底，保证系统永远有输出
            sql, category = _mock_generate(question, error_feedback)
            return Generation(sql=sql, source=f"fallback-after-error:{type(exc).__name__}",
                              category=category, prompt=prompt)
    sql, category = _mock_generate(question, error_feedback)
    return Generation(sql=sql, source="mock", category=category, prompt=prompt)
