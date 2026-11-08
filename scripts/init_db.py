"""一键建库脚本：``python scripts/init_db.py [--force]``

做三件事：确保 data/ 目录存在 -> 执行 schema.sql -> 灌入可复现的合成数据。
``--force`` 会先删除旧库；不加则复用已有数据（幂等，不会重复灌库）。

为什么把脚本放在 scripts/ 而不是 src/：它是一次性的运维入口，
不属于运行时代码；这里用 sys.path.insert 把项目根加进搜索路径，
这样从任意工作目录执行都能 import src.*。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.core import init_database  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="删除已有库并重建")
    args = ap.parse_args()
    info = init_database(force=args.force)
    if info.get("reused"):
        print(f"数据库已存在，复用现有数据：sensor_data {info['sensor_data']} 行")
    else:
        print("建库完成：")
        for k, v in info.items():
            print(f"  {k}: {v}")
    print("\n下一步：python -m src.cli \"3号教室昨天的平均温度是多少\"")


if __name__ == "__main__":
    main()
