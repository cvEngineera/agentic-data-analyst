"""UCI 公开数据集接入适配器（v0.4，对应开题报告「4.1（1）数据层建设」）。

为什么需要这一层
----------------
v0.1–v0.3 用的是固定种子的合成数据：链路能跑、能演示、能复现，但论文里
「数据来源」一章经不起追问。本模块把 UCI 公开数据集真正接进系统，用来证明
开题报告里写的「数据接入适配器」不是空话——换数据源时四张表结构、六个上层
模块、评估判定口径全部不变，只替换读数与字段映射。

选的数据集
----------
UCI Occupancy Detection（id=357），2015 年真实办公室环境测量，含
Temperature / Humidity / CO2 / Light，与 sensor_data 的温湿度、CO2 三列
直接对应。三个 txt 合计约 2 万条，zip 约 328 KB，无需申请、免费下载。
主页：https://archive.ics.uci.edu/dataset/357/occupancy+detection

三处必须诚实说明的处理（论文「数据集与预处理」一节要照抄）
--------------------------------------------------------
1. **表头缺列**：文件首行只有 7 个列名，实际数据有 8 列（首列是序号），
   用 csv.DictReader 会整体错位，因此按位置解析并跳过首行。
2. **时间轴平移**：原始数据是 2015 年 2 月，而评估集与演示都用「昨天 /
   最近一周」这类相对时间。这里保留数值与采样顺序，把时间戳等距映射到
   最近 7 天，只改时间标签、不改测量值。
3. **多站点构造 + 注入式异常**：UCI 是单房间数据，为保留「哪个教室最差」
   这类跨站点问题，按时间片轮转分配到 5 个虚拟教室；并在 3 号教室最近
   两天注入 CO2 偏移与 CALIBRATION_DRIFT 告警。这是**注入式已知异常**，
   目的是给多步归因链路一个客观标准答案，不能当作真实故障来表述。

tvoc / pm25 两列
----------------
UCI 数据集中没有这两个指标，**保持 NULL，不做任何推算或编造**。因此涉及
这两列的评估题只在合成数据源上有效；UCI 模式用 ``scripts/check_uci.py``
单独验证核心链路，不混入正式评估集的准确率统计——避免用「跳过部分题」
的方式把数字做好看。

零第三方依赖：下载、解压、解析全部用标准库完成，不卡在装包上。
"""
from __future__ import annotations

import csv
import io
import json
import ssl
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from ..config import DATA_DIR, DB_DIR, ensure_dirs

# ---- 数据集常量 ----------------------------------------------------------
DATASET_ID = 357
DATASET_NAME = "UCI Occupancy Detection (id=357)"
DATASET_URL = "https://archive.ics.uci.edu/static/public/357/occupancy+detection.zip"
RAW_DIR = DATA_DIR / "raw"
ZIP_PATH = RAW_DIR / "occupancy_detection.zip"
SOURCE_META = DB_DIR / "source.json"

# zip 里三个 txt 分别是训练段与两段测试段，按时间拼接后是连续序列
MEMBERS = ("datatraining.txt", "datatest.txt", "datatest2.txt")

# 数据行共 8 列：序号, 时间, 温度, 湿度, 光照, CO2, 湿度比, 是否有人。
# 表头只有 7 个名字（漏了序号列），所以必须用下标而不是列名。
COL = {
    "date": 1,
    "temperature": 2,
    "humidity": 3,
    "light": 4,
    "co2": 5,
}

# 与合成数据保持一致的规模：7 天 × 每天 48 个半小时 × 5 个教室 = 1680 行。
# 规模不变是为了让查询耗时、评估结果、回归门禁在两种数据源之间可比。
DAYS = 7
PER_DAY = 48
SITES = 5
EXPECTED_ROWS = DAYS * PER_DAY * SITES


def _ssl_context() -> ssl.SSLContext:
    """构造宽松的 SSL 上下文。

    本机访问部分 HTTPS 站点会遇到证书吊销检查失败（GitHub 二进制通道就是这样
    被掐断的）。这里只对 UCI 这个公开只读下载使用，关闭主机名校验与证书校验；
    不用于任何携带凭据的请求。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def ensure_zip() -> Path:
    """确保数据集 zip 已下载到本地，返回路径。

    已存在且大小合理（>10KB）就直接复用，避免每次建库都联网；
    下载失败会抛出原始异常，由调用方决定是否回退到合成数据源。
    """
    ensure_dirs()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists() and ZIP_PATH.stat().st_size > 10_000:
        return ZIP_PATH
    req = urllib.request.Request(DATASET_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90, context=_ssl_context()) as resp:
        payload = resp.read()
    ZIP_PATH.write_bytes(payload)
    return ZIP_PATH


def read_records() -> list[dict]:
    """解析三个 txt，返回按时间升序排列的记录列表。

    每条记录保留 date / temperature / humidity / co2 / light 五个字段。
    无法转成浮点数的行（空值、注释行）直接跳过，不让脏数据污染实验。
    """
    zpath = ensure_zip()
    records: list[dict] = []
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        for member in MEMBERS:
            if member not in names:
                continue
            # errors="ignore" 是为了容忍个别非 UTF-8 字节，不影响数值列
            text = io.TextIOWrapper(zf.open(member), encoding="utf-8", errors="ignore")
            reader = csv.reader(text)
            next(reader, None)  # 首行是错位的表头，直接丢弃
            for row in reader:
                if len(row) <= COL["co2"]:
                    continue
                try:
                    records.append({
                        "date": row[COL["date"]].strip().strip('"'),
                        "temperature": float(row[COL["temperature"]]),
                        "humidity": float(row[COL["humidity"]]),
                        "light": float(row[COL["light"]]),
                        "co2": float(row[COL["co2"]]),
                    })
                except ValueError:
                    continue
    records.sort(key=lambda r: r["date"])
    return records


def resample(records: list[dict], n: int) -> list[dict]:
    """等间隔抽样到 n 条，保持时间顺序。

    为什么不随机抽样：随机会打乱日内周期，趋势类问题「按小时统计变化」
    就会变成噪声曲线；等间隔抽样能保留原有起伏形态。
    """
    if not records:
        return []
    if len(records) <= n:
        return list(records)
    last = len(records) - 1
    return [records[round(i * last / (n - 1))] for i in range(n)]


def build_rows(records: list[dict], now: datetime | None = None) -> tuple[list[tuple], datetime]:
    """把 UCI 记录映射成 sensor_data 的行，返回 (行列表, 基准时间)。

    映射规则：
        - 时间戳：原始 2015 年日期 -> 最近 7 天等距时间轴（只换标签，不换数值）
        - 站点：按记录序号轮转分给 5 个虚拟教室，使跨站点比较题有数据
        - 温度 / 湿度 / CO2：直接取真实测量值
        - tvoc / pm25：数据集中无对应指标，写 NULL
        - 注入式异常：3 号教室最近两天 CO2 +470ppm，与合成数据剧情一致

    Returns:
        ``(rows, now)``；rows 每项为 schema.sql 里 sensor_data 的 8 列。
        now 一并返回，是为了让告警时间落在同一时间轴上。
    """
    now = now or datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=DAYS)
    step_minutes = 24 * 60 // PER_DAY  # 48 个半小时 -> 30 分钟
    picked = resample(records, EXPECTED_ROWS)

    rows: list[tuple] = []
    for k, rec in enumerate(picked):
        site_id = k % SITES + 1
        step = k // SITES
        ts = start + timedelta(minutes=step_minutes * step)
        co2 = rec["co2"]
        if site_id == 3 and ts >= now - timedelta(days=2):
            co2 += 470.0  # 注入式已知异常，给归因链路提供客观答案
        rows.append((
            k + 1,
            site_id,
            ts.strftime("%Y-%m-%d %H:%M:%S"),
            round(rec["temperature"], 2),
            round(rec["humidity"], 2),
            round(co2, 2),
            None,  # tvoc：UCI 无此指标，不编造
            None,  # pm25：UCI 无此指标，不编造
        ))
    return rows, now


def write_meta(source: str, *, rows: int, note: str = "") -> None:
    """把当前数据源写进 data/db/source.json，供页面 /api/status 展示。

    这是运行产物，不入库（.gitignore 已忽略），换库即重写。
    """
    ensure_dirs()
    payload = {
        "source": source,
        "dataset": DATASET_NAME if source == "uci" else "内置可复现合成数据（种子 42）",
        "rows": rows,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
    }
    SOURCE_META.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_meta() -> dict:
    """读取数据源元信息；文件缺失或损坏时回退到 synthetic，保证接口不崩。"""
    fallback = {
        "source": "synthetic",
        "dataset": "内置可复现合成数据（种子 42）",
        "rows": 0,
        "built_at": "",
        "note": "",
    }
    if not SOURCE_META.exists():
        return fallback
    try:
        return json.loads(SOURCE_META.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback
