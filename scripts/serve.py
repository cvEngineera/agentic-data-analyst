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
from src.config import DB_PATH, settings  # noqa: E402
from src.evaluation.ablation import run_ablation  # noqa: E402
from src.evaluation.runner import evaluate, load_cases  # noqa: E402
from src.version import VERSION  # noqa: E402
# v0.12：状态与版本信息的实现抽到 src/web/payloads.py，
# FastAPI 版本（scripts/api.py）复用同一份，避免两个服务显示不一致。
from src.web.payloads import changes_payload, status_payload  # noqa: E402

WEB_DIR = ROOT / "src" / "web"


def _status() -> dict:
    """兼容包装：老代码里仍叫 _status()，实现已移到 src.web.payloads。"""
    return status_payload(VERSION)


def _changes() -> dict:
    """兼容包装：实现已移到 src.web.payloads。"""
    return changes_payload()


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
            elif path.startswith("/vendor/"):
                # v0.12：前端改用 Vue 3 + ECharts，两个 js 放在 src/web/vendor。
                # 标准库版服务也要能把它们发出去，否则页面白屏。
                # 这里只允许读取 vendor 目录内的文件，并用 resolve() 校验，
                # 防止 ../../ 这类路径穿越读到仓库外的文件。
                base = (WEB_DIR / "vendor").resolve()
                target = (base / path[len("/vendor/"):]).resolve()
                if not str(target).startswith(str(base)) or not target.is_file():
                    self._send({"error": "not found"}, status=404)
                    return
                ctype = ("application/javascript; charset=utf-8"
                         if target.suffix == ".js" else "application/octet-stream")
                self._send(target.read_bytes(), ctype=ctype)
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
