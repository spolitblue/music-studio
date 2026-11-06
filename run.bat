@echo off
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

rem 用 goto 标签而不是 if(...) 括号块。括号块里的 %errorlevel% 会在**解析期**
rem 就被展开，判断到的是执行前的旧值 —— 这是批处理最经典的坑之一。

if not exist "%PY%" goto bootstrap
goto launch


:bootstrap
echo.
echo   首次运行，正在准备运行环境…
echo.
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m venv .venv
goto check_venv

:use_python
python -m venv .venv

:check_venv
if not exist "%PY%" goto no_python
echo   正在安装依赖（约 250 MB，仅需一次，请耐心等待）…
echo.
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto pip_failed
goto launch


:launch
"%PY%" -m app.main %*
if errorlevel 1 goto app_failed
goto done


:no_python
echo.
echo   [错误] 创建虚拟环境失败。
echo   请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
echo   安装时请勾选 "Add Python to PATH"。
goto failed

:pip_failed
echo.
echo   [错误] 依赖安装失败，请检查网络连接后重试。
goto failed

:app_failed
echo.
echo   [错误] 程序异常退出，上面的信息是具体原因。
goto failed


:failed
echo.
pause
exit /b 1

:done
endlocal
