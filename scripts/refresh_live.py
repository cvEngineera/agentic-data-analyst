"""手动刷新实时库：``python scripts/refresh_live.py``

从 Open-Meteo 公开接口拉当前观测，写入独立的 ``data/db/live.db``。
**不碰评估库 bishe.db**，所以 144 条评估集、baseline、消融与回归门禁
完全不受影响。

两种用法：
    python scripts/refresh_live.py              # 只跑一次（默认）
    python scripts/refresh_live.py --loop 3600  # 每小时自动刷新，Ctrl+C 停止

自动循环刻意做成显式参数、默认不常驻：常駐进程会持续产生外部请求，
是否开启、多久一次属于部署决策。也可以用 Windows 任务计划替代 --loop。
单次失败不会退出循环，只打印错误后等待下一轮。

输出一份本地报告 ``data/reports/live_refresh.json``（被 .gitignore 忽略），
记录本次插入行数、各站点采样值与失败原因，方便论文写「数据来源」时取证。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import REPORT_DIR, ensure_dirs  # noqa: E402
from src.db.live_feed import refresh  # noqa: E402


def run_once() -> None:
    """执行一次采集并打印结果，写本地报告。"""
    print("正在从 Open-Meteo 公开接口拉取当前观测…", flush=True)
    info = refresh()

    print(f"\n实时库：{info['db']}")
    print(f"本次新增 {info['inserted']} 行，累计 {info['total_rows']} 行，"
          f"最新时间 {info['latest_ts']}（本地 UTC+8）")
    for s in info["samples"]:
        print(f"  {s['site']}: {s['temperature']}℃ / 湿度 {s['humidity']}% / "
              f"PM2.5 {s['pm25']}  @ {s['ts']}")
    if info["errors"]:
        print("\n以下站点本次失败（不影响其它站点）：")
        for e in info["errors"]:
            print("  " + e)
    print("\n注意：co2 / tvoc 为 NULL（公开源无此指标），"
          "live.db 不支持归因演示，也不参与任何评估与回归门禁。")

    ensure_dirs()
    out = REPORT_DIR / "live_refresh.json"
    out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="每 N 秒自动刷新一次；0（默认）表示只跑一次")
    args = ap.parse_args()
    if args.loop <= 0:
        run_once()
        return
    print(f"自动采集已启动：每 {args.loop} 秒一次，Ctrl+C 停止\n", flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:  # 单轮网络异常不能让常驻进程退出
            print(f"[采集失败] {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
