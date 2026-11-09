-- MySQL 版 DDL（v0.12，对齐申报表技术栈「MySQL 8.0」）
--
-- 与 schema.sql 的关系
-- --------------------
-- schema.sql 是 SQLite 版，字段类型按 SQLite 的动态类型写（TEXT / REAL）；
-- 本文件是同一套逻辑 Schema 的 MySQL 表示，差异只有三处，都是方言决定的：
--
--   1. TEXT → VARCHAR(n)：MySQL 建索引/外键要求字符串列有明确长度，
--      TEXT 不能直接做索引键，所以教室名、型号、告警类型都给了长度上限。
--   2. ts 用 DATETIME：SQLite 里时间戳存成文本，MySQL 用原生 DATETIME，
--      才能走索引范围扫描、才能用 DATE(ts) 做日聚合。
--   3. 索引与外键显式命名：MySQL 的约束名是库级的，重名会报 1061，
--      显式命名后重建脚本不会互相踩。
--
-- 引擎统一 InnoDB + utf8mb4：前者要事务与外键，后者要支持中文教室名。
-- 所有语句都带 IF NOT EXISTS，脚本可重复执行。

CREATE TABLE IF NOT EXISTS site (
    id       INT         NOT NULL,
    name     VARCHAR(64) NOT NULL,
    building VARCHAR(32) NOT NULL,
    floor    INT         NOT NULL,
    PRIMARY KEY (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS device (
    id           INT         NOT NULL,
    site_id      INT         NOT NULL,
    model        VARCHAR(32) NOT NULL,
    -- 保留 VARCHAR(10) 而不是 DATE：与 ORM 模型、SQLite 侧的值格式一致，
    -- 迁移时不需要做类型转换，少一处可能出错的环节。
    install_date VARCHAR(10) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_device_site (site_id),
    CONSTRAINT fk_device_site FOREIGN KEY (site_id) REFERENCES site (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS sensor_data (
    id          INT      NOT NULL,
    device_id   INT      NOT NULL,
    ts          DATETIME NOT NULL,
    temperature DOUBLE   NULL,
    humidity    DOUBLE   NULL,
    co2         DOUBLE   NULL,
    tvoc        DOUBLE   NULL,
    pm25        DOUBLE   NULL,
    PRIMARY KEY (id),
    KEY idx_sensor_device_ts (device_id, ts),
    KEY idx_sensor_ts (ts),
    CONSTRAINT fk_sensor_device FOREIGN KEY (device_id) REFERENCES device (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS alarm (
    id        INT         NOT NULL,
    device_id INT         NOT NULL,
    ts        DATETIME    NOT NULL,
    type      VARCHAR(32) NOT NULL,
    level     VARCHAR(16) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_alarm_device (device_id),
    KEY idx_alarm_type (type),
    CONSTRAINT fk_alarm_device FOREIGN KEY (device_id) REFERENCES device (id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 聚合视图：与 SQLite 版口径完全一致（同一份 GROUP BY），
-- 这样「查视图」类评估题在两个后端返回相同数字。
CREATE OR REPLACE VIEW v_site_air_quality AS
SELECT s.id                AS site_id,
       s.name              AS site_name,
       s.building          AS building,
       s.floor             AS floor,
       AVG(d.co2)          AS avg_co2,
       AVG(d.pm25)         AS avg_pm25,
       AVG(d.temperature)  AS avg_temperature,
       COUNT(*)            AS sample_count
FROM sensor_data d
         JOIN device dv ON d.device_id = dv.id
         JOIN site s ON dv.site_id = s.id
GROUP BY s.id, s.name, s.building, s.floor;
