@echo off
cd /d "%~dp0"
rem 用法：
rem   start_demo.bat         -> mock 模式（规则模板，离线、免费、结果确定）
rem   start_demo.bat real    -> real 模式（真调大模型，需 .env 里配好 LLM_API_KEY，按量计费）
rem 想演示 MySQL 后端请改用 start_mysql_demo.bat（独立 3307 实例，免密码）
if /i "%~1"=="real" set LLM_MODE=real

echo ============================================
echo  agentic-data-analyst v0.13.1  local demo
echo  mode      : %LLM_MODE%
echo  FastAPI   : http://127.0.0.1:8000
echo  API doc   : http://127.0.0.1:8000/docs
echo  Press Ctrl+C to stop.
echo ============================================
echo.
echo [1/2] starting FastAPI ...
python scripts/api.py
if errorlevel 1 (
  echo.
  echo FastAPI 启动失败（可能缺 fastapi / uvicorn）。改用零依赖兜底服务：
  echo   http://127.0.0.1:8765
  python scripts/serve.py
)
pause
