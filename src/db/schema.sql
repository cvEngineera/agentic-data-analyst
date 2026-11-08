-- 数据层 DDL（对应开题报告「4.1（1）数据层建设」）
--
-- 结构：两张维表（site / device）+ 一张事实表（sensor_data）+ 一张告警表（alarm）。
-- 这样设计是为了让评估集能自然产生三表 JOIN 问题：
--     sensor_data -> device -> site，以及 alarm -> device -> site。
-- 视图 v_site_air_quality 用于常用口径复用；索引用于压测与查询延迟测量。
--
-- 所有语句都带 IF NOT EXISTS，建库脚本可重复执行而不报错。
-- 换成 UCI 公开数据集时保持这四张表的结构不变，只改 db/core.py::seed()。

CREATE TABLE IF NOT EXISTS site (
    id       INTEGER PRIMARY KEY,
    name     TEXT    NOT NULL,
    building TEXT    NOT NULL,
    floor    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS device (
    id           INTEGER PRIMARY KEY,
    site_id      INTEGER NOT NULL REFERENCES site(id),
    model        TEXT    NOT NULL,
    install_date TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sensor_data (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device(id),
    ts          TEXT    NOT NULL,
    temperature REAL,
    humidity    REAL,
    co2         REAL,
    tvoc        REAL,
    pm25        REAL
);

CREATE TABLE IF NOT EXISTS alarm (
    id        INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES device(id),
    ts        TEXT    NOT NULL,
    type      TEXT    NOT NULL,
    level     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensor_device_ts ON sensor_data(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_sensor_ts ON sensor_data(ts);
CREATE INDEX IF NOT EXISTS idx_alarm_device ON alarm(device_id);
CREATE INDEX IF NOT EXISTS idx_alarm_type ON alarm(type);

CREATE VIEW IF NOT EXISTS v_site_air_quality AS
SELECT s.id          AS site_id,
       s.name        AS site_name,
       s.building,
       s.floor,
       AVG(d.co2)    AS avg_co2,
       AVG(d.pm25)   AS avg_pm25,
       AVG(d.temperature) AS avg_temperature,
       COUNT(*)      AS sample_count
FROM sensor_data d
JOIN device dv ON d.device_id = dv.id
JOIN site s ON dv.site_id = s.id
GROUP BY s.id, s.name, s.building, s.floor;
