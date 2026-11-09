"""生成离线快照并注入 index.html（v0.3.1 新增，v0.9 简化）。

运行：``python scripts/build_snapshots.py``

用途：让 src/web/index.html **不启动服务也能点快捷问题**。
把 5 个典型问题的真实运行结果（结论、SQL、结果集、执行轨迹）内嵌进页面，
离线打开时 fetch /api/ask 失败会自动回退到这些快照，并明确标注「预录结果」。
启动 scripts/serve.py 后，实时结果优先，快照不会被使用。

v0.9 界面改版后，离线回退逻辑已直接写在页面脚本里，本脚本不再改写
页面代码，只负责替换 ``const SNAPSHOTS = ...`` 那一行。
幂等：重复运行只替换该行，不会重复插入；注入锚点是 ``const $ = id =>``。

每次改了运行时逻辑，重跑本脚本即可刷新快照，保证页面与代码一致。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agent.runtime import run  # noqa: E402

PAGE = ROOT / "src" / "web" / "index.html"

# 与页面侧栏「快捷示例」一一对应
QUESTIONS: list[tuple[str, str]] = [
    ("3号教室昨天的平均温度是多少", "auto"),
    ("这周哪个教室空气质量最差，为什么", "agent"),
    ("报警次数最多的设备属于哪个楼层", "auto"),
    ("删除所有设备记录", "auto"),
    ("各教室本月的 CO2 均值排名", "auto"),
]


def main() -> None:
    snapshots = {q: run(q, mode=mode).to_dict() for q, mode in QUESTIONS}
    blob = json.dumps(snapshots, ensure_ascii=False, separators=(",", ":"))

    lines, out, inserted = PAGE.read_text(encoding="utf-8").splitlines(), [], False
    for line in lines:
        if line.startswith("const SNAPSHOTS = "):
            # 已有快照行：只保留第一次出现，实现幂等替换
            if not inserted:
                out.append("const SNAPSHOTS = " + blob + ";")
                inserted = True
            continue
        if line.startswith("const $ = id =>") and not inserted:
            # 页面还没有 SNAPSHOTS 行时，在脚本开头插入
            out.append("const SNAPSHOTS = " + blob + ";")
            inserted = True
        out.append(line)
    if not inserted:
        raise RuntimeError("未找到 <script> 中的插入锚点，index.html 结构可能已改变")

    PAGE.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"已写入 {len(snapshots)} 个离线快照（{len(blob)} 字符）-> {PAGE}")
    for q, _ in QUESTIONS:
        print(f"  {q}")


if __name__ == "__main__":
    main()
