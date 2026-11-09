"""公开数据源采集器：把网上真实观测值写入**独立的** live.db（v0.8）。

为什么要独立一个库（这是整个设计里最重要的一条）
------------------------------------------------
本项目所有实验结论——144 条评估集、mock 基线 23.6%、真实模型
64.6% ± 0.7%、消融 −7.64pp、失败分布、回归门禁——全部建立在
``data/db/bishe.db`` 这份**冻结快照**上。一旦让实时数据混进这个库：

    1. 数字不可复现，论文第七章全部作废；
    2. 注入式已知异常（3 号教室 CO2 +470 与 CALIBRATION_DRIFT）无法保证；
    3. 「昨天 / 最近一周」类题目随时间漂移，回归门禁会天天报红。

因此本模块**只写 ``data/db/live.db``**，评估链路永远读冻结库，两者互不干扰。
页面与评估脚本默认行为一行都不改；想看实时数据需要显式切换。

数据源与许可
------------
Open-Meteo 的 Weather / Air Quality 两个接口，免费、无需 API Key、
非商业用途，返回当前观测。需在论文里注明来源与许可，并控制调用频率
（本实现一次刷新 5 个坐标 × 2 接口 = 10 次请求，不要每秒调用）。
主页：https://open-meteo.com/

字段映射（诚实边界，不能编造）
------------------------------
    temperature <- current.temperature_2m
    humidity    <- current.relative_humidity_2m
    pm25        <- current.pm2_5
    co2         <- **None**（免费公开源没有 CO2 观测）
    tvoc        <- **None**（同上）

CO2 缺失的后果必须说清：现有多步归因链路靠「CO2 均值最高的教室」找异常，
实时库里这一列为 NULL，所以 **live.db 不支持归因演示**，只适合演示
「真实数据在库里、能查能画图」。要恢复归因，得改用 PM2.5 作判据或接入
带 CO2 的数据源，那属于另一个改动。

五个坐标是桂林市区附近的示意采样点，代表五个虚拟站点，**不是真实设备位置**。
"""
from __future__ import annotations

import json
import sqlite3
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from ..config import DB_DIR, SCHEMA_PATH

LIVE_DB = DB_DIR / "live.db"

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# 桂林市区附近的五个示意坐标；真实项目里应换成设备实际安装位置
SITES: tuple[tuple[int, str, float, float], ...] = (
    (1, "桂林采样点1", 25.2736, 110.2900),
    (2, "桂林采样点2", 25.2800, 110.3000),
    (3, "桂林采样点3", 25.2650, 110.3100),
    (4, "桂林采样点4", 25.2900, 110.2850),
    (5, "桂林采样点5", 25.2700, 110.3200),
)


def _get_json(base: str, params: dict) -> dict:
    """GET 一个公开 JSON 接口。

    本机 HTTPS 有证书吊销检查问题（GitHub 二进制通道就是这样被掐断的），
    这里只对这两个公开只读接口放宽校验；不发送任何凭据。
    """
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "bishe-agent/0.8"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _to_local(iso_utc: str) -> str:
    """把 Open-Meteo 返回的 UTC 时间转成本地（UTC+8）字符串。

    评估库里的 ts 是本地时间，统一时区才能复用 ``date('now','-1 day')``
    这类相对时间写法；不转换会让「今天」的边界错 8 小时。
    """
    dt = datetime.fromisoformat(iso_utc)
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def fetch_current(lat: float, lon: float) -> dict:
    """拉一个坐标的当前观测，返回统一字段字典。

    两次独立请求：气象接口给温湿度，空气质量接口给 PM2.5。
    任一接口失败都会向上抛异常，由 refresh() 决定如何处理，绝不静默填假值。
    """
    weather = _get_json(WEATHER_URL, {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m",
    })
    air = _get_json(AIR_URL, {
        "latitude": lat, "longitude": lon, "current": "pm2_5",
    })
    return {
        "time": weather["current"]["time"],
        "temperature": weather["current"]["temperature_2m"],
        "humidity": weather["current"]["relative_humidity_2m"],
        "pm25": air["current"]["pm2_5"],
    }


def map_row(device_id: int, obs: dict) -> tuple:
    """把一次观测映射成 sensor_data 的一行（不含自增 id）。

    co2 / tvoc 恒为 None：数据源没有这两个指标，宁可缺列也不推算或编造。
    这个纯函数单独抽出是为了能在不联网的情况下做单元测试。
    """
    return (
        device_id,
        _to_local(obs["time"]),
        float(obs["temperature"]),
        float(obs["humidity"]),
        None,   # co2
        None,   # tvoc
        float(obs["pm25"]),
    )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表、灌维度、建去重索引，全部幂等。"""
    conn.executescript(Path(SCHEMA_PATH).read_text(encoding="utf-8"))
    if not conn.execute("SELECT COUNT(*) FROM site").fetchone()[0]:
        conn.executemany(
            "INSERT INTO site VALUES (?,?,?,?)",
            [(i, name, "公开数据源", 0) for i, name, _, _ in SITES],
        )
        conn.executemany(
            "INSERT INTO device VALUES (?,?,?,?)",
            [(i, i, "OPEN-METEO", "2026-11-09") for i, _, _, _ in SITES],
        )
    # 同一设备同一时刻只保留一条，重复刷新不会把数据翻倍
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_live_device_ts "
                 "ON sensor_data(device_id, ts)")
    conn.commit()


def refresh() -> dict:
    """执行一次采集并写入 live.db，返回统计信息。

    幂等：同一时刻重复刷新靠唯一索引 + INSERT OR IGNORE 去重。
    单站失败不影响其它站点，失败原因会原样带回，便于排查网络问题。
    """
    LIVE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LIVE_DB))
    inserted = 0
    errors: list[str] = []
    samples: list[dict] = []
    try:
        _ensure_schema(conn)
        for site_id, name, lat, lon in SITES:
            try:
                obs = fetch_current(lat, lon)
                row = map_row(site_id, obs)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO sensor_data "
                    "(device_id, ts, temperature, humidity, co2, tvoc, pm25) "
                    "VALUES (?,?,?,?,?,?,?)", row)
                inserted += cur.rowcount
                samples.append({"site": name, "ts": row[1],
                                "temperature": row[2], "humidity": row[3],
                                "pm25": row[6]})
            except Exception as exc:  # 单点失败不拖垮整批
                errors.append(f"{name}: {type(exc).__name__} {str(exc)[:80]}")
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0]
        latest = conn.execute("SELECT MAX(ts) FROM sensor_data").fetchone()[0]
    finally:
        conn.close()
    return {
        "db": str(LIVE_DB), "inserted": inserted, "total_rows": total,
        "latest_ts": latest, "sites": len(SITES),
        "errors": errors, "samples": samples,
        "note": "co2/tvoc 为 NULL；live.db 不参与任何评估与回归门禁。",
    }
