@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在安装 MCP 依赖（只需这一次）...
call npm install
if errorlevel 1 (
  echo npm install 失败，请确认已安装 Node 20+。
  pause
  exit /b 1
)
if exist "node_modules\playwright\cli.js" (
  echo 正在安装 Chromium 浏览器（只需这一次）...
  node "node_modules\playwright\cli.js" install chromium
)
echo.
echo 完成。之后启动大眼不会再重新下载这些包。
pause
