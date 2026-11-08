"""数据层（研究模块①）：DDL、连接与合成数据。

- schema.sql：四张业务表 + 索引 + 聚合视图，是 Schema 检索与校验的共同依据。
- core.py：只读连接、表结构自省、建库与固定种子的合成数据。

对外主要接口：connect() / introspect() / init_database() / ALLOWED_TABLES。
"""
