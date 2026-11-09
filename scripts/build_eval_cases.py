"""生成正式评估集 src/evaluation/cases_eval.jsonl（144 条）。

用法：``python scripts/build_eval_cases.py``

设计目的：解决 15 条冒烟集的两个问题——
    1. **题量太小**，分层统计没有统计意义；
    2. **问题表述与 mock 模板同源**，属于「自己考自己」。

做法：18 个语义原型 × 8 种口语化表述变体 = 144 条（达到开题报告承诺的 ≥140 条）。
变体刻意改变语序、同义词、数字写法（3 / 三号 / ３），
用来测「同一语义、不同说法」下的稳定性，这是 NL2SQL 的标准评测维度。

v0.4.1 的关键修正：标准答案 SQL 独立人工标注
-------------------------------------------
v0.2 时标准 SQL 直接引用生成器里的 MOCK_RULES，等于拿被测对象的规则去判
自己的答案，mock 模式的数字带有**自证偏差**。本版把每条标准 SQL 写成
本文件里的常量，与 src/sql/generator.py 完全解耦：

    - 生成器怎么改、将来换成真实大模型，标准答案都不变；
    - 标准结果由 runner 对同一个库执行这些 SQL 动态取得，不写死数值；
    - 换数据源（synthetic / uci）后判定口径仍然自洽。

「独立」指的是**真值来源独立**，不是要求 SQL 故意写得不一样：同一个语义
问题往往只有一种正确写法，长得像恰恰说明标注没错。

判定口径（case.check）：
    result            执行结果集比对（默认）
    row_count         只校验返回行数
    scalar_tolerance  校验单个数值，允许浮点容差
    deny_sql          必须被安全层拦截（攻击 SQL 直接喂校验层，绕开生成层）
    conclusion        结论必须命中预设原因关键词

⚠️ 口径变更提醒：标准答案独立后，mock 模式准确率会**下降**，这是消除
自证偏差的正常结果，不能与 v0.2–v0.4 的 33.7% 直接比较。回归门禁的
baseline 已按新口径重新固化。真实准确率仍需在 real 模式下测量。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import CASES_PATH  # noqa: E402

OUT = CASES_PATH.parent / "cases_eval.jsonl"

# 三表 JOIN 片段在本文件里独立书写，不 import generator.JOIN。
# 目的不是换个写法，而是让评估集的真值完全不依赖被测代码——
# 只要标准答案来自 generator，消融实验与真实模型评估就都说不清。
JOIN3 = ("FROM sensor_data d "
         "JOIN device dv ON d.device_id = dv.id "
         "JOIN site s ON dv.site_id = s.id")

# 标准答案：18 个人工标注的 SQL 常量（v0.4.1 起）。
# 每条都能在 schema.sql 的四张表上直接执行；runner 会执行它们取结果集，
# 因此不写死任何数值，换数据源后结果自动跟着变。
SQL = {
    # 单表 / 点查
    "avg_temp_3": (
        "SELECT ROUND(AVG(d.temperature),2) AS avg_temperature "
        f"{JOIN3} WHERE s.name = '3号教室' AND date(d.ts) = date('now','-1 day');"
    ),
    "co2_over": (
        # 不带人为 LIMIT：题目问的是「所有时段」，LIMIT 只是防全表扫描的
        # 安全措施（validator 会自动补 row_limit）。标准答案写死 50 会让
        # 「模型写了 LIMIT 100、返回更多正确记录」反而被行数判定判错。
        "SELECT d.ts, s.name AS site_name, d.co2 "
        f"{JOIN3} WHERE d.co2 > 1000 AND d.ts >= datetime('now','-7 day') "
        "ORDER BY d.ts DESC;"
    ),
    "models": "SELECT DISTINCT model FROM device;",
    "humidity": (
        "SELECT ROUND(AVG(humidity),2) AS avg_humidity FROM sensor_data "
        "WHERE device_id = 5 AND ts >= datetime('now','-3 day');"
    ),
    "hot_and_humid": (
        # 同 co2_over：标准答案不设人为展示上限，让结果集反映真实语义
        "SELECT d.ts, d.temperature, d.humidity FROM sensor_data d "
        "WHERE d.temperature > 23 AND d.humidity > 45 "
        "ORDER BY d.ts;"
    ),
    # 分组聚合
    "max_temp": (
        "SELECT s.name AS site_name, ROUND(MAX(d.temperature),2) AS max_temperature "
        f"{JOIN3} WHERE date(d.ts) = date('now') GROUP BY s.name ORDER BY s.name;"
    ),
    "co2_rank": (
        "SELECT s.name AS site_name, ROUND(AVG(d.co2),2) AS avg_co2 "
        f"{JOIN3} GROUP BY s.name ORDER BY avg_co2 DESC;"
    ),
    "co2_top": (
        "SELECT s.name AS site_name, ROUND(AVG(d.co2),2) AS avg_co2 "
        f"{JOIN3} GROUP BY s.name ORDER BY avg_co2 DESC;"
    ),
    "top_devices": (
        "SELECT dv.id, dv.model, ROUND(AVG(d.temperature),2) AS avg_temperature "
        "FROM sensor_data d JOIN device dv ON d.device_id = dv.id "
        "GROUP BY dv.id ORDER BY avg_temperature DESC LIMIT 5;"
    ),
    # 三表 JOIN
    "alarm_floor": (
        "SELECT s.floor, s.name AS site_name, COUNT(*) AS alarm_count "
        "FROM alarm a JOIN device dv ON a.device_id = dv.id "
        "JOIN site s ON dv.site_id = s.id "
        "GROUP BY dv.id ORDER BY alarm_count DESC LIMIT 5;"
    ),
    "alarm_by_floor": (
        "SELECT s.floor, COUNT(*) AS alarm_count "
        "FROM alarm a JOIN device dv ON a.device_id = dv.id "
        "JOIN site s ON dv.site_id = s.id GROUP BY s.floor ORDER BY s.floor;"
    ),
    "alarm_type_by_building": (
        "SELECT s.building, a.type, COUNT(*) AS alarm_count "
        "FROM alarm a JOIN device dv ON a.device_id = dv.id "
        "JOIN site s ON dv.site_id = s.id "
        "GROUP BY s.building, a.type ORDER BY s.building, a.type;"
    ),
    # 嵌套子查询
    "above_avg": (
        "SELECT COUNT(*) AS cnt FROM sensor_data "
        "WHERE temperature > (SELECT AVG(temperature) FROM sensor_data);"
    ),
    "devices_with_alarms": (
        "SELECT COUNT(*) AS cnt FROM device "
        "WHERE id IN (SELECT DISTINCT device_id FROM alarm);"
    ),
    # 时序趋势
    "hourly": (
        "SELECT strftime('%Y-%m-%d %H:00', d.ts) AS hour, "
        f"ROUND(AVG(d.co2),2) AS avg_co2 {JOIN3} "
        "WHERE d.ts >= datetime('now','-1 day') GROUP BY hour ORDER BY hour;"
    ),
    "daily": (
        "SELECT date(d.ts) AS day, ROUND(AVG(d.co2),2) AS avg_co2 "
        "FROM sensor_data d GROUP BY day ORDER BY day;"
    ),
    # 归因（第一步查询；第二步由 runtime 追加查告警表）
    "attribution": (
        "SELECT s.name AS site_name, ROUND(AVG(d.co2),2) AS avg_co2 "
        f"{JOIN3} WHERE d.ts >= datetime('now','-7 day') "
        "GROUP BY s.name ORDER BY avg_co2 DESC;"
    ),
}

# 18 个语义原型。每个 prototype 的 8 个变体必须是同一语义的不同说法。
PROTOTYPES: list[dict] = [
    {
        "prefix": "p01", "category": "single_filter", "check": "result",
        "sql": SQL["avg_temp_3"], "note": "单表指标 + 时间条件",
        "variants": [
            "3号教室昨天的平均温度是多少",
            "昨天三号教室平均多少度",
            "帮我查一下 3 号教室昨天的温度均值",
            "3号教室昨日气温的平均值",
            "想看 3 号教室昨天的平均气温",
            "昨天 3 号教室温度平均下来是多少",
            "3 号教室，昨天，平均温度，给我个数",
            "统计一下三号教室昨日的温度平均值",
        ],
    },
    {
        "prefix": "p02", "category": "single_filter", "check": "row_count",
        "sql": SQL["co2_over"], "note": "条件筛选；只校验行数，测召回稳定性",
        "variants": [
            "列出最近一周 CO2 超过 1000 的所有时段",
            "过去七天二氧化碳大于 1000 的记录有哪些",
            "把这一周 co2 高于 1000ppm 的数据找出来",
            "最近 7 天哪些时刻的 CO₂ 读数破了一千",
            "查一下近一周二氧化碳浓度超过 1000 的采样点",
            "一周内 CO2 > 1000 的时段给我列出来",
            "有没有最近七天 CO2 超 1000 的记录",
            "筛出本周二氧化碳值大于 1000 的所有记录",
        ],
    },
    {
        "prefix": "p03", "category": "single_filter", "check": "result",
        "sql": SQL["models"], "note": "维表点查",
        "variants": [
            "列出所有设备型号",
            "都有哪些型号的设备",
            "设备型号有哪几种",
            "把设备型号去重后列一下",
            "我想看看全部终端的型号",
            "目前用了哪几款设备型号",
            "查一下 device 表里有哪些 model",
            "给我一份设备型号清单",
        ],
    },
    {
        "prefix": "p04", "category": "single_filter", "check": "scalar_tolerance",
        "sql": SQL["humidity"], "tolerance": 0.01,
        "note": "单值聚合；允许浮点容差",
        "variants": [
            "5号设备最近三天的湿度均值",
            "5 号终端这三天的平均湿度",
            "最近三天，编号 5 的设备湿度平均多少",
            "查一下设备 5 近 3 日湿度平均值",
            "三天内 5 号设备的平均相对湿度",
            "5 号设备过去 72 小时的湿度均值是多少",
            "帮我算 5 号设备最近三天的湿度平均",
            "设备编号 5，近三天，湿度求平均",
        ],
    },
    {
        "prefix": "p05", "category": "aggregation", "check": "result",
        "sql": SQL["max_temp"], "note": "按日 + 分组聚合",
        "variants": [
            "今天各教室的最高温度",
            "今天每个教室温度最高是多少",
            "按教室统计今日最高气温",
            "今日各房间的最高温",
            "今天各个点位温度峰值分别是多少",
            "查今天每间教室的温度最大值",
            "今天各站点 MAX(温度)",
            "把今天每个教室的最高温度列出来",
        ],
    },
    {
        "prefix": "p06", "category": "aggregation", "check": "result",
        "sql": SQL["co2_rank"], "note": "分组聚合 + 排序",
        "variants": [
            "各教室本月的 CO2 均值排名",
            "按教室排一下二氧化碳平均值",
            "每个教室的 CO2 平均浓度从高到低",
            "给我各点位 co2 均值的排行榜",
            "统计各教室二氧化碳均值并排序",
            "哪几个教室平均 CO2 高，按高低列出来",
            "按平均 co2 给教室做个排名",
            "各房间 CO₂ 平均值降序",
        ],
    },
    {
        "prefix": "p07", "category": "aggregation", "check": "result",
        "sql": SQL["co2_top"], "note": "Top-1 聚合",
        "variants": [
            "哪个教室的 CO2 均值最高",
            "二氧化碳平均浓度最高的是哪间教室",
            "平均 co2 最大的站点是哪个",
            "找一下 CO₂ 均值排第一的教室",
            "哪间教室平均二氧化碳最多",
            "CO2 平均最高的房间叫什么",
            "按平均 CO2 看，哪个点位最高",
            "告诉我二氧化碳均值最高的教室",
        ],
    },
    {
        "prefix": "p08", "category": "join", "check": "result",
        "sql": SQL["alarm_floor"], "note": "三表 JOIN",
        "variants": [
            "报警次数最多的设备属于哪个楼层",
            "告警最多的那台设备在几楼",
            "哪层楼的设备报警最频繁",
            "按告警数量排第一的设备在哪个楼层",
            "查一下报警次数最高的终端所在楼层",
            "设备告警次数最多，它属于哪一层",
            "哪个楼层的设备告警最多（按设备统计）",
            "找出告警最多的设备并给出它的楼层",
        ],
    },
    {
        "prefix": "p09", "category": "join", "check": "result",
        "sql": SQL["alarm_by_floor"], "note": "告警与楼层关联",
        "variants": [
            "统计各楼层的告警数量",
            "每层楼分别有多少条告警",
            "按楼层汇总报警次数",
            "各层告警条数统计",
            "给我按 floor 分组的告警计数",
            "一楼二楼三楼各有多少告警记录",
            "楼层维度的告警数量分布",
            "查一下每个楼层的报警总数",
        ],
    },
    {
        "prefix": "p10", "category": "subquery", "check": "scalar_tolerance",
        "sql": SQL["above_avg"], "tolerance": 0,
        "note": "嵌套子查询；整数计数",
        "variants": [
            "温度高于全局均值的记录有多少条",
            "比整体平均温度高的采样记录有几条",
            "统计温度超过全表平均值的行数",
            "有多少条记录的温度在均值以上",
            "温度 > 所有数据平均温度的条数",
            "查一下高于平均气温的记录数",
            "超过全局温度均值的记录数量",
            "计数：温度大于整体平均值的行",
        ],
    },
    {
        "prefix": "p11", "category": "trend", "check": "row_count",
        "sql": SQL["hourly"], "note": "时间粒度聚合；只校验分桶数",
        "variants": [
            "按小时统计今天的 CO2 变化",
            "今天每小时的二氧化碳平均值",
            "把今日 CO2 按小时做个趋势",
            "按小时聚合今天的 co2 数据",
            "今天各时段的 CO₂ 均值曲线",
            "以小时为粒度看今天二氧化碳变化",
            "今天每小时平均 CO2 是多少",
            "给我今天 CO2 的逐小时统计",
        ],
    },
    {
        "prefix": "p12", "category": "safety", "check": "deny_sql",
        "sql": "",
        "note": "直接把危险 SQL 喂给校验层，独立测「拦截率 100%」，不依赖生成层是否命中关键词",
        # 与上面 8 个变体一一对应的攻击语句，覆盖 DELETE / DROP / UPDATE / TRUNCATE
        "attacks": [
            "DELETE FROM device;",
            "DELETE FROM sensor_data;",
            "DELETE FROM sensor_data WHERE co2 > 0;",
            "DELETE FROM alarm;",
            "UPDATE sensor_data SET temperature = 0;",
            "TRUNCATE TABLE sensor_data;",
            "DROP TABLE site;",
            "UPDATE sensor_data SET temperature = 0 WHERE device_id = 3;",
        ],
        "variants": [
            "删除所有设备记录",
            "把 device 表清空",
            "删掉全部监测数据",
            "帮我移除所有告警记录",
            "把所有教室的温度改成 0",
            "清空 sensor_data 这张表",
            "执行 DELETE FROM site",
            "把 3 号教室的数据全部更新成 0",
        ],
    },
    {
        "prefix": "p13", "category": "attribution", "check": "conclusion",
        "sql": "", "expect_conclusion": "校准",
        "note": "多步归因；结论必须命中校准偏移线索",
        "variants": [
            "这周哪个教室空气质量最差，为什么",
            "最近七天哪间教室空气最差，原因是什么",
            "分析一下本周空气质量最差的点位以及可能原因",
            "哪个教室 CO2 最高，是不是设备有问题",
            "帮我找找这周空气质量异常最严重的教室并解释",
            "本周最差教室是哪个，怎么回事",
            "查一下最近一周哪间教室读数异常，给出原因线索",
            "这周空气质量最不行的教室，为什么会这样",
        ],
    },
    # ---- v0.4.1 新增 5 个原型：把七类题量补齐到 144 条，并增加
    #      多条件筛选 / 告警类型 JOIN / 按日趋势 / Top-N / IN 子查询的覆盖 ----
    {
        "prefix": "p14", "category": "single_filter", "check": "result",
        "sql": SQL["hot_and_humid"], "note": "双条件筛选；检验 AND 条件不丢",
        "variants": [
            "找出温度高于 23 度且湿度超过 45 的采样记录",
            "温度 >23 并且湿度 >45 的数据有哪些",
            "筛出又热又潮的那些时刻（23 度、45 湿度）",
            "把温度超过 23、相对湿度大于 45 的记录列出来",
            "查一下同时满足高温 23 和高湿 45 的采样点",
            "哪些记录温度在 23 以上、湿度在 45 以上",
            "给我温度大于 23 且 humidity 大于 45 的数据",
            "列出又热（>23℃）又湿（>45%）的记录",
        ],
    },
    {
        "prefix": "p15", "category": "join", "check": "result",
        "sql": SQL["alarm_type_by_building"], "note": "三表 JOIN + 双字段分组",
        "variants": [
            "按楼栋和告警类型统计告警数量",
            "每栋楼各类告警分别有多少条",
            "统计各楼不同报警类型的次数",
            "楼栋 + 告警类型的二维分布",
            "按 building 和 type 分组数一下告警",
            "各栋楼里每种告警各出现了几次",
            "给我按楼宇、告警类别汇总的数量",
            "查一下每栋楼各类 alarm 的条数",
        ],
    },
    {
        "prefix": "p16", "category": "trend", "check": "row_count",
        "sql": SQL["daily"], "note": "按日聚合；只校验分桶数，避免时间窗口漂移",
        "variants": [
            "按天统计 CO2 的日均值",
            "每天二氧化碳平均浓度是多少",
            "把 CO2 按日期做个逐日趋势",
            "以天为粒度汇总 co2 均值",
            "给我每日 CO₂ 平均值曲线",
            "按 date 分组算一下二氧化碳均值",
            "这几天每天的平均 CO2",
            "统计每日二氧化碳浓度的平均水平",
        ],
    },
    {
        "prefix": "p17", "category": "aggregation", "check": "result",
        "sql": SQL["top_devices"], "note": "Top-N + 两表 JOIN + 排序",
        "variants": [
            "平均温度最高的 5 台设备",
            "按平均温度给设备排个前五",
            "哪五台设备的均温最高",
            "列出均温排名前 5 的终端及型号",
            "设备平均温度 TOP5 是谁",
            "给我平均气温最高的五台设备",
            "按 avg 温度降序取前五个设备",
            "查一下温度均值排名前 5 的设备",
        ],
    },
    {
        "prefix": "p18", "category": "subquery", "check": "scalar_tolerance",
        "sql": SQL["devices_with_alarms"], "tolerance": 0,
        "note": "IN 子查询；检验集合成员判断不退化成 JOIN 计数",
        "variants": [
            "有过告警记录的设备一共有多少台",
            "统计产生过告警的设备数量",
            "有多少台设备出现在告警表里",
            "查一下至少有一条告警的设备数",
            "出现过报警的终端总数是几",
            "在 alarm 表里有记录的设备有几台",
            "算一下发生过告警的不重复设备数",
            "有告警历史的设备数量是多少",
        ],
    },
]


def build() -> list[dict]:
    """把原型展开成逐条用例，id 形如 p01-03，便于定位失败项。"""
    cases: list[dict] = []
    for proto in PROTOTYPES:
        for i, question in enumerate(proto["variants"], start=1):
            case = {
                "id": f"{proto['prefix']}-{i:02d}",
                "category": proto["category"],
                "check": proto["check"],
                "question": question,
                "expected_sql": proto["sql"],
                "note": proto["note"],
            }
            # 判定口径需要的额外参数，按类型挂上
            if proto["check"] == "conclusion":
                case["expect_conclusion"] = proto["expect_conclusion"]
            if proto["check"] == "scalar_tolerance":
                case["tolerance"] = proto["tolerance"]
            if proto["check"] == "deny_sql":
                # 攻击 SQL 与变体按下标一一对应，绕过生成层直接考校验层
                case["attack_sql"] = proto["attacks"][i - 1]
            if proto["check"] == "row_count":
                # 行数不由人工写死，由 runner 执行标准 SQL 后动态取 len；
                # 这里只标记口径，避免标准 SQL 改动后行数过期。
                case["dynamic_row_count"] = True
            cases.append(case)
    return cases


def main() -> None:
    cases = build()
    OUT.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
                   encoding="utf-8")
    by_cat: dict[str, int] = {}
    for c in cases:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    print(f"已生成 {len(cases)} 条 -> {OUT}")
    for k, v in sorted(by_cat.items()):
        print(f"  {k:<16} {v}")
    print("\n标准答案 SQL 为独立人工标注（不依赖 generator），真值来源已解耦；")
    print("mock 模式数字仅供链路自检，正式准确率请在 real 模式下测量。")


if __name__ == "__main__":
    main()
