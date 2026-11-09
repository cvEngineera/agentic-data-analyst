"""一键建库脚本：``python scripts/init_db.py [--force] [--source synthetic|uci]``

做三件事：确保 data/ 目录存在 -> 执行 schema.sql -> 按指定数据源灌数据。
``--force`` 会先删除旧库；不加则复用已有数据（幂等，不会重复灌库）。

数据源（v0.4 新增）：
    synthetic  默认。固定种子 42 的合成数据，离线可复现，正式评估集与
               回归门禁都基于它，保证数字不因网络或数据集版本漂移。
    uci        UCI Occupancy Detection 公开真实数据集，首次运行会下载
               约 328KB 的 zip 到 data/raw/。用于验证「数据接入适配器」
               确实可用，并给论文的数据来源一章提供真实依据。

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
    ap.add_argument("--source", choices=("synthetic", "uci"), default="synthetic",
                    help="数据源：synthetic（默认，可复现）或 uci（公开真实数据集）")
    args = ap.parse_args()

    info = init_database(force=args.force, source=args.source)
    if info.get("reused"):
        print(f"数据库已存在，复用现有数据：数据源 {info['source']}，"
              f"sensor_data {info['sensor_data']} 行")
        if info["source"] != args.source:
            print(f"  注意：当前库是 {info['source']}，要切换到 {args.source} 请加 --force")
    else:
        print("建库完成：")
        for k, v in info.items():
            print(f"  {k}: {v}")
        if args.source == "uci":
            print("\n提示：tvoc/pm25 为 NULL（UCI 无此指标），"
                  "请改用 python scripts/check_uci.py 验证核心链路。")
    print("\n下一步：python -m src.cli \"3号教室昨天的平均温度是多少\"")


if __name__ == "__main__":
    main()
