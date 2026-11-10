@echo off
setlocal
cd /d "%~dp0"

rem Standalone MySQL 8.0 demo instance on port 3307.
rem It owns a private datadir at E:\tmp\mysql3307\data and NEVER touches
rem the existing 3306 instance. root has an empty password on purpose
rem (--initialize-insecure) so no credential is needed.
rem
rem Usage:
rem   start_mysql_demo.bat         -> MySQL + mock model (offline)
rem   start_mysql_demo.bat real    -> MySQL + real LLM (needs LLM_API_KEY in .env)
rem   start_mysql_demo.bat vector  -> also enable pgvector retrieval
rem   start_mysql_demo.bat real vector -> all features on
rem
rem If port 3307 is already up, steps 2-3 are skipped and we just re-migrate.

set "MYSQL_HOME=C:\Program Files\MySQL\MySQL Server 8.0"
set "MYSQLD=%MYSQL_HOME%\bin\mysqld.exe"
set "MYSQL=%MYSQL_HOME%\bin\mysql.exe"
set "BASE=E:\tmp\mysql3307"
set "DATA=%BASE%\data"
set "URL=mysql+pymysql://root@127.0.0.1:3307/bishe?charset=utf8mb4"

if not exist "%MYSQLD%" (
  echo [ERROR] mysqld.exe not found: %MYSQLD%
  pause
  exit /b 1
)

echo [1/5] Checking port 3307 ...
"%MYSQL%" -u root -h 127.0.0.1 -P 3307 --connect-timeout=3 -N -e "SELECT 1" >nul 2>&1
if not errorlevel 1 goto :ready

if not exist "%DATA%\ibdata1" (
  echo [2/5] Initializing fresh datadir with an empty root password ...
  if not exist "%BASE%" mkdir "%BASE%"
  "%MYSQLD%" --no-defaults --initialize-insecure --basedir="%MYSQL_HOME%" --datadir="%DATA%" --console
  if errorlevel 1 goto :fail
) else (
  echo [2/5] Reusing existing datadir %DATA%
)

echo [3/5] Starting mysqld on 3307 in a minimized window ...
start "MySQL 3307 standalone demo" /MIN "%MYSQLD%" --no-defaults --datadir="%DATA%" --port=3307 --bind-address=127.0.0.1 --socket="%BASE%\mysql.sock" --pid-file="%BASE%\mysql.pid" --console --log-error-verbosity=2

set /a TRIES=0
:wait
set /a TRIES+=1
"%MYSQL%" -u root -h 127.0.0.1 -P 3307 --connect-timeout=2 -N -e "SELECT 1" >nul 2>&1
if not errorlevel 1 goto :ready
if %TRIES% GEQ 30 goto :fail
ping -n 2 127.0.0.1 >nul
goto :wait

:ready
echo [4/5] Creating schema, migrating data and verifying ...
set "MYSQL_URL=%URL%"
python scripts/setup_mysql.py --force
if errorlevel 1 goto :fail

echo [5/5] Starting FastAPI against MySQL ...
set "DB_BACKEND=mysql"
for %%a in (%*) do (
  if /i "%%a"=="real" set "LLM_MODE=real"
  if /i "%%a"=="vector" set "ENABLE_VECTOR_RETRIEVAL=1"
)
echo.
echo   FastAPI : http://127.0.0.1:8000
echo   API doc : http://127.0.0.1:8000/docs
echo   Backend : mysql (127.0.0.1:3307/bishe)
echo   Press Ctrl+C to stop FastAPI. The MySQL window stays open until you close it.
echo.
python scripts/api.py
pause
exit /b 0

:fail
echo [ERROR] Standalone MySQL demo failed. See the messages above.
pause
exit /b 1
