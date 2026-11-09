"""轻量语义打分：Chroma / pgvector 的零依赖可插拔替代（v0.7）。

为什么不是 Chroma
------------------
开题报告 4.3 把「Chroma（本地文件型），必要时 pgvector」列为 Schema 语义
索引的选型。实测本机 pip 连 chromadb 的版本列表都取不到，langgraph 同样
装不上；把整个毕设卡在装包上不合理。因此这里用**纯标准库**实现一个与
向量库接口等价的 provider：

    输入 (问题, 表文档) -> [0, 1] 相似度

将来环境允许时，只需替换本模块内部的打分实现（换成 Chroma 查询或
embedding 余弦），``catalog.hybrid_score()`` 与消融开关一行都不用改。

诚实边界（论文必须照抄）
------------------------
这是**字符 n-gram + TF-IDF 余弦的词形级语义匹配，不是神经网络 embedding**，
效果上限低于 BGE / text-embedding 这类模型，也不同于 Chroma 的向量索引。
它的价值是让「混合检索」在工程上真正跑起来、并能被消融实验量化贡献，
而不是冒充向量数据库。开题报告里的 Chroma 选型应表述为「接口预留、
因环境限制改用轻量实现」，不能写成已部署 Chroma。

实现要点
--------
- 中文没有天然词边界，用字符 bigram 切分；英文/数字按小写词保留。
- IDF 只在这五张表的文档集合上统计，规模固定、可缓存，单次查询是纯内存运算。
- 向量 L2 归一化后点积即余弦相似度，裁剪到 [0,1] 便于与关键词分加权。
"""
from __future__ import annotations

import math
import re
from collections import Counter

# 全库表文档与 IDF 只算一次：Schema 检索是高频调用，不能每次重新统计
_DOCS_CACHE: dict[str, str] | None = None
_IDF_CACHE: dict[str, float] | None = None


def _table_docs() -> dict[str, str]:
    """把每张表的表名、注释、中文别名、字段名与字段说明拼成一篇「文档」。

    延迟 import catalog 是为了避免模块循环依赖（catalog 会 import 本模块）。
    字段说明里带「温度(℃)」「二氧化碳(ppm)」这类中文，正是字符级语义匹配
    能补上关键词别名表的地方。
    """
    global _DOCS_CACHE
    if _DOCS_CACHE is not None:
        return _DOCS_CACHE
    from .catalog import TABLE_META  # 局部导入，打破循环依赖

    docs: dict[str, str] = {}
    for table, meta in TABLE_META.items():
        parts: list[str] = [table, str(meta.get("comment", ""))]
        parts.extend(str(a) for a in meta.get("aliases", []))
        cols = meta.get("columns", {})
        parts.extend(str(k) for k in cols)
        parts.extend(str(v) for v in cols.values())
        docs[table] = " ".join(parts)
    _DOCS_CACHE = docs
    return docs


def _ngrams(text: str) -> list[str]:
    """中文取字符 bigram，英文/数字取小写词；两者合并成 token 列表。

    例：「3号教室 CO2」-> ['3号','号教','教室','co2', ...]。
    字符 bigram 对「二氧化碳」与「co2」这类中英混排说法有一定泛化能力，
    且完全不需要分词器或模型文件。
    """
    s = re.sub(r"\s+", " ", text.lower()).strip()
    tokens = re.findall(r"[a-z0-9_]+", s)
    han = re.sub(r"[^一-鿿]", "", s)
    tokens.extend(han[i:i + 2] for i in range(len(han) - 1))
    return tokens or [s]


def _idf(docs: dict[str, str]) -> dict[str, float]:
    """在固定表文档集合上算 IDF，稀有 token 权重更高。"""
    global _IDF_CACHE
    if _IDF_CACHE is not None:
        return _IDF_CACHE
    n = len(docs)
    df: Counter[str] = Counter()
    for doc in docs.values():
        df.update(set(_ngrams(doc)))
    _IDF_CACHE = {t: math.log((n + 1) / (c + 0.5)) + 1.0 for t, c in df.items()}
    return _IDF_CACHE


def _vector(text: str, idf: dict[str, float]) -> dict[str, float]:
    """TF-IDF 向量并做 L2 归一化，返回稀疏字典。"""
    tf = Counter(_ngrams(text))
    raw = {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in raw.values()))
    if not norm:
        return {}
    return {t: v / norm for t, v in raw.items()}


def semantic_score(question: str, table: str) -> float:
    """返回问题与某张表文档的语义相似度，范围 [0, 1]。

    这就是留给 Chroma / pgvector 的替换点：将来把函数体换成
    「取 embedding -> 查向量库 -> 返回归一化相似度」即可，
    调用方 ``catalog.hybrid_score()`` 不需要任何改动。
    """
    docs = _table_docs()
    idf = _idf(docs)
    qv = _vector(question, idf)
    dv = _vector(docs.get(table, table), idf)
    if not qv or not dv:
        return 0.0
    cosine = sum(v * dv.get(t, 0.0) for t, v in qv.items())
    return max(0.0, min(1.0, cosine))
