"""初始化 pgvector 里的 Schema 向量索引（v0.12）。

用法::

    docker compose up -d                       # 先把 pgvector 容器起起来
    python scripts/setup_pgvector.py            # 建扩展 + 建表 + 灌向量
    python scripts/setup_pgvector.py --check    # 只探测连通性与索引状态
    python scripts/setup_pgvector.py --query "3号教室的二氧化碳"   # 试一条检索

脚本不会写入向量以外的任何东西，也不会连业务库。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.schema_retrieval import vector_store as vs  # noqa: E402


def check() -> bool:
    """探测连通性：能不能连上、扩展在不在、表里有多少行。"""
    print(f"目标库：{settings.pgvector_url}")
    if not vs.available():
        print("× 连不上 PostgreSQL 或 vector 扩展未启用")
        print("  排查顺序：docker compose ps -> 端口 5433 是否被占用 -> 密码是否一致")
        return False
    try:
        import psycopg
        with psycopg.connect(settings.pgvector_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ver = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM schema_vectors")
            rows = cur.fetchone()[0]
    except Exception as exc:
        # 表还没建时这里会报错，说明扩展在、但索引还没初始化
        print(f"√ 扩展已启用，但 schema_vectors 表尚不存在（{type(exc).__name__}）")
        print("  执行不带 --check 的命令即可建表")
        return False
    print(f"√ pgvector {ver} 可用，schema_vectors 现有 {rows} 行")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="初始化 Schema 向量索引（pgvector）")
    ap.add_argument("--check", action="store_true", help="只探测状态，不写入")
    ap.add_argument("--force", action="store_true", help="清空重建")
    ap.add_argument("--query", default="", help="初始化后试一条检索")
    args = ap.parse_args()

    if args.check:
        raise SystemExit(0 if check() else 1)

    try:
        result = vs.index_schema(force=args.force)
    except Exception as exc:
        print(f"× 初始化失败：{type(exc).__name__}: {exc}")
        print("  确认容器已启动：docker compose up -d")
        raise SystemExit(1)
    print(f"√ 已写入 {result['indexed']} 张表的向量（维度 {vs.EMBED_DIM}）")

    q = args.query or "哪个教室二氧化碳最高"
    rows, source = vs.search_or_fallback(q)
    print(f"\n检索示例：{q}（来源 {source}）")
    for table, score in rows:
        print(f"  {table:22s} {score:.4f}")


if __name__ == "__main__":
    main()
