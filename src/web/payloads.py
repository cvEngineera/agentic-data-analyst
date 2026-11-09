"""前端所需的两个「元信息」接口实现（v0.12 抽出来复用）。

背景
----
v0.3 起这些逻辑写在 ``scripts/serve.py`` 里（标准库 http.server），
v0.12 加了 FastAPI 通道后两个服务器都需要同一份逻辑。复制一份会导致
「页面在 A 端口显示 v0.11、在 B 端口显示 v0.12」这种低级不一致，
所以抽到 ``src/web`` 下，两个服务器都 import 它。

这里只做「读」，不写任何文件，也不改配置。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import DB_PATH, ROOT, REPORT_DIR, settings
from ..db.core import connect
from ..evaluation.runner import load_cases


def _git_text(args: list[str]) -> str:
    """在项目目录执行只读 git 命令，把输出作为文本返回。

    只用于展示，不修改仓库；任何异常都返回说明文字而不是抛错，
    避免页面因为 git 没配置好就整块崩掉。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        return (out.stdout or out.stderr).strip() or "(无输出)"
    except Exception as exc:
        return f"(git 不可用: {type(exc).__name__})"


def status_payload(version: str) -> dict:
    """汇总当前版本状态，供页面顶部状态栏显示。

    v0.12 增加 ``db_backend`` / ``db_url_masked`` / ``llm_backend`` 三项：
    现在同时存在 SQLite 与 MySQL 两个后端，页面必须让人一眼看出
    「当前连的是哪个库、用哪条模型通道」，否则演示时数字对不上会很难解释。

    密码做了打码：状态接口是给前端渲染用的，不应该把连接串里的
    明文密码发到浏览器（哪怕只是本地）。
    """
    # 局部导入：启动服务时不触碰 uci_loader 的下载/解压相关代码
    from ..db.uci_loader import read_meta

    meta = read_meta()
    rows = 0
    if DB_PATH.exists():
        conn = connect(readonly=True)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0]
        finally:
            conn.close()

    # MySQL 模式下的行数单独查：DB_BACKEND=mysql 时页面要显示真库的规模
    mysql_rows = None
    if settings.db_backend == "mysql":
        try:
            from ..db.orm import table_counts
            mysql_rows = table_counts().get("sensor_data")
        except Exception:
            mysql_rows = -1  # 连不上时给 -1，让页面显示「不可用」而不是崩掉

    reports = []
    if REPORT_DIR.exists():
        for p in sorted(REPORT_DIR.iterdir()):
            if p.is_file() and p.suffix in (".json", ".jsonl"):
                reports.append({"name": p.name, "size": p.stat().st_size})

    full_cases = ROOT / "src" / "evaluation" / "cases_eval.jsonl"
    return {
        "version": version,
        "llm_mode": settings.llm_mode,
        "llm_backend": settings.llm_backend,
        "db_backend": settings.db_backend,
        "db_exists": DB_PATH.exists() or settings.db_backend == "mysql",
        "sensor_rows": rows,
        "mysql_sensor_rows": mysql_rows,
        "data_source": meta.get("source", "synthetic"),
        "dataset": meta.get("dataset", ""),
        "data_note": meta.get("note", ""),
        "cases_smoke": len(load_cases()),
        "cases_full": len(load_cases(full_cases)) if full_cases.exists() else 0,
        "reports": reports,
    }


def changes_payload() -> dict:
    """返回 git 版本历史与相对上一版的变更统计，页面用来展示「代码不断变化」。

    diff 不写死版本号：按版本排序取最近两个 tag 自动对比，
    这样以后每打一个新 tag，页面刷新就能看到最新一步改了什么。
    """
    tags_text = _git_text(["tag", "-l", "--sort=-v:refname"])
    tags = [t.strip() for t in tags_text.splitlines() if t.strip()]
    cur, prev = (tags[0] if tags else "HEAD"), (tags[1] if len(tags) > 1 else "")
    diff = (_git_text(["diff", prev, cur, "--stat"])
            if prev else "(只有一个版本，暂无可对比的 diff)")
    return {
        "tags": tags_text,
        "log": _git_text(["log", "--oneline", "--decorate", "-10"]),
        "diff_label": f"{prev} → {cur} 变更" if prev else "版本变更",
        "diff_prev": diff,
        "status": _git_text(["status", "--short"]),
    }
