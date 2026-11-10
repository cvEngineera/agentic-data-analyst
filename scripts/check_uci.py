"""UCI 数据源核心链路验证：``python scripts/check_uci.py [--keep-uci]``

为什么单独写这个脚本，而不是直接跑正式评估集
--------------------------------------------
UCI 数据集没有 tvoc / pm25 两个指标，这两列在 UCI 模式下是 NULL。
如果把正式评估集直接搬到 UCI 数据上跑、再把不适用的题跳过，准确率数字
会变得好看但没有意义。因此这里只验证「真实数据接进来以后，核心链路还通不通」，
输出一份独立报告；**正式评估与回归门禁始终基于可复现的 synthetic 数据源**。

两种收尾行为
------------
默认（不加参数）：验证完自动把数据库恢复成 synthetic。脚本会强制重建库
两次（切到 uci、再切回来），所以执行后系统状态与执行前一致，不会让后续的
评估/门禁跑在错误的数据源上。即使中途出错，finally 里也会恢复——这比留一个
「跑完记得手动切回来」的口头约定可靠。

``--keep-uci``（v0.13）：只切一次，验证后**保留 UCI 作为当前演示数据源**，
供论文截图、真实数据问答演示使用。此时不要再跑正式评估集出准确率——
``run_eval.py`` 会主动拒绝在非 synthetic 数据源上产出论文数字。

报告写到 data/reports/uci_check.json，可直接作为论文「数据集与预处理」
一节的实验依据（含原始记录数、真实值范围、NULL 列说明、链路检查结果）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.runtime import run as agent_run  # noqa: E402
from src.config import REPORT_DIR, ensure_dirs  # noqa: E402
from src.db.core import fetch_all, init_database  # noqa: E402
from src.db.uci_loader import read_meta  # noqa: E402


def _check(name: str, ok: bool, detail: str) -> dict:
    """统一的结果结构，便于终端打印与 JSON 报告共用。"""
    return {"name": name, "ok": bool(ok), "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description="UCI 数据源核心链路验证")
    ap.add_argument("--keep-uci", action="store_true",
                    help="验证后保留 UCI 数据源（默认恢复 synthetic）")
    args = ap.parse_args()
    keep = args.keep_uci

    original = read_meta().get("source", "synthetic")
    print(f"执行前数据源：{original} | 收尾方式：{'保留 UCI' if keep else '恢复 synthetic'}")
    checks: list[dict] = []
    stats: dict = {}

    print("\n=== 切换到 UCI 公开数据集 ===")
    info = init_database(force=True, source="uci")
    print(f"  建库：{info}")

    try:
        # 1) 数据规模：原始记录数远大于抽样后的入库行数，说明是真实数据集
        _, cnt = fetch_all("SELECT COUNT(*) FROM sensor_data")
        raw_records = info.get("uci_records", 0)
        stats["sensor_rows"] = cnt[0][0]
        stats["uci_raw_records"] = raw_records
        checks.append(_check(
            "数据规模", cnt[0][0] == 1680 and raw_records > 20_000,
            f"入库 {cnt[0][0]} 行，UCI 原始 {raw_records} 条，等间隔抽样",
        ))

        # 2) 真实值范围：温度约 19–24℃、CO2 数百到上千 ppm，与合成数据不同
        _, rng = fetch_all(
            "SELECT ROUND(MIN(temperature),2), ROUND(MAX(temperature),2), "
            "ROUND(MIN(co2),2), ROUND(MAX(co2),2) FROM sensor_data"
        )
        stats["temperature_range"] = list(rng[0][:2])
        stats["co2_range"] = list(rng[0][2:])
        checks.append(_check(
            "真实测量值范围", rng[0][0] is not None and rng[0][2] is not None,
            f"温度 {rng[0][0]}~{rng[0][1]}℃，CO2 {rng[0][2]}~{rng[0][3]}ppm",
        ))

        # 3) 缺失指标保持 NULL——这一条是诚实性检查，不是功能检查
        _, nulls = fetch_all("SELECT COUNT(*) FROM sensor_data WHERE tvoc IS NULL AND pm25 IS NULL")
        stats["null_triple_rows"] = nulls[0][0]
        checks.append(_check(
            "缺失指标不编造", nulls[0][0] == cnt[0][0],
            f"tvoc/pm25 为 NULL 的行数 {nulls[0][0]}/{cnt[0][0]}（UCI 无此指标）",
        ))

        # 4) 单轮查询链路：自然语言 -> SQL -> 执行 -> 结果
        r1 = agent_run("3号教室昨天的平均温度是多少", mode="single")
        checks.append(_check(
            "单轮查询链路", bool(r1.rows) and not r1.denied,
            f"SQL={r1.sql[:70]}... 结果={str(r1.rows)[:60]}",
        ))

        # 5) 多表 JOIN：真实数据下维表关联仍可用
        _, joined = fetch_all(
            "SELECT s.name, ROUND(AVG(d.co2),1) FROM sensor_data d "
            "JOIN device v ON d.device_id=v.id JOIN site s ON v.site_id=s.id "
            "GROUP BY s.id ORDER BY s.id"
        )
        checks.append(_check(
            "三表 JOIN", len(joined) == 5,
            f"5 个教室 CO2 均值：{joined}",
        ))

        # 6) 安全管控：真实数据源下危险语句仍被拦截
        r2 = agent_run("删除所有设备记录")
        checks.append(_check("危险语句拦截", r2.denied, "只读 + AST 白名单生效"))

        # 7) 多步归因：注入式已知异常仍能被定位
        r3 = agent_run("这周哪个教室空气质量最差，为什么", mode="agent")
        checks.append(_check(
            "多步归因", "校准" in r3.conclusion,
            f"步数 {r3.steps}，结论：{r3.conclusion[:70]}",
        ))

    finally:
        if keep:
            # 演示模式：留着 UCI 库给页面和论文截图用。恢复命令见 README 第六节。
            print("\n=== 保留 UCI 数据源（--keep-uci）===")
        else:
            # 无论上面是否抛异常，都必须把库恢复成默认可复现数据源
            print("\n=== 恢复默认数据源 synthetic ===")
            back = init_database(force=True, source="synthetic")
            print(f"  已恢复：{back}")

    passed = sum(1 for c in checks if c["ok"])
    final_source = read_meta().get("source", "unknown")
    report = {
        "dataset": "UCI Occupancy Detection (id=357)",
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "passed": passed, "total": len(checks),
        "stats": stats, "checks": checks,
        "data_source_after": final_source,
        "note": ("正式评估集与回归门禁仍基于 synthetic；UCI 模式 tvoc/pm25 为 NULL。"
                 + (" 本次以 --keep-uci 收尾，当前演示库为 UCI；切回命令见 README 第六节。"
                    if keep else "")),
    }
    ensure_dirs()
    out = REPORT_DIR / "uci_check.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== 结果 {passed}/{len(checks)} ===")
    for c in checks:
        print(f"  [{'OK' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    print(f"\n结束后数据源：{final_source}")
    print(f"报告：{out}")
    sys.exit(0 if passed == len(checks) else 1)


if __name__ == "__main__":
    main()
