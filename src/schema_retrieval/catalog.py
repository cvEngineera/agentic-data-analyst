"""Schema 检索模块（对应开题报告「4.1（2）Schema 检索」）。

要解决的问题：数据库表一多，没法把全部 DDL 塞进提示词；而且中文业务用语
（「教室」「空气质量」）与英文字段名（site.name、sensor_data.co2）没有字面重合，
纯关键词会漏、纯向量又可能召回语义相近但无关的表。

基线实现三招：
    A. 中文别名 / 业务术语打分（keyword_score）
    B. 外键一跳扩展（FK_EXPANSION），选中事实表时自动补上关联维表
    C. 语义向量接口预留（hybrid_score），接入 Chroma / pgvector 后无需改调用方

**为什么把向量检索做成函数而不是直接依赖**：这是消融实验的开关点。
关掉向量 = 纯关键词基线，打开 = 混合检索，两者跑同一套测试集就能算出
「语义检索贡献了几个百分点」，正是论文第七章要的数据。

本模块不碰数据库文件，只依赖传入的 available_tables 列表，便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings

# 数据字典：业务术语 -> 物理表/字段。
# 这是开题报告「数据字典建设」的落地，也是缓解中英文语义鸿沟的核心资产。
# aliases 用于问题命中打分；columns 的中文说明会被拼进提示词 DDL 注释，
# 让模型知道「co2 就是二氧化碳」，而不是只看字段名猜。
TABLE_META: dict[str, dict] = {
    "site": {
        "aliases": ["教室", "站点", "房间", "位置", "楼层", "楼栋", "建筑"],
        "comment": "监测点位/教室维表",
        "columns": {"id": "站点编号", "name": "教室名称，如 3号教室",
                    "building": "所在楼栋", "floor": "所在楼层"},
    },
    "device": {
        "aliases": ["设备", "终端", "传感器节点", "型号", "安装"],
        "comment": "设备维表",
        "columns": {"id": "设备编号", "site_id": "所属站点",
                    "model": "设备型号", "install_date": "安装日期"},
    },
    "sensor_data": {
        "aliases": ["温度", "湿度", "CO2", "二氧化碳", "TVOC", "PM2.5",
                    "空气质量", "监测数据", "读数", "时序", "均值", "趋势"],
        "comment": "环境监测事实表，每 30 分钟一条",
        "columns": {"id": "记录编号", "device_id": "设备编号", "ts": "采集时间",
                    "temperature": "温度(℃)", "humidity": "湿度(%)",
                    "co2": "二氧化碳(ppm)", "tvoc": "TVOC", "pm25": "PM2.5"},
    },
    "alarm": {
        "aliases": ["告警", "报警", "故障", "异常", "离线", "校准", "次数"],
        "comment": "设备告警记录表",
        "columns": {"id": "告警编号", "device_id": "设备编号", "ts": "告警时间",
                    "type": "告警类型", "level": "告警级别"},
    },
    "v_site_air_quality": {
        "aliases": ["空气质量汇总", "各教室空气质量"],
        "comment": "按站点聚合的空气质量视图",
        "columns": {"site_id": "站点编号", "site_name": "教室名称",
                    "building": "楼栋", "floor": "楼层",
                    "avg_co2": "CO2 均值", "avg_pm25": "PM2.5 均值",
                    "avg_temperature": "温度均值", "sample_count": "样本数"},
    },
}

# 外键一跳扩展：选中事实表时自动补上关联维表。
# 「一跳」是刻意的：两跳以上会把整库表都拉进来，提示词变长、噪声增大，
# 反而降低生成质量。这个取舍可以在消融实验里单独验证。
FK_EXPANSION: dict[str, list[str]] = {
    "sensor_data": ["device", "site"],
    "alarm": ["device", "site"],
    "device": ["site"],
    "v_site_air_quality": ["site"],
}


@dataclass
class RetrievalResult:
    """一次 Schema 检索的结果。

    tables:   最终注入提示词的表（命中表 + 扩展表），顺序即相关度顺序
    scores:   每张候选表的打分，写进执行轨迹，便于调试「为什么选错表」
    expanded: 由外键扩展补进来的表，单独记录以便做消融统计
    """

    tables: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    expanded: list[str] = field(default_factory=list)

    @property
    def context(self) -> str:
        """把选中的表直接转成可注入提示词的 DDL 片段。"""
        return build_schema_context(self.tables)


def keyword_score(question: str, table: str) -> float:
    """计算「用户问题」与「某张表」的关键词相关度。

    权重是经验值，理由：
        - 别名命中 +2.0：中文业务词是最强信号（「教室」几乎必然指 site）
        - 物理列名命中 +1.0：用户偶尔会直接说字段名
        - 列注释片段命中 +0.2：弱信号，只用于打破平分
        - 表名本身出现 +3.0：显式指定，优先级最高

    返回 0 表示该表与问题无关，不会被选中。
    """
    q = question.lower()
    meta = TABLE_META.get(table, {})
    score = 0.0
    for word in meta.get("aliases", []):
        if word.lower() in q:
            score += 2.0
    for col, desc in meta.get("columns", {}).items():
        if col.lower() in q:
            score += 1.0
        # 只取括号前的中文描述，避免把单位符号也当匹配词
        if any(seg in q for seg in desc.lower().split("(")[:1]) and len(desc) > 4:
            score += 0.2
    if table in q:
        score += 3.0
    return score


def hybrid_score(question: str, table: str) -> float:
    """混合检索打分 = 关键词/数据字典分 + 可选的语义相似度。

    v0.7 之前这里只是纯关键词基线，向量分支是注释里的伪代码；本版把
    ``semantic.semantic_score()`` 真正接进来，让开题报告 4.2 写的
    「向量 + 关键词混合检索」不再是空话。

    为什么默认关闭（``settings.enable_semantic_retrieval``）：
        v0.6 的 65.3%、五组消融、重复实验都是纯关键词口径下的数字。
        若悄悄改默认，那些已固化的实验结论就失去可比性。语义层作为
        **可选增强**由 ``scripts/compare_semantic.py`` 显式开启并量化贡献，
        论文里可以报告「开启后 X pp」，主方案数字保持不动。

    为什么用函数而不是直接依赖向量库：
        这是消融开关点，也是 provider 替换点。关掉 = 纯关键词基线，
        打开 = 混合检索；将来换成 Chroma / pgvector 查询时，只改
        ``semantic.py`` 内部实现，本函数与 retrieve() 一行都不用动。
    """
    score = keyword_score(question, table)
    if not settings.enable_semantic_retrieval:
        return score
    from .semantic import semantic_score  # 局部导入：默认路径不加载语义模块
    # 权重 0.6：关键词别名命中仍然是强信号（命中一次 +3），语义分只做
    # 补充排序，不能反过来压过明确的业务术语匹配。
    return score + 0.6 * semantic_score(question, table)


def retrieve(question: str, available_tables: list[str], top_k: int = 4) -> RetrievalResult:
    """检索出该问题最可能用到的表。

    Args:
        question: 用户自然语言问题。
        available_tables: 数据库自省得到的全部表名，避免检索出不存在的表。
        top_k: 最多取几张表。取 4 是因为四张业务表刚好覆盖三表 JOIN 场景，
               再大就超出本课题的数据规模，只会增加提示词噪声。

    Returns:
        RetrievalResult，tables 已包含外键扩展结果。

    兜底策略：一个词都没命中时默认给 sensor_data（事实表），
    保证系统永远有输出，不会因为检索空集直接失败。
    """
    scored = {t: hybrid_score(question, t) for t in available_tables
              if t in TABLE_META}
    # 按分数降序、同分按表名升序，保证结果稳定可复现
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    selected = [t for t, s in ranked if s > 0][:top_k]
    if not selected:
        selected = ["sensor_data"] if "sensor_data" in available_tables else available_tables[:1]

    # 外键一跳扩展：选中事实表但没选维表时补上，否则三表 JOIN 会缺表。
    # 消融开关：关掉后只保留关键词直接命中的表，用来量化「外键扩展」
    # 这一层对准确率的贡献（论文第七章消融实验）。
    expanded: list[str] = []
    if settings.ablation_fk_expansion:
        for t in list(selected):
            for extra in FK_EXPANSION.get(t, []):
                if extra in available_tables and extra not in selected and extra not in expanded:
                    expanded.append(extra)
    return RetrievalResult(
        tables=selected + expanded,
        scores={t: round(s, 2) for t, s in scored.items()},
        expanded=expanded,
    )


def build_schema_context(tables: list[str]) -> str:
    """把选中的表拼成「带中文注释的 DDL」，注入提示词。

    为什么用 DDL 形式而不是自然语言描述：大模型在预训练里见过大量
    CREATE TABLE，按这个格式给上下文，生成 SQL 的语法正确率更高；
    行内注释则负责补上业务语义（模型看不到数据库里的 COMMENT）。
    """
    lines = []
    for t in tables:
        meta = TABLE_META.get(t)
        if not meta:
            continue
        cols = ",\n    ".join(f"{c} -- {d}" for c, d in meta["columns"].items())
        lines.append(f"CREATE TABLE {t} (\n    {cols}\n); -- {meta['comment']}")
    return "\n\n".join(lines)
