@echo off
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

if not exist "%PY%" goto no_venv
goto launch


:no_venv
echo.
echo   尚未创建虚拟环境，请先双击运行 run.bat 完成首次安装。
goto failed


:launch
"%PY%" -u tests\run_all.py %*
if errorlevel 1 goto tests_failed
echo.
echo   全部测试通过。
pause
exit /b 0


:tests_failed
echo.
echo   存在失败的测试项，详见上方输出。
goto failed


:failed
echo.
pause
exit /b 1
