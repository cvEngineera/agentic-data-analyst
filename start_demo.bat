@echo off
cd /d "%~dp0"
echo Starting agentic-data-analyst local demo...
echo Open http://127.0.0.1:8765 in your browser (it opens automatically).
echo Press Ctrl+C to stop.
python scripts/serve.py
pause
