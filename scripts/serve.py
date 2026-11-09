"""零依赖本地演示服务器（v0.3 新增）。

运行：``python scripts/serve.py``，然后浏览器打开 http://127.0.0.1:8765
（默认自动打开浏览器；加 --no-browser 只启动服务，便于脚本化验证）。

为什么不用 Streamlit：
    Streamlit 是可选增强，本机网络下 pip 安装一直超时。为了做到
    「每完成一步都有一个能打开看的版本」，用 Python 标准库
    http.server 写一个极薄的 HTTP 层，前端是单文件 HTML，
    零第三方依赖，任何环境都能起。

架构（延续全项目「界面做薄」的原则）：
    浏览器 --> GET /            静态页面（src/web/index.html）
           --> GET /api/status  版本、模式、数据行数、报告清单
           --> GET /api/ask     调 agent.runtime.run，返回完整结果 JSON
           --> GET /api/eval    现跑 15 条冒烟集，返回分层汇总
           --> GET /api/ablation 现跑五组消融，返回 delta 报告
           --> GET /api/changes 读 git log / tag / diff --stat，展示代码变化

v0.9：前端改为单文件深色对话式界面（src/web/index.html），API 契约保持不变，
服务端无需改动；会话历史由浏览器 localStorage 管理，不落盘、不入库。

所有业务判断仍在 src/ 里，本文件只做参数解析、调用与 JSON 序列化。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 让任意工作目录下都能 import src.*

from src.agent.runtime import run  # noqa: E402
from src.config import DB_PATH, REPORT_DIR, settings  # noqa: E402
from src.db.core import connect  # noqa: E402
from src.evaluation.ablation import run_ablation  # noqa: E402
from src.evaluation.runner import evaluate, load_cases  # noqa: E402

WEB_DIR = ROOT / "src" / "web"
VERSION = "v0.11"


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


def _status() -> dict:
    """汇总当前版本状态，供页面顶部状态栏显示。

    v0.4 起额外返回数据源元信息（synthetic / uci）。页面要让人一眼看出
    当前跑的是可复现合成数据还是 UCI 公开真实数据，避免把两种口径的
    评估数字混着看——这是数据接入适配器落地后必须补上的一处可解释性。
    """
    # 局部导入：启动服务时不触碰 uci_loader 的下载/解压相关代码
    from src.db.uci_loader import read_meta
    meta = read_meta()
    rows = 0
    if DB_PATH.exists():
        conn = connect(readonly=True)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0]
        finally:
            conn.close()
    reports = []
    if REPORT_DIR.exists():
        for p in sorted(REPORT_DIR.iterdir()):
            if p.is_file() and p.suffix in (".json", ".jsonl"):
                reports.append({"name": p.name, "size": p.stat().st_size})
    return {
        "version": VERSION,
        "llm_mode": settings.llm_mode,
        "db_exists": DB_PATH.exists(),
        "sensor_rows": rows,
        "data_source": meta.get("source", "synthetic"),
        "dataset": meta.get("dataset", ""),
        "data_note": meta.get("note", ""),
        "cases_smoke": len(load_cases()),
        "cases_full": len(load_cases(ROOT / "src" / "evaluation" / "cases_eval.jsonl"))
        if (ROOT / "src" / "evaluation" / "cases_eval.jsonl").exists() else 0,
        "reports": reports,
    }


def _changes() -> dict:
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


class Handler(BaseHTTPRequestHandler):
    """极简路由：静态页 + 五个只读/计算型 API。"""

    def _send(self, payload, ctype: str = "application/json; charset=utf-8",
              status: int = 200) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                if isinstance(payload, (dict, list)) else
                payload.encode("utf-8") if isinstance(payload, str) else payload)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 本地演示服务，允许页面用 fetch 直接读；不对外监听，仅绑 127.0.0.1
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (标准库约定的方法名)
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
                self._send(html, ctype="text/html; charset=utf-8")
            elif path == "/api/status":
                self._send(_status())
            elif path == "/api/ask":
                question = query.get("q", [""])[0].strip()
                mode = query.get("mode", ["auto"])[0]
                if not question:
                    self._send({"error": "缺少问题参数 q"}, status=400)
                    return
                if mode not in ("auto", "single", "agent"):
                    mode = "auto"
                # 复用 CLI 同一条链路，保证网页看到的结果与命令行完全一致
                self._send(run(question, mode=mode).to_dict())
            elif path == "/api/eval":
                _, summary = evaluate(load_cases())
                self._send(summary)
            elif path == "/api/ablation":
                self._send(run_ablation())
            elif path == "/api/changes":
                self._send(_changes())
            else:
                self._send({"error": "not found"}, status=404)
        except Exception as exc:
            # 单个请求出错不能让整个服务挂掉
            self._send({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def log_message(self, fmt: str, *args) -> None:
        """精简访问日志，只打印方法与路径，避免刷屏。"""
        print(f"  {self.command} {self.path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="本地演示服务器（零第三方依赖）")
    ap.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    # 只绑回环地址，不对外暴露；符合「本地演示」定位
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"毕设演示 {VERSION} 已启动：{url}")
    print(f"模式 {settings.llm_mode} | 数据库 {DB_PATH.name} | Ctrl+C 停止\n")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
