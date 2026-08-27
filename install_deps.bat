@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================
echo   大眼X 依赖安装
echo ================================
echo.

where py >nul 2>&1
if errorlevel 1 (
  echo 未找到 py 命令，请先安装 Python 3.10+
  pause
  exit /b 1
)

echo [1/3] 升级 pip ...
py -m pip install -U pip
if errorlevel 1 goto fail

echo.
echo [2/3] 桌面壳 desktop.py ...
py -m pip install -r requirements-desktop.txt
if errorlevel 1 goto fail

echo.
echo [3/3] 服务 server.py ...
py -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo ================================
echo   安装完成
echo ================================
echo.
echo 系统组件（需手动确认）:
echo   - Microsoft Edge WebView2 运行时（桌面窗口必需）
echo     下载: https://developer.microsoft.com/microsoft-edge/web-view2/
echo.
echo 可选 MCP 扩展: 运行 mcp_setup.bat
echo 启动: 双击 大眼.bat 或桌面快捷方式
echo.
pause
exit /b 0

:fail
echo.
echo 安装失败，请把上方报错截图发出来。
pause
exit /b 1
