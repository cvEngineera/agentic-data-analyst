"""FastAPI 服务入口（v0.12，对齐申报表技术栈）。

为什么从 http.server 换成 FastAPI
--------------------------------
v0.3 用标准库 ``http.server`` 起演示服务，理由是当时 pip 装不上 FastAPI，
而「每完成一步都有一个能打开看的版本」比技术栈好看更重要。现在依赖
装得上了，按申报表换成 FastAPI，收益有三个：

1. **接口契约可校验**。请求参数用 Pydantic 模型声明，``q`` 缺失会返回
   422 并指出缺哪个字段，而不是自己手写 ``if not question``。
2. **自带 OpenAPI 文档**。访问 ``/docs`` 就是一份可交互的接口说明，
   答辩现场可以直接点着演示「系统对外提供哪些能力」。
3. **异步与并发**。评估、消融这两条链路耗时长，用线程池跑不会堵住
   其他请求；标准库的 ThreadingHTTPServer 也能做，但边界要自己管。

服务分层（与 v0.3 一致，界面做薄、逻辑在 src/）：

    浏览器 --HTTP--> 本文件（参数解析 / 序列化）
                        └──> src.agent.runtime.run      问答主链路
                        └──> src.evaluation.runner      评估
                        └──> src.evaluation.ablation    消融
                        └──> src.web.payloads           状态与版本信息

运行::

    python scripts/api.py                 # 默认 127.0.0.1:8000
    python scripts/api.py --port 8765     # 换端口
    uvicorn scripts.api:app --reload      # 开发模式（热重载）

注意：``scripts/serve.py``（零依赖版）仍然保留，作为「装不上包也能演示」
的兜底通道。两者接口完全一致，前端不需要区分。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 让任意工作目录下都能 import src.*

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src.agent.runtime import run as agent_run  # noqa: E402
from src.db.orm import reflect_schema, table_counts  # noqa: E402
from src.evaluation.ablation import run_ablation  # noqa: E402
from src.evaluation.runner import evaluate, load_cases  # noqa: E402
from src.version import VERSION  # noqa: E402
from src.web.payloads import changes_payload, status_payload  # noqa: E402

WEB_DIR = ROOT / "src" / "web"

app = FastAPI(
    title="物联网监测数据分析 Agent",
    version=VERSION,
    description=(
        "基于大语言模型的监测数据分析 Agent：自然语言 → SQL → 多步归因 → 分层评估。\n\n"
        "本服务只读访问监测数据库，不提供任何写接口。"
    ),
)

# vendor 目录放 Vue 3 与 ECharts 的本地副本（约 1.2 MB）。
# 为什么不走 CDN：答辩现场的网络不一定通，图表挂了会被误认为系统坏了；
# 本地文件也符合「离线可演示」这条贯穿全项目的原则。
app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")


class AskRequest(BaseModel):
    """问答请求体（POST 用）。

    为什么 GET 与 POST 都提供：
        - GET 方便在浏览器地址栏直接试、也方便 curl 一行验证；
        - POST 是正规用法，问题里带 ``#``、``&`` 这类字符时不会被
          URL 编码搞坏（实测过「CO2 超过 1000 #3 号教室」被截断）。
    """

    question: str = Field(..., min_length=1, max_length=500,
                          description="自然语言问题，例如：这周哪个教室空气质量最差，为什么")
    mode: str = Field("auto", pattern="^(auto|single|agent)$",
                      description="auto=自动判断是否需要多步归因；single=只查一次；agent=强制多步")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """返回单页前端。

    前端是单文件 HTML（Vue 3 + ECharts，依赖已随仓库 vendor 到 src/web/vendor/），
    不需要构建步骤，改完刷新即生效——毕设演示不适合引入 npm 工具链。
    """
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="前端文件缺失：src/web/index.html")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/api/status")
def status() -> dict:
    """版本、后端类型、数据规模、报告清单。页面顶部状态栏用它。"""
    return status_payload(VERSION)


@app.get("/api/health")
def health() -> dict:
    """健康检查：确认数据库与模型通道当前是否可用。

    与 ``/api/status`` 的区别：这里会真的连一次数据库（``table_counts``），
    所以能反映「服务活着但数据库连不上」这种半死状态。
    """
    payload = {"ok": True, "version": VERSION, "db_backend": status_payload(VERSION)["db_backend"]}
    try:
        payload["tables"] = table_counts()
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload


@app.get("/api/ask")
def ask_get(q: str = Query(..., min_length=1, max_length=500),
            mode: str = Query("auto", pattern="^(auto|single|agent)$")) -> dict:
    """GET 版问答接口。"""
    return agent_run(q, mode=mode).to_dict()


@app.post("/api/ask")
def ask_post(req: AskRequest) -> dict:
    """POST 版问答接口（推荐）。"""
    return agent_run(req.question, mode=req.mode).to_dict()


@app.get("/api/schema")
def schema() -> dict:
    """用 SQLAlchemy Inspector 反射当前库的表结构。

    这个接口存在本身就是为了证明「数据层走的是 SQLAlchemy 而不是手写 SQL」：
    它不认识具体表名，全靠反射，换库（SQLite→MySQL）后返回的结构一致。
    """
    try:
        tables = reflect_schema()
        return {"backend": status_payload(VERSION)["db_backend"], "tables": tables}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/api/eval")
def eval_smoke() -> dict:
    """现跑 15 条冒烟集并返回分层汇总。

    冒烟集故意小：它是给「改完代码立刻确认没搞坏」用的，
    正式 144 条评估走 ``scripts/run_eval.py``，那个要几分钟。
    """
    _, summary = evaluate(load_cases())
    return summary


@app.get("/api/ablation")
def ablation() -> dict:
    """现跑五组消融配置，返回每组相对完整方案的 delta。

    耗时较长（五组 × 15 条），页面上是点击后才发起的，不做预加载。
    """
    return run_ablation()


@app.get("/api/changes")
def changes() -> dict:
    """git 版本历史与相对上一版的 diff 统计。"""
    return changes_payload()


@app.exception_handler(Exception)
async def unhandled(_request, exc: Exception) -> JSONResponse:
    """兜底异常处理：把异常类型带回前端，而不是只给一个 500 空响应。

    本地演示服务，错误信息对使用者有用（比如「MySQL 连不上」），
    不需要像生产环境那样藏起来。
    """
    return JSONResponse(status_code=500,
                        content={"error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    """命令行入口。"""
    import uvicorn

    ap = argparse.ArgumentParser(description="FastAPI 演示服务")
    ap.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    ap.add_argument("--reload", action="store_true", help="代码变更自动重载（开发用）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"毕设演示 {VERSION}（FastAPI）已启动：{url}")
    print(f"接口文档：{url}/docs")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    uvicorn.run("scripts.api:app" if args.reload else app,
                host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
