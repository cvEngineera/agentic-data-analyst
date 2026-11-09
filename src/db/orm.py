"""SQLAlchemy 数据访问层（v0.12，对齐申报表技术栈）。

为什么要有这一层
----------------
v0.11 之前数据访问是裸 ``sqlite3``：查询写 ``conn.execute("SELECT ...")``，
建库用 ``executescript``。它跑得通，但有两个硬伤：

1. **换库要改代码**。申报表写的是 MySQL，真到部署阶段，字段类型、自增、
   日期函数全都得重写一遍，而这件事拖到答辩前做风险极高。
2. **没有模型层**。表结构只存在于 schema.sql 里，字段类型与外键关系在
   Python 侧不可见；加一个字段要 grep 全仓库找哪些 SQL 受影响。

这一版引入 SQLAlchemy 2.0 声明式 ORM：

- ``Site / Device / SensorData / Alarm`` 四个模型与 ``schema.sql`` 一一对应，
  字段类型、外键、索引都写在模型里，代码即文档；
- 引擎只认 URL：``sqlite:///...`` 与 ``mysql+pymysql://...`` 是同一套代码；
- 只读语义在两个后端对齐：SQLite 用 ``mode=ro`` + ``PRAGMA query_only``，
  MySQL 用 ``SET SESSION TRANSACTION READ ONLY``，都不靠「事后检查 SQL」。

为什么默认仍然走 SQLite
-----------------------
评估结论（144 条用例、64.6% ± 0.7%）是在 ``data/db/bishe.db`` 上冻结的。
把默认库换成 MySQL，等于让「同一个脚本在不同机器上跑出不同数字」，
实验就不可复现了。所以分工是：

    评估链路 → 永远 SQLite（evaluation/runner.py 里显式钉死）
    演示/部署 → MySQL（DB_BACKEND=mysql，数据由 scripts/setup_mysql.py 迁入）

两者共享同一份逻辑 Schema 与同一份数据，迁移脚本会逐表比对行数做校验。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from urllib.parse import quote_plus

from sqlalchemy import (DateTime, Float, ForeignKey, Index, Integer, String,
                        create_engine, inspect, text)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            relationship, sessionmaker)

from ..config import settings


class Base(DeclarativeBase):
    """所有模型的基类。

    SQLAlchemy 2.0 用 ``DeclarativeBase`` 子类代替旧的 ``declarative_base()``，
    好处是类型检查器能推断出 ``Mapped[int]`` 这类注解对应的列类型。
    """


class Site(Base):
    """教室/站点维表。

    对应 schema.sql 的 ``site``。命名用 Site 而不是 Classroom，是为了跟
    评估集里的英文表名保持一致——模型类名和真实表名不一致时，
    排查「生成 SQL 引用了哪张表」会多一层心智负担。
    """

    __tablename__ = "site"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 教室名固定是「N号教室」这种短字符串，给 64 足够；字符串列在 MySQL 上
    # 必须是 VARCHAR 才能建索引，所以不能用 TEXT。
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    building: Mapped[str] = mapped_column(String(32), nullable=False)
    floor: Mapped[int] = mapped_column(Integer, nullable=False)

    # 一对多：一个教室挂多台设备。当前数据集是一对一，但关系按一对多建模，
    # 这样以后加设备不用改表结构。
    devices: Mapped[list["Device"]] = relationship(back_populates="site")

    def __repr__(self) -> str:  # 调试时打印对象可读，不影响查询
        return f"<Site {self.id} {self.name}>"


class Device(Base):
    """设备维表，通过 site_id 关联教室。"""

    __tablename__ = "device"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), nullable=False)
    model: Mapped[str] = mapped_column(String(32), nullable=False)
    # 安装日期保持字符串：SQLite 里本来就是 'YYYY-MM-DD'，转 date 类型
    # 只会让「跨库迁移时格式不一致」多一个坑，而业务上从不拿它做日期运算。
    install_date: Mapped[str] = mapped_column(String(10), nullable=False)

    site: Mapped["Site"] = relationship(back_populates="devices")
    readings: Mapped[list["SensorData"]] = relationship(back_populates="device")


class SensorData(Base):
    """监测事实表：一条读数对应一台设备一个时刻。

    ``ts`` 用 ``DateTime`` 而不是字符串：MySQL 里它是 DATETIME，能直接
    走索引范围扫描，也能用 ``DATE(ts)`` 做日聚合；SQLite 侧仍由
    sqlite3 原生连接写入 'YYYY-MM-DD HH:MM:SS' 文本，两种表示在
    SQLAlchemy 的 DateTime 类型下都能解析。
    """

    __tablename__ = "sensor_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    ts: Mapped[object] = mapped_column(DateTime, nullable=False)
    # 五项指标都允许 NULL：UCI 数据集只有温度/湿度/CO2/光照，tvoc 与 pm25
    # 没有对应列，硬填 0 会污染均值类问题，所以留空。
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    co2: Mapped[float | None] = mapped_column(Float, nullable=True)
    tvoc: Mapped[float | None] = mapped_column(Float, nullable=True)
    pm25: Mapped[float | None] = mapped_column(Float, nullable=True)

    device: Mapped["Device"] = relationship(back_populates="readings")

    # 索引与 schema.sql 对齐：(device_id, ts) 支撑「某设备某时间段的读数」，
    # ts 单列支撑「按时间全局聚合」。
    __table_args__ = (
        Index("idx_sensor_device_ts", "device_id", "ts"),
        Index("idx_sensor_ts", "ts"),
    )


class Alarm(Base):
    """告警表：归因链路的关键证据源。"""

    __tablename__ = "alarm"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("device.id"), nullable=False)
    ts: Mapped[object] = mapped_column(DateTime, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        Index("idx_alarm_device", "device_id"),
        Index("idx_alarm_type", "type"),
    )


# ── 引擎与会话 ────────────────────────────────────────────────────────────

# 引擎按 URL 缓存：SQLAlchemy 的 Engine 自带连接池，重复 create_engine
# 会额外开池子，长驻进程里是明确的资源泄漏点。
_ENGINES: dict[str, Engine] = {}


def build_mysql_url() -> str:
    """由 settings 里的分项配置拼出 MySQL URL。

    密码可能含 ``@``、``/``、``#`` 等字符，必须 ``quote_plus`` 转义，
    否则 URL 解析会把密码截断成主机名的一部分——这是踩过的坑。

    Returns:
        ``mysql+pymysql://user:pass@host:port/db?charset=utf8mb4``
    """
    user = quote_plus(settings.mysql_user)
    pwd = quote_plus(settings.mysql_password)
    return (f"mysql+pymysql://{user}:{pwd}@{settings.mysql_host}:"
            f"{settings.mysql_port}/{settings.mysql_database}?charset=utf8mb4")


def database_url(backend: str | None = None) -> str:
    """返回当前后端的 SQLAlchemy URL。

    Args:
        backend: ``sqlite`` / ``mysql``；None 时取 settings.db_backend。

    说明：
        SQLite 分支直接复用 config.DB_PATH，保证「评估用的库」和
        「ORM 打开的库」是同一个文件，不会出现两份数据。
    """
    be = backend or settings.db_backend
    if be == "mysql":
        # 允许用 MYSQL_URL 整体覆盖，方便连远端或容器里的实例
        return settings.mysql_url or build_mysql_url()
    return f"sqlite:///{settings_db_path_str()}"


def settings_db_path_str() -> str:
    """把 Path 转成 SQLAlchemy 能吃的字符串路径。

    延迟 import 是为了避免 config 与 db 包之间的循环依赖
    （config 被 db/core 引用，db/orm 又引用 config）。
    """
    from ..config import DB_PATH

    return DB_PATH.as_posix()


def get_engine(url: str | None = None, echo: bool = False) -> Engine:
    """取（或创建）一个引擎。

    Args:
        url: 目标库 URL；None 表示按 settings.db_backend 决定。
        echo: 是否打印 SQL。写脚本排查方言差异时很有用，默认关闭。

    连接池参数说明：
        - ``pool_pre_ping=True``：MySQL 默认 8 小时断连，长驻进程里
          不探活就会拿到死连接然后报 "MySQL server has gone away"。
        - ``pool_recycle=1800``：半小时回收一次，比 wait_timeout 短。
        - SQLite 不需要池参数，传了也无害（NullPool 之外会被忽略）。
    """
    target = url or database_url()
    cached = _ENGINES.get(target)
    if cached is not None and not echo:
        return cached
    if target.startswith("mysql"):
        eng = create_engine(target, pool_pre_ping=True, pool_recycle=1800,
                            future=True, echo=echo)
    else:
        # check_same_thread=False：Web 服务是多线程的，SQLite 连接默认
        # 绑线程，不放开会报 "SQLite objects created in a thread..."。
        eng = create_engine(target, future=True, echo=echo,
                            connect_args={"check_same_thread": False})
    if not echo:
        _ENGINES[target] = eng
    return eng


def dispose_engines() -> None:
    """关闭全部缓存的引擎。

    测试里切换后端或删除库文件前调用，否则 Windows 上会出现
    「文件被占用无法删除」。
    """
    for eng in _ENGINES.values():
        eng.dispose()
    _ENGINES.clear()


@contextmanager
def read_connection(timeout: int | None = None) -> Iterator:
    """只读连接上下文（MySQL/SQLite 通用）。

    MySQL 的只读是**会话级**的：``SET SESSION TRANSACTION READ ONLY``
    之后，这个连接上的写操作会被服务端直接拒绝（errno 1792），
    与 SQLite 的 ``query_only`` 语义一致。

    另外设 ``max_execution_time``，等价于 SQLite 那边的
    ``set_progress_handler`` 看门狗——两个后端都要有「一条失控 SQL
    不能拖死整个服务」的兜底。
    """
    eng = get_engine()
    conn = eng.connect()
    try:
        if eng.dialect.name == "mysql":
            conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
            ms = int((timeout or settings.query_timeout) * 1000)
            conn.execute(text(f"SET SESSION max_execution_time = {ms}"))
        yield conn
    finally:
        conn.close()


@contextmanager
def write_session() -> Iterator[Session]:
    """可写会话上下文，只给建库/迁移脚本用。

    业务链路**不允许**走这里：只读是安全设计的第一道防线，
    提供一个随手可用的可写会话等于给自己留后门。
    """
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── 自省 ──────────────────────────────────────────────────────────────────


def reflect_schema(url: str | None = None) -> dict[str, list[str]]:
    """用 SQLAlchemy Inspector 读出 ``{表名: [列名]}``。

    它与 ``db/core.introspect()`` 返回同样的结构，但走的是 SQLAlchemy
    的反射接口，因此对 MySQL 也成立（core.introspect 读的是
    sqlite_master，只能在 SQLite 上跑）。

    Returns:
        按表名排序的字典；视图也会被包含，因为评估集允许直接查聚合视图。
    """
    eng = get_engine(url)
    insp = inspect(eng)
    result: dict[str, list[str]] = {}
    for name in sorted(insp.get_table_names() + insp.get_view_names()):
        result[name] = [c["name"] for c in insp.get_columns(name)]
    return result


def table_counts(url: str | None = None) -> dict[str, int]:
    """统计四张表的行数，用于迁移后的一致性校验与页面状态栏展示。"""
    eng = get_engine(url)
    counts: dict[str, int] = {}
    with eng.connect() as conn:
        for name in ("site", "device", "sensor_data", "alarm"):
            try:
                counts[name] = int(conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one())
            except Exception:
                counts[name] = -1  # 表不存在时给 -1，而不是让整个状态接口报错
    return counts
