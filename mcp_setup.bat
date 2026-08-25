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
echo 正在安装 Python MCP（Blender）...
if not exist ".venv-mcp\Scripts\python.exe" (
  py -m venv .venv-mcp
)
".venv-mcp\Scripts\python.exe" -m pip install -r requirements-mcp.txt
if exist ".venv-mcp\Scripts\blender-mcp.exe" (
  echo 正在安装 Blender 插件（本机已装 Blender 才会成功）...
  ".venv-mcp\Scripts\blender-mcp.exe" install-addon
)
echo.
echo 完成。之后启动大眼不会再重新下载这些包。
echo Blender：打开软件 → N 键 → BlenderMCP 面板 → Start MCP Server，再让大眼操控。
pause
