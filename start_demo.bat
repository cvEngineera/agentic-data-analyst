@echo off
cd /d "%~dp0"
echo ============================================
echo  agentic-data-analyst v0.12  local demo
echo  FastAPI:  http://127.0.0.1:8000
echo  API doc:  http://127.0.0.1:8000/docs
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
