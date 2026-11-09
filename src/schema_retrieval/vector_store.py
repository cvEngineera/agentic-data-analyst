"""Schema 向量索引：pgvector 实现（v0.12，对齐申报表技术栈）。

放在哪一层
----------
Schema 检索（``catalog.retrieve``）目前是「关键词 + 数据字典 + 外键一跳」。
v0.7 加过一个零依赖的语义打分（``semantic.py``），但它是**每次都重新算**的，
表少的时候无所谓，表多起来就该把向量存下来了——这正是向量库的职责。

本模块把「五张表的文档」编码成定长向量，存进 PostgreSQL 的 pgvector，
检索时用 ``<=>``（余弦距离）取 top-k。它不改变关键词打分的逻辑，
而是作为一个可插拔的 provider：开关打开就叠加，关掉就回到 v0.7 的口径。

诚实边界（论文必须写清楚）
--------------------------
embedding **不是**神经网络模型，而是字符 n-gram + TF-IDF 哈希到 256 维。
原因：DeepSeek 当前没有 embedding 接口，本地跑 BGE 需要下几百 MB 模型，
答辩机不一定有网。用哈希向量能保证：

    同样的输入 → 同样的向量（可复现）
    不需要任何模型文件或 API 调用（离线可跑）
    pgvector 的存储、索引、距离算子是真的在用（不是假装）

代价是语义泛化能力弱于 BGE，这一点在论文「局限性」里要如实写，
不能说成「已接入语义 embedding 模型」。

为什么维度是 256
----------------
哈希向量的冲突率随维度下降而上升。256 维在「5 张表 × 约 40 个字段名」
这个量级上冲突可忽略，同时索引体积小、建索引导入快。换成真实
embedding 时只改 ``EMBED_DIM`` 与 ``_embed`` 两处。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from ..config import settings

# 向量维度：见模块说明。改了它必须重建表（pgvector 的 vector(n) 是强类型）。
EMBED_DIM = 256


def _tokens(text: str) -> list[str]:
    """切 token：英文/数字按词，中文按字符 bigram。

    与 ``semantic.py`` 保持同一套切法，这样「向量检索」和「语义打分」
    对同一个问题给出的相对排序是接近的，不会出现两套口径互相矛盾。
    """
    s = re.sub(r"\s+", " ", text.lower()).strip()
    words = re.findall(r"[a-z0-9_]+", s)
    han = re.sub(r"[^一-鿿]", "", s)
    words.extend(han[i:i + 2] for i in range(len(han) - 1))
    return words or [s]


def _embed(text: str) -> list[float]:
    """把文本编码成 ``EMBED_DIM`` 维的单位向量。

    做法是「哈希技巧（hashing trick）」：
        1. 统计 token 频次，用 ``1+log(tf)`` 抑制高频词；
        2. 每个 token 取 md5 前 4 字节决定落到哪一维，第 5 字节决定符号；
        3. L2 归一化后，点积即余弦相似度。

    为什么要符号位：只用正数会让所有向量挤在第一象限，任意两段文本的
    余弦都偏高，区分度差。带符号的随机投影能保持近似正交。
    """
    dim = [0.0] * EMBED_DIM
    for token, count in Counter(_tokens(text)).items():
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % EMBED_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        dim[idx] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in dim))
    if not norm:
        return dim
    return [v / norm for v in dim]


def table_documents() -> dict[str, str]:
    """生成每张表的「文档」文本（表名 + 注释 + 别名 + 字段名与说明）。

    直接复用 ``semantic._table_docs()``，避免两处文档口径不一致——
    如果一边把「二氧化碳」写进别名、另一边没写，向量检索和关键词打分
    就会对同一个问题给出相反的判断。
    """
    from .semantic import _table_docs

    return _table_docs()


# ── pgvector 存储 ─────────────────────────────────────────────────────────


def _connect():
    """建立 psycopg 连接（autocommit，方便执行 CREATE EXTENSION）。

    只在真正使用向量库时调用；没装 PG 的环境不会因为 import 就报错，
    因为 psycopg 是延迟 import 的。

    ``connect_timeout=2``：本机 PG 应该毫秒级握手，2 秒还连不上说明根本
    没起；留 5 秒会让「开了开关但没起容器」的每次检索都白等 5 秒（实测
    Windows 上被防火墙丢包时确实是等满超时才返回）。
    """
    import psycopg

    return psycopg.connect(settings.pgvector_url, autocommit=True, connect_timeout=2)


# 可用性探测结果缓存：连不上时每次检索都要付出一次连接超时，
# 而调用方（catalog._vector_scores）是按问题缓存的，换问题就会再探一次。
# 缓存 60 秒，既不会在容器起来后长期误判，也不会让降级路径反复卡住。
_AVAIL: dict[str, float | bool | None] = {"ok": None, "at": 0.0}
_AVAIL_TTL = 60.0


def available() -> bool:
    """探测 pgvector 是否可用（能连上并且扩展已启用）。

    返回 False 不算错误：``catalog`` 会退回到内存打分，主链路继续可用。
    这个函数被状态接口调用，所以必须**快**且不抛异常。

    结果带 60 秒缓存：容器晚起一分钟内被判定不可用，是**可接受**的代价，
    换掉的是「每个新问题都卡一次连接超时」——降级路径不该比主路径还慢。
    """
    import time

    now = time.time()
    if _AVAIL["ok"] is not None and now - float(_AVAIL["at"]) < _AVAIL_TTL:
        return bool(_AVAIL["ok"])

    ok = False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ok = cur.fetchone() is not None
    except Exception:
        ok = False
    finally:
        _AVAIL["ok"] = ok
        _AVAIL["at"] = now
    return ok


def ensure_schema() -> None:
    """建扩展、建表、建 HNSW 索引（幂等）。

    索引类型选 HNSW 而不是 IVFFlat：IVFFlat 需要先有数据才有意义
    （要训练聚类中心），而本项目只有 5 行向量，HNSW 在小数据集上
    建索引的开销可以忽略，行为也更稳定。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS schema_vectors (
                table_name VARCHAR(64) PRIMARY KEY,
                doc        TEXT NOT NULL,
                embedding  vector({EMBED_DIM}) NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_schema_vectors_embedding
            ON schema_vectors USING hnsw (embedding vector_cosine_ops)
        """)


def index_schema(force: bool = False) -> dict[str, int]:
    """把当前 Schema 的文档写入 pgvector。

    Args:
        force: True 时先清空表再重建，用于 Schema 变更后刷新。

    Returns:
        ``{"indexed": n}``。

    用 ``ON CONFLICT (table_name) DO UPDATE`` 而不是先删后插：
    这样即使中途失败，表里仍是「旧向量 + 部分新向量」而不是空表，
    检索不会因为一次失败就完全失效。
    """
    docs = table_documents()
    ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        if force:
            cur.execute("TRUNCATE schema_vectors")
        for table, doc in docs.items():
            vec = _embed(doc)
            # 向量字面量形如 [0.1,0.2,...]，用 ::vector 显式转型，
            # 让 psycopg 不必猜测参数类型（它不认识 pgvector 的自定义类型）。
            literal = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
            cur.execute(
                """
                INSERT INTO schema_vectors (table_name, doc, embedding, updated_at)
                VALUES (%s, %s, %s::vector, NOW())
                ON CONFLICT (table_name)
                DO UPDATE SET doc = EXCLUDED.doc,
                              embedding = EXCLUDED.embedding,
                              updated_at = NOW()
                """,
                (table, doc, literal),
            )
    return {"indexed": len(docs)}


def search(question: str, top_k: int = 4) -> list[tuple[str, float]]:
    """按问题检索最相关的表。

    Args:
        question: 用户问题。
        top_k: 返回条数。

    Returns:
        ``[(表名, 相似度)]``，相似度 = 1 - 余弦距离，范围 [0,1]，降序。

    为什么用 ``1 - (embedding <=> %s)``：pgvector 的 ``<=>`` 返回余弦**距离**
    （0 表示同向），而调用方需要一个「越大越相关」的分数，与关键词打分
    加权时才不会符号错乱。
    """
    literal = "[" + ",".join(f"{v:.6f}" for v in _embed(question)) + "]"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, 1 - (embedding <=> %s::vector) AS score
            FROM schema_vectors
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (literal, literal, top_k),
        )
        return [(row[0], float(row[1])) for row in cur.fetchall()]


def search_or_fallback(question: str, top_k: int = 4) -> tuple[list[tuple[str, float]], str]:
    """带降级的检索入口。

    Returns:
        ``(结果, 来源)``。来源取值 ``pgvector`` / ``memory``，
        写进执行轨迹，方便在演示时证明「这一条真的查了向量库」。

    降级不是「偷偷换实现」：返回值里明确标出来源，页面上能看到。
    如果静默降级，答辩时被问「你怎么证明用了 pgvector」就答不上来。
    """
    try:
        if available():
            rows = search(question, top_k)
            if rows:
                return rows, "pgvector"
    except Exception:
        pass

    # 内存降级：同一套 embedding，暴力算余弦。5 张表的时候比网络往返还快。
    qv = _embed(question)
    scored: list[tuple[str, float]] = []
    for table, doc in table_documents().items():
        dv = _embed(doc)
        scored.append((table, sum(a * b for a, b in zip(qv, dv))))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k], "memory"
