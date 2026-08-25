# mcp-tools
> 连接社区 MCP 工具服务器，扩展 AI 能力。本机已装好的不要再走 npx 下载。

## 本机已经有的

`mcp_servers.json` 已配置，包在 `node_modules/`，Chromium 已下载。启动后可直接调用，**不要改回 `npx -y @playwright/mcp@latest`**（每次启动会重新联网拉包）。

| 服务器 | 工具名前缀 | 做什么 |
|--------|------------|--------|
| filesystem | `mcp_filesystem__` | 按 MCP 协议读写允许的目录 |
| playwright | `mcp_playwright__` | 操控浏览器：打开页面、点击、输入、快照 |

浏览器工具常见名字：`mcp_playwright__browser_navigate`、`mcp_playwright__browser_snapshot`、`mcp_playwright__browser_click`、`mcp_playwright__browser_type`。先 `list_tools` 看完整列表。

包缺失时让用户运行一次 `mcp_setup.bat`（或项目根目录 `npm install` + `npm run mcp:install`），然后 `hot_reload` 或重启大眼。

## 什么是 MCP

Model Context Protocol — AI 工具的标准协议。服务器是独立进程，工具会出现在列表里，格式：`mcp_服务器名__工具名`。

## 再加新 MCP（装一次，以后用本地）

1. 在项目根目录：`npm install 包名`（不要写 `@latest` 当日常启动命令）
2. 改 `mcp_servers.json`，用 `node` 跑本地入口，例如：

```json
{
  "servers": {
    "playwright": {
      "command": "node",
      "args": ["node_modules/@playwright/mcp/cli.js"],
      "env": {}
    }
  }
}
```

3. `hot_reload`（会重读配置并重启 MCP）。不行再让用户重启大眼。

仍可用 `npx` 做一次性试用，但会每次启动检查/下载，长期用应改成本地 `node_modules`。

## 常用包名（社区已迁到 @modelcontextprotocol）

| 功能 | npm 包 | 备注 |
|------|--------|------|
| 文件系统 | `@modelcontextprotocol/server-filesystem` | 本机已装 |
| 浏览器 | `@playwright/mcp` | 本机已装；不要用已过时的 puppeteer MCP |
| GitHub | `@modelcontextprotocol/server-github` | 需 `GITHUB_TOKEN` |
| PostgreSQL | `@modelcontextprotocol/server-postgres` | 连接串放 args/env |
| 更多 | https://github.com/modelcontextprotocol/servers | |

旧文档里的 `@anthropic/mcp-server-*` 已废弃，不要再写进配置。

## Python MCP 服务器

也可以用 Python 写自己的 MCP server：

```python
# my_server.py
from mcp.server import Server
server = Server("my-tools")

@server.tool()
def hello(name: str) -> str:
    return f"Hello, {name}!"
```

配置: `{"command": "python", "args": ["my_server.py"]}`
