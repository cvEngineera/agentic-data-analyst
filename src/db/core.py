"""数据层：DDL 执行、只读连接、表结构自省，以及两种可切换的数据源。

对应开题报告「4.1（1）数据层建设」与「4.3 技术选型」里的 SQLite/MySQL 一层。

四个设计要点：
1. **查询只读**。执行链路一律用 SQLite 的 ``mode=ro`` URI 打开，再叠加
   ``PRAGMA query_only=ON``；写入只发生在初始化脚本里。这是安全管控的
   第一道防线，即使上层校验被绕过，数据库层面也写不进去。
2. **实验可复现**。合成数据用固定随机种子 42，任何人重建库得到的数值一致，
   否则「准确率提升」无法归因到算法改动。
3. **内置可演示的异常剧情**。3 号教室最近两天 CO2 显著偏高，并伴随一条
   CALIBRATION_DRIFT 告警，用于验证「发现异常 → 追加查询 → 定位原因」这条
   多步归因链路是否真的走通。
4. **数据接入适配器（v0.4 已落地）**。`--source uci` 换用 UCI 公开真实数据集，
   只替换 sensor_data 的读数与字段映射；四张表结构、维表、告警剧情、其余
   六个模块一行都不用改。实现见 uci_loader.py，两种源由 init_database 分派。

表结构（见 schema.sql）：site（教室维表）、device（设备维表）、
sensor_data（监测事实表）、alarm（告警表），另有聚合视图 v_site_air_quality。
"""
from __future__ import annotations

import math
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ..config import DB_PATH, SCHEMA_PATH, ensure_dirs, settings

# 表白名单：validator 用它判断 SQL 是否引用了不存在的表。
# 视图也算合法对象，因为评估里允许直接查聚合视图。
ALLOWED_TABLES = ("site", "device", "sensor_data", "alarm", "v_site_air_quality")

# 维表数据两种数据源共用。放在模块级而不是各自写在 seed() 里，是为了避免
# 换数据源时教室/设备编号漂移，导致「三表 JOIN」类评估题的答案跟着变。
SITE_ROWS = (
    (1, "1号教室", "A栋", 1),
    (2, "2号教室", "A栋", 2),
    (3, "3号教室", "B栋", 1),
    (4, "4号教室", "B栋", 2),
    (5, "5号教室", "C栋", 3),
)
DEVICE_ROWS = tuple((i, i, f"AQ-200{i}", "2026-09-01") for i in range(1, 6))


def connect(readonly: bool = True, timeout: int | None = None) -> sqlite3.Connection:
    """建立数据库连接。

    Args:
        readonly: 是否只读。查询链路恒为 True；只有 init_database / seed 用 False。
        timeout: 连接超时秒数，默认取 settings.query_timeout。

    Returns:
        已设置 row_factory 与 PRAGMA 的 sqlite3.Connection。调用方负责 close()。

    设计说明：
        - ``as_uri() + "?mode=ro"`` 是文件级只读，比事后检查 SQL 更可靠。
        - 某些平台/旧版 SQLite 不支持 URI，故用 try/except 降级为普通连接；
          降级后仍有 ``PRAGMA query_only=ON`` 兜底，安全性不丢。
        - ``row_factory = sqlite3.Row`` 让结果可按列名访问，便于后续转 dict。
        - 外键约束默认关闭，这里显式打开，保证 JOIN 关系与数据一致性。
    """
    ensure_dirs()
    t = timeout if timeout is not None else settings.query_timeout
    if readonly and DB_PATH.exists():
        try:
            uri = DB_PATH.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=t)
        except sqlite3.Error:
            # URI 模式不可用时降级；query_only 仍会在下面设置
            conn = sqlite3.connect(str(DB_PATH), timeout=t)
    else:
        # 库文件还不存在时必须用读写连接创建它
        conn = sqlite3.connect(str(DB_PATH), timeout=t)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON" if readonly else "PRAGMA query_only = OFF")
    return conn


def introspect(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """自省数据库，返回 ``{表名: [列名, ...]}``。

    用途有二：给 Schema 检索提供候选表清单；给 validator 提供字段白名单。
    直接从库里读而不是写死在代码里，是为了换数据集/加字段时校验规则自动跟着变。

    排除 sqlite_ 开头的内部对象，否则白名单会被系统表污染。
    """
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    result: dict[str, list[str]] = {}
    for name in tables:
        # PRAGMA 不支持参数化，表名来自 sqlite_master 而非用户输入，故可直接拼接
        result[name] = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
    return result


def create_schema(conn: sqlite3.Connection) -> None:
    """执行 schema.sql，建表、建索引、建视图。

    用 executescript 因为 DDL 文件含多条语句；CREATE IF NOT EXISTS 保证可重复执行。
    """
    ddl = Path(SCHEMA_PATH).read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()


def seed_synthetic(conn: sqlite3.Connection) -> dict[str, int]:
    """生成 7 天 × 5 个教室 × 每 30 分钟一条的**合成**监测数据。

    Returns:
        各表写入行数的字典，供初始化脚本打印。

    为什么是 30 分钟粒度：
        7×48×5 = 1680 行，单机毫秒级可查完，既能演示按小时聚合，
        又不会让评估脚本变慢。要压测时把步长改小即可。

    为什么用正弦模拟日变化：
        让温度/CO2 有明显的日内周期，趋势类问题才问得出有意义的结果；
        纯随机数会让「按小时统计变化」变成一条噪声曲线。

    异常剧情（论文里对应「十类故障场景」中的读数漂移）：
        site_id == 3 且时间在最近两天内时，CO2 加 470ppm、PM2.5 加 12，
        同时写入 CALIBRATION_DRIFT 告警。这样归因链路有一个确定的正确答案，
        评估集才可以用「结论是否命中『校准』」做客观判定。
    """
    random.seed(42)  # 固定种子：保证每次重建得到完全相同的数据
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=7)

    # 维表：5 个教室，分布在 3 栋楼、3 个楼层，便于构造 GROUP BY 与 JOIN 问题
    sites = SITE_ROWS
    # 一个教室一台设备，id 与 site_id 对齐，简化 JOIN 条件
    devices = DEVICE_ROWS
    conn.executemany("INSERT INTO site VALUES (?,?,?,?)", sites)
    conn.executemany("INSERT INTO device VALUES (?,?,?,?)", devices)

    rows = []
    rid = 1
    steps = 7 * 48  # 7 天 × 每天 48 个半小时
    for k in range(steps):
        ts = start + timedelta(minutes=30 * k)
        # 日内周期：以早上 6 点为相位偏移，让白天数值高于夜间
        hour_factor = math.sin((ts.hour - 6) / 24 * 2 * math.pi)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")  # ISO 风格，可直接用 date()/strftime() 比较
        for site_id in range(1, 6):
            temperature = 22.5 + 2.0 * hour_factor + random.uniform(-0.6, 0.6)
            humidity = 46.0 + 6.0 * hour_factor + random.uniform(-2.0, 2.0)
            co2 = 560.0 + 70.0 * hour_factor + random.uniform(-25, 25)
            tvoc = 120.0 + 20.0 * hour_factor + random.uniform(-8, 8)
            pm25 = 18.0 + 5.0 * hour_factor + random.uniform(-3, 3)
            # 剧情注入：3 号教室最近两天读数整体抬升，制造可被发现的异常
            if site_id == 3 and ts >= now - timedelta(days=2):
                co2 += 470.0
                pm25 += 12.0
            rows.append((rid, site_id, ts_str, round(temperature, 2),
                         round(humidity, 2), round(co2, 2),
                         round(tvoc, 2), round(pm25, 2)))
            rid += 1
    conn.executemany("INSERT INTO sensor_data VALUES (?,?,?,?,?,?,?,?)", rows)

    # 告警剧情抽成共用函数：合成数据与 UCI 真实数据用同一套标准答案，
    # 这样换数据源后归因题仍然可判定，不用重写评估集。
    alarm_count = _seed_alarms(conn, now)
    conn.commit()
    return {"site": len(sites), "device": len(devices),
            "sensor_data": len(rows), "alarm": alarm_count}


def _seed_alarms(conn: sqlite3.Connection, now: datetime) -> int:
    """写入告警表并返回条数，两种数据源共用。

    CALIBRATION_DRIFT 是归因题的标准答案；CO2_HIGH 是干扰项，用来检验
    Agent 会不会只报现象不报原因；其余四条常规告警让「按楼层统计告警数」
    这类三表 JOIN 题有数据可查。
    """
    alarms = []
    aid = 1
    alarms.append((aid, 3, (now - timedelta(days=1, hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
                   "CALIBRATION_DRIFT", "WARN")); aid += 1
    alarms.append((aid, 3, (now - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S"),
                   "CO2_HIGH", "WARN")); aid += 1
    for device_id, atype in ((1, "SIGNAL_WEAK"), (2, "OFFLINE"), (4, "BATTERY_LOW"), (5, "SIGNAL_WEAK")):
        alarms.append((aid, device_id, (now - timedelta(days=3, hours=device_id)).strftime("%Y-%m-%d %H:%M:%S"),
                       atype, "INFO")); aid += 1
    conn.executemany("INSERT INTO alarm VALUES (?,?,?,?,?)", alarms)
    return len(alarms)


def seed_uci(conn: sqlite3.Connection) -> dict[str, int]:
    """用 UCI Occupancy Detection 公开数据集灌库（v0.4）。

    与 seed_synthetic 的唯一差别是 sensor_data 的数值来源；site / device /
    alarm 三张表完全一致，因此 Schema 检索、SQL 生成、Agent 归因、评估判定
    一行都不用改——这正是开题报告里「数据接入适配器」要验证的性质。

    Returns:
        计数信息，额外带 ``uci_records``（解析出的原始记录数），
        用于论文说明数据规模与抽样比例。
    """
    # 局部导入：默认数据源完全不触碰下载/解压代码，离线可复现性不受影响
    from . import uci_loader

    records = uci_loader.read_records()
    rows, now = uci_loader.build_rows(records)
    conn.executemany("INSERT INTO site VALUES (?,?,?,?)", SITE_ROWS)
    conn.executemany("INSERT INTO device VALUES (?,?,?,?)", DEVICE_ROWS)
    conn.executemany("INSERT INTO sensor_data VALUES (?,?,?,?,?,?,?,?)", rows)
    alarm_count = _seed_alarms(conn, now)
    conn.commit()
    return {"site": len(SITE_ROWS), "device": len(DEVICE_ROWS),
            "sensor_data": len(rows), "alarm": alarm_count,
            "uci_records": len(records)}


def init_database(force: bool = False, source: str = "synthetic") -> dict[str, int]:
    """一键建库，支持两种可切换的数据源。

    Args:
        force: True 会先删除已有库再重建；False 时若已有数据则直接复用。
        source: ``synthetic``（默认，固定种子可复现）或 ``uci``（公开真实数据集）。

    Returns:
        计数信息，含 ``reused`` 与 ``source`` 标记。

    幂等设计：跑多少次都不会重复灌数据，避免评估结果因为数据翻倍而漂移。
    数据源元信息单独写在 data/db/source.json，与库文件同生共死：
    force 重建时一并删除，否则页面会显示上一次的数据源，造成误判。
    """
    from . import uci_loader

    if source not in ("synthetic", "uci"):
        raise ValueError(f"未知数据源：{source}，只能是 synthetic 或 uci")
    ensure_dirs()
    if force and DB_PATH.exists():
        DB_PATH.unlink()
        if uci_loader.SOURCE_META.exists():
            uci_loader.SOURCE_META.unlink()
    conn = connect(readonly=False)  # 只有这里允许写
    try:
        create_schema(conn)
        existing = conn.execute("SELECT COUNT(*) FROM site").fetchone()[0]
        if existing:
            meta = uci_loader.read_meta()
            return {"reused": True, "source": meta["source"], "sensor_data": conn.execute(
                "SELECT COUNT(*) FROM sensor_data").fetchone()[0]}
        counts = seed_uci(conn) if source == "uci" else seed_synthetic(conn)
        uci_loader.write_meta(
            source,
            rows=counts["sensor_data"],
            note=("UCI 真实测量值；时间轴平移、5 站点轮转、注入式已知异常；tvoc/pm25 为 NULL"
                  if source == "uci" else "固定种子 42，含注入式已知异常"),
        )
        counts["reused"] = False
        counts["source"] = source
        return counts  # type: ignore[return-value]
    finally:
        conn.close()


def fetch_all(sql: str, params: tuple = (), readonly: bool = True) -> tuple[list[str], list[tuple]]:
    """便捷查询函数：执行一条 SQL，返回 (列名列表, 行元组列表)。

    面向脚本与调试；主链路走 executor.execute()，因为那边带超时、行数上限
    与错误捕获，这里不重复实现。参数化查询，不做字符串拼接。
    """
    conn = connect(readonly=readonly)
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [tuple(r) for r in cur.fetchall()]
        return cols, rows
    finally:
        conn.close()
