"""把 SQLite 实验库迁移到 MySQL（v0.12）。

为什么要有这一步
----------------
申报表的技术栈写的是 MySQL，但评估结论必须在冻结库上复现，所以项目
最终是「SQLite 跑实验 + MySQL 跑演示/部署」。这份脚本负责把前者搬到
后者，并且**逐表核对行数、抽样核对聚合值**——迁移最怕的不是报错，
而是悄悄少了几行，等到演示时才发现均值对不上。

用法::

    # 1) 先设置密码（三种方式任选其一）
    set MYSQL_PASSWORD=你的root密码          # Windows cmd
    export MYSQL_PASSWORD=你的root密码       # bash
    set MYSQL_URL=mysql+pymysql://root:xxx@127.0.0.1:3306/bishe?charset=utf8mb4

    # 2) 建库 + 建表 + 导数 + 校验
    python scripts/setup_mysql.py

    # 3) 只做校验（不动数据）
    python scripts/setup_mysql.py --verify

    # 4) 删库重建（会清空已有数据）
    python scripts/setup_mysql.py --force

迁移完成后，把 ``DB_BACKEND=mysql`` 写进 .env，演示链路就切到 MySQL；
评估脚本会显式钉死 SQLite，不受影响。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from src.config import DB_PATH, settings  # noqa: E402
from src.db.orm import (Alarm, Device, SensorData, Site, build_mysql_url,  # noqa: E402
                        dispose_engines, get_engine, table_counts)

SCHEMA_MYSQL = ROOT / "src" / "db" / "schema_mysql.sql"

# 建表顺序 / 删表顺序必须与 FK 依赖一致：site <- device <- sensor_data / alarm。
# 删表时反着来，否则 MySQL 会以「被外键引用」为由拒绝 DROP。
DROP_ORDER = ("alarm", "sensor_data", "device", "site")


def _server_url() -> str:
    """构造「不指定库」的连接 URL，用于 CREATE DATABASE。

    为什么需要它：目标库可能还不存在，直接连 ``.../bishe`` 会报
    ``Unknown database``，连建库的机会都没有。
    """
    url = settings.mysql_url or build_mysql_url()
    head, _, _tail = url.rpartition("/")
    return head + "/"


def _split_statements(ddl: str) -> list[str]:
    """按分号切分 DDL 文件。

    这份 DDL 里没有存储过程/触发器（它们内部也带分号，简单切分会切坏），
    所以按 ``;`` 切是安全的。注释行先剔除，避免出现只含注释的「语句」。
    """
    cleaned = "\n".join(
        line for line in ddl.splitlines() if not line.strip().startswith("--")
    )
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def create_database(force: bool = False) -> None:
    """第一步：建库（``--force`` 时先删库）。"""
    db = settings.mysql_database
    url = _server_url()
    print(f"[1/5] 连接 MySQL 服务器 {url.split('@')[-1]}")
    eng = get_engine(url)
    with eng.connect() as conn:
        if force:
            # 删库是破坏性操作，但它是 --force 的显式语义，
            # 且只作用于 settings.mysql_database 这一个库名，不会误伤其他库。
            conn.execute(text(f"DROP DATABASE IF EXISTS `{db}`"))
            print(f"      已删除旧库 {db}")
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{db}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"))
        conn.commit()
        ver = conn.execute(text("SELECT VERSION()")).scalar_one()
    print(f"      数据库 {db} 就绪（MySQL {ver}）")


def create_tables() -> None:
    """第二步：执行 schema_mysql.sql 建表、建索引、建视图。

    用 ``exec_driver_sql`` 而不是 ``text``：DDL 里没有绑定参数，
    走驱动层原样执行更快，也避免 SQLAlchemy 尝试解析 ``--`` 注释。
    """
    ddl = SCHEMA_MYSQL.read_text(encoding="utf-8")
    stmts = _split_statements(ddl)
    eng = get_engine()
    print(f"[2/5] 执行 DDL，共 {len(stmts)} 条语句")
    with eng.connect() as conn:
        for stmt in stmts:
            conn.exec_driver_sql(stmt)
        conn.commit()


def _read_sqlite() -> dict[str, list[tuple]]:
    """从 SQLite 读出四张表的原始行。

    用原生 sqlite3 而不是 ORM：SQLite 侧的 ts 是文本，ORM 的 DateTime
    类型会尝试解析；直接用裸连接读出字符串再自己转 datetime，
    出错点更少、也更容易定位是哪一行格式不对。
    """
    if not DB_PATH.exists():
        raise SystemExit(f"找不到 SQLite 实验库：{DB_PATH}\n请先跑 python -m src.cli init")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        out = {
            "site": [tuple(r) for r in conn.execute(
                "SELECT id, name, building, floor FROM site ORDER BY id")],
            "device": [tuple(r) for r in conn.execute(
                "SELECT id, site_id, model, install_date FROM device ORDER BY id")],
            "sensor_data": [tuple(r) for r in conn.execute(
                "SELECT id, device_id, ts, temperature, humidity, co2, tvoc, pm25 "
                "FROM sensor_data ORDER BY id")],
            "alarm": [tuple(r) for r in conn.execute(
                "SELECT id, device_id, ts, type, level FROM alarm ORDER BY id")],
        }
    finally:
        conn.close()
    return out


def _to_dt(value) -> datetime:
    """把 SQLite 的时间戳文本转成 datetime。

    兼容 ``YYYY-MM-DD HH:MM:SS`` 与带 ``T`` 的 ISO 变体：
    UCI 数据装载后是这个格式，live_feed 写入的也是，统一在这里兜住。
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("T", " "))


def migrate() -> dict[str, int]:
    """第三步：把 SQLite 数据灌进 MySQL。

    Returns:
        各表写入行数。

    为什么用 ORM 而不是 ``INSERT INTO ... SELECT``：
        两个库不在同一个实例上，无法做跨库 SELECT；ORM 还能顺带校验
        字段类型是否与模型定义一致——类型不匹配会在这里就报错，
        而不是等演示时查出来一条脏数据。
    """
    from src.db.orm import write_session

    data = _read_sqlite()
    counts: dict[str, int] = {}
    print("[3/5] 迁移数据")
    with write_session() as session:
        # 先清空：MySQL 侧可能有上一次迁移的残留，直接追加会出现主键冲突。
        # 删除顺序与 FK 相反，逐表提交前关闭外键检查更稳妥。
        session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for name in DROP_ORDER:
            session.execute(text(f"TRUNCATE TABLE {name}"))
        session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

        # 维表先写，事实表后写：外键要求父表先有记录。
        session.add_all(Site(id=r[0], name=r[1], building=r[2], floor=r[3])
                        for r in data["site"])
        session.flush()
        session.add_all(Device(id=r[0], site_id=r[1], model=r[2], install_date=r[3])
                        for r in data["device"])
        session.flush()
        counts["site"] = len(data["site"])
        counts["device"] = len(data["device"])

        # 事实表分批写：UCI 数据集有两万行，一次性 add_all 会让 Session
        # 里堆满对象、内存和提交时间都不可控。500 一批是经验值。
        chunk = 500
        for i in range(0, len(data["sensor_data"]), chunk):
            batch = data["sensor_data"][i:i + chunk]
            session.add_all(SensorData(
                id=r[0], device_id=r[1], ts=_to_dt(r[2]),
                temperature=r[3], humidity=r[4], co2=r[5], tvoc=r[6], pm25=r[7])
                for r in batch)
            session.flush()
        counts["sensor_data"] = len(data["sensor_data"])

        session.add_all(Alarm(id=r[0], device_id=r[1], ts=_to_dt(r[2]),
                              type=r[3], level=r[4]) for r in data["alarm"])
        session.flush()
        counts["alarm"] = len(data["alarm"])
    print("      " + "，".join(f"{k}={v}" for k, v in counts.items()) + " 行")
    return counts


def _sqlite_scalar(sql: str):
    """在 SQLite 侧取一个标量，用于与 MySQL 结果对拍。"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def verify() -> bool:
    """第四步：行数与聚合值双重校验。

    Returns:
        True 表示两边一致。

    为什么不止比行数：行数相等但内容错位（比如 id 与 device 对调）
    是迁移里很典型的静默错误。再比三个聚合值，能覆盖到 JOIN 与
    GROUP BY 是否真的落到同一批数据上。
    """
    print("[4/5] 校验")
    mysql_counts = table_counts()
    sqlite_counts = {
        name: _sqlite_scalar(f"SELECT COUNT(*) FROM {name}")
        for name in ("site", "device", "sensor_data", "alarm")
    }
    ok = True
    for name in ("site", "device", "sensor_data", "alarm"):
        a, b = sqlite_counts[name], mysql_counts.get(name, -1)
        flag = "OK " if a == b else "差异"
        print(f"      {flag} {name:12s} sqlite={a:<6d} mysql={b}")
        ok = ok and a == b

    # 聚合对拍：四舍五入到 2 位再比，避免浮点尾差造成误报
    checks = [
        ("全局 CO2 均值", "SELECT ROUND(AVG(co2),2) FROM sensor_data"),
        ("3 号教室读数条数",
         "SELECT COUNT(*) FROM sensor_data d JOIN device dv ON d.device_id=dv.id "
         "JOIN site s ON dv.site_id=s.id WHERE s.name='3号教室'"),
        ("告警总条数", "SELECT COUNT(*) FROM alarm"),
    ]
    from src.db.orm import read_connection

    with read_connection() as conn:
        for label, sql in checks:
            mine = conn.execute(text(sql)).scalar_one()
            ref = _sqlite_scalar(sql)
            flag = "OK " if abs(float(mine) - float(ref)) < 0.01 else "差异"
            print(f"      {flag} {label}: sqlite={ref} mysql={mine}")
            ok = ok and flag.strip() == "OK"
    return ok


def show_connection_hint() -> None:
    """第五步：打印启用方式。

    不直接改 .env：改配置是用户的选择，脚本只负责给出准确的两行内容，
    避免「脚本偷偷改了后端，结果演示时连的是空库」这种事故。
    """
    print("[5/5] 启用方式（写进项目根目录 .env 后重启服务，或用环境变量临时启动）")
    print("      DB_BACKEND=mysql")
    if settings.mysql_url:
        # 完整 URL 优先级最高：分项打印出来会和实际连接的端口/密码不一致
        # （v0.13.1 在 3307 免密独立实例上实测踩到）。
        print(f"      MYSQL_URL={settings.mysql_url}")
        print("      # 给了完整 URL 就不必再写 MYSQL_HOST/PORT/USER/PASSWORD/DATABASE")
        print(f"      # 本次实际连接：{settings.mysql_url.split('@')[-1]}")
    else:
        print(f"      MYSQL_HOST={settings.mysql_host}")
        print(f"      MYSQL_PORT={settings.mysql_port}")
        print(f"      MYSQL_USER={settings.mysql_user}")
        print(f"      MYSQL_PASSWORD={'（空）' if not settings.mysql_password else '******'}"
              "   # 与本次迁移使用的一致")
        print(f"      MYSQL_DATABASE={settings.mysql_database}")
    print("      # 评估与回归门禁仍强制走 SQLite，不受 DB_BACKEND 影响")


def main() -> None:
    ap = argparse.ArgumentParser(description="SQLite -> MySQL 迁移与校验")
    ap.add_argument("--force", action="store_true", help="删库重建（清空现有数据）")
    ap.add_argument("--verify", action="store_true", help="只做校验，不迁移")
    args = ap.parse_args()

    if not (settings.mysql_url or settings.mysql_password):
        print("缺少 MySQL 密码。请设置环境变量 MYSQL_PASSWORD 或 MYSQL_URL 后重试。")
        print("  Windows: set MYSQL_PASSWORD=你的密码")
        print("  bash   : export MYSQL_PASSWORD=你的密码")
        raise SystemExit(2)

    # 迁移脚本必须自己把后端切到 mysql（v0.13.1 修）。
    # 起因：文档写的是「迁移完成后再把 DB_BACKEND=mysql 写进 .env」，
    # 但 create_tables / migrate / verify 里的 get_engine() 不带 URL，
    # 会按 settings.db_backend 取默认 sqlite —— 于是 MySQL 版 DDL 被
    # 灌进 SQLite，报 `near "=": syntax error`（SQLite 不认 ENGINE=InnoDB）。
    # 这个脚本此前从未端到端跑通，所以坑一直没暴露。
    # 只改进程内的 settings 单例，不碰 .env：切换后端仍是用户的显式选择。
    settings.db_backend = "mysql"

    try:
        if args.verify:
            ok = verify()
            raise SystemExit(0 if ok else 1)
        create_database(force=args.force)
        create_tables()
        migrate()
        ok = verify()
        if ok:
            show_connection_hint()
        else:
            print("校验未通过，请检查上面的差异项后再切换后端。")
        raise SystemExit(0 if ok else 1)
    finally:
        dispose_engines()


if __name__ == "__main__":
    main()
