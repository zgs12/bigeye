#!/usr/bin/env python3
"""
MCP (Model Context Protocol) client for 大眼X.

Connects to MCP servers via stdio (subprocess), discovers their tools,
and registers them as native AI tools — no code changes needed.

Config: mcp_servers.json in project root.
Format:
{
  "servers": {
    "filesystem": {
      "command": "node",
      "args": ["node_modules/@modelcontextprotocol/server-filesystem/dist/index.js", "."],
      "env": {}
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-server-github"],
      "env": {"GITHUB_TOKEN": "..."}
    },
    "playwright": {
      "command": "node",
      "args": ["node_modules/@playwright/mcp/cli.js"],
      "env": {}
    },
    "blender": {
      "command": ".venv-mcp/Scripts/blender-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

SERVER_FILE = "mcp_servers.json"
ENABLED_FILE = "mcp_enabled.json"

# ── JSON-RPC helpers ──────────────────────────────────

def _rpc_request(method, params=None, rid=None):
    return {
        "jsonrpc": "2.0",
        "id": rid or str(uuid.uuid4())[:8],
        "method": method,
        "params": params or {},
    }

def _rpc_notification(method, params=None):
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    }

# ── MCP Server process ───────────────────────────────

class MCPServer:
    """Manages one MCP server subprocess."""

    def __init__(self, name, config):
        self.name = name
        self.command = config.get("command", "")
        self.args = config.get("args", [])
        self.env = config.get("env", {})
        self.process = None
        self.tools = []          # list of tool defs from server
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending = {}       # id -> threading.Event + result
        self._reader_thread = None
        self._err_thread = None
        self._initialized = False

    def start(self):
        if self.process and self.process.poll() is None:
            return True
        try:
            merged_env = {**os.environ, **self.env}
            cmd = [self.command] + list(self.args)
            # Windows 上 npx/npm 是 .cmd，不经 shell 直接 Popen 会 FileNotFound
            if os.name == "nt" and cmd:
                c0 = cmd[0].lower()
                if c0 == "npx":
                    cmd[0] = "npx.cmd"
                elif c0 == "npm":
                    cmd[0] = "npm.cmd"
                elif c0 == "node":
                    cmd[0] = "node.exe"
            project_dir = os.path.dirname(os.path.abspath(__file__))
            # 相对路径命令（如 .venv-mcp/Scripts/python.exe）改成绝对路径
            if cmd and not os.path.isabs(cmd[0]):
                cand = os.path.normpath(os.path.join(project_dir, cmd[0]))
                if os.path.isfile(cand):
                    cmd[0] = cand
                elif os.name == "nt" and os.path.isfile(cand + ".exe"):
                    cmd[0] = cand + ".exe"
            # 本地 node_modules 脚本改成绝对路径；缺文件时提示先装一次
            if len(cmd) > 1 and not os.path.isabs(cmd[1]) and cmd[1].replace("\\", "/").startswith("node_modules/"):
                cmd[1] = os.path.normpath(os.path.join(project_dir, cmd[1]))
                if not os.path.isfile(cmd[1]):
                    print(f"[mcp] {self.name}: 找不到 {cmd[1]}，请先运行 mcp_setup.bat（只需一次）")
                    return False
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=project_dir,
            )
            self._reader_thread = threading.Thread(target=self._reader, daemon=True)
            self._reader_thread.start()
            # Playwright 等会往 stderr 打日志，不读会把管道撑满导致卡死
            self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self._err_thread.start()
            # 本地安装后启动很快；首次拉起浏览器仍可能稍慢
            resp = self._call("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "大眼X", "version": "1.0"},
            }, timeout=180)
            if not resp or "error" in resp:
                print(f"[mcp] {self.name}: init failed: {resp}")
                return False
            # Send initialized notification
            self._send(_rpc_notification("notifications/initialized"))
            # Discover tools
            tools_resp = self._call("tools/list", timeout=60)
            if tools_resp and "result" in tools_resp:
                self.tools = tools_resp["result"].get("tools", [])
                print(f"[mcp] {self.name}: {len(self.tools)} tools discovered")
            self._initialized = True
            return True
        except Exception as e:
            print(f"[mcp] {self.name}: failed to start: {e}")
            return False

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
            self.process = None
            self._initialized = False

    def _send(self, msg):
        if not self.process or self.process.poll() is not None:
            return
        try:
            line = json.dumps(msg, ensure_ascii=False) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except Exception:
            pass

    def _drain_stderr(self):
        """Keep stderr from filling the OS pipe buffer."""
        try:
            for line in self.process.stderr:
                line = line.rstrip()
                if line:
                    print(f"[mcp:{self.name}] {line}")
        except Exception:
            pass

    def _reader(self):
        """Read JSON-RPC responses from stdout."""
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = msg.get("id")
                if rid and rid in self._pending:
                    event = self._pending[rid][1]
                    self._pending[rid] = (msg, event)
                    event.set()
        except Exception:
            pass

    def _call(self, method, params=None, timeout=30):
        rid = str(uuid.uuid4())[:8]
        event = threading.Event()
        self._pending[rid] = (None, event)
        self._send(_rpc_request(method, params, rid))
        if event.wait(timeout):
            result, _ = self._pending.pop(rid, (None, None))
            return result
        self._pending.pop(rid, None)
        return {"error": "timeout"}

    def call_tool(self, tool_name, arguments, timeout=120):
        resp = self._call("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, timeout=timeout)
        if not resp:
            return {"error": f"{self.name}: 无响应"}
        if "error" in resp:
            return {"error": f"{self.name}: {resp['error']}"}
        result = resp.get("result", {})
        content = result.get("content", [])
        if not content:
            return {"result": str(result)}
        # Extract text from content blocks
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "resource":
                    texts.append(f"[resource: {block.get('resource', {})}]")
        return {"result": "\n".join(texts) if texts else str(result)}


# ── Global manager ────────────────────────────────────

_mcp_servers: dict[str, MCPServer] = {}
_mcp_loaded = False
_mcp_lock = threading.Lock()


def _config_candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(here, SERVER_FILE),
        os.path.join(os.path.dirname(here), SERVER_FILE),
    ]
    if getattr(sys, 'frozen', False):
        paths.append(os.path.join(os.path.dirname(sys.executable), SERVER_FILE))
    return paths


def _config_home():
    """Directory of mcp_servers.json, else this file's directory."""
    for path in _config_candidates():
        if os.path.isfile(path):
            return os.path.dirname(path)
    return os.path.dirname(os.path.abspath(__file__))


def _load_config():
    """Find mcp_servers.json next to this file (project root), then parent, then exe dir."""
    for path in _config_candidates():
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return {"servers": {}}


def _enabled_path():
    return os.path.join(_config_home(), ENABLED_FILE)


def _load_enabled():
    """UI 开关：master 总闸 + 各服务器默认开。缺文件 = 全开。"""
    path = _enabled_path()
    state = {"master": True, "servers": {}}
    if not os.path.isfile(path):
        return state
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            if "master" in raw:
                state["master"] = bool(raw["master"])
            servers = raw.get("servers")
            if isinstance(servers, dict):
                state["servers"] = {str(k): bool(v) for k, v in servers.items()}
    except Exception:
        pass
    return state


def _save_enabled(state):
    path = _enabled_path()
    payload = {
        "master": bool(state.get("master", True)),
        "servers": {str(k): bool(v) for k, v in (state.get("servers") or {}).items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def is_mcp_server_enabled(name):
    state = _load_enabled()
    if not state.get("master", True):
        return False
    return bool(state.get("servers", {}).get(name, True))


def _stop_named(name):
    srv = _mcp_servers.pop(name, None)
    if srv:
        try:
            srv.stop()
        except Exception:
            pass


def _start_named(name, cfg):
    if name in _mcp_servers:
        srv = _mcp_servers[name]
        if srv.process is not None and srv.process.poll() is None:
            return True
        _stop_named(name)
    srv = MCPServer(name, cfg)
    if srv.start():
        _mcp_servers[name] = srv
        return True
    return False


def load_mcp_servers():
    """Start enabled MCP servers and discover tools. Idempotent."""
    global _mcp_loaded
    with _mcp_lock:
        if _mcp_loaded:
            return len(_mcp_servers)
        config = _load_config()
        for name, cfg in config.get("servers", {}).items():
            if name in _mcp_servers:
                continue
            if not is_mcp_server_enabled(name):
                print(f"[mcp] {name}: skipped (disabled)")
                continue
            srv = MCPServer(name, cfg)
            if srv.start():
                _mcp_servers[name] = srv
        _mcp_loaded = True
        return len(_mcp_servers)


def reload_mcp_servers():
    """Stop running MCP servers and start from current mcp_servers.json (respects switches)."""
    global _mcp_loaded
    with _mcp_lock:
        for srv in list(_mcp_servers.values()):
            try:
                srv.stop()
            except Exception:
                pass
        _mcp_servers.clear()
        _mcp_loaded = False
    return load_mcp_servers()


def set_mcp_master(enabled):
    """总闸：关则停掉全部；开则只拉起各服务器开关仍为开的。"""
    state = _load_enabled()
    state["master"] = bool(enabled)
    _save_enabled(state)
    config = _load_config().get("servers", {}) or {}
    if not enabled:
        with _mcp_lock:
            names = list(_mcp_servers.keys())
        for name in names:
            _stop_named(name)
        return get_mcp_status()
    for name, cfg in config.items():
        if is_mcp_server_enabled(name):
            _start_named(name, cfg)
    return get_mcp_status()


def set_mcp_server_enabled(name, enabled):
    """单个服务器开关。关：停进程、对话里不再出现其工具。开：立刻启动。"""
    config = _load_config().get("servers", {}) or {}
    if name not in config:
        return {"error": f"未配置 MCP 服务器: {name}"}
    state = _load_enabled()
    state.setdefault("servers", {})[name] = bool(enabled)
    _save_enabled(state)
    if not is_mcp_server_enabled(name):
        _stop_named(name)
        return get_mcp_status()
    ok = _start_named(name, config[name])
    status = get_mcp_status()
    if not ok:
        status["error"] = f"{name} 启动失败，看控制台 [mcp] 日志"
    return status


def get_mcp_tool_defs():
    """Get flat tool definitions matching register_tool (name/description/parameters)."""
    defs = []
    for name, srv in _mcp_servers.items():
        for tool in srv.tools:
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            defs.append({
                "name": f"mcp_{name}__{tool['name']}",
                "description": f"[MCP:{name}] {tool.get('description', '')}",
                "parameters": schema,
            })
    return defs


def execute_mcp_tool(full_name, args):
    """Execute an MCP tool. full_name format: mcp_{server}__{tool}"""
    for sname, srv in _mcp_servers.items():
        prefix = f"mcp_{sname}__"
        if full_name.startswith(prefix):
            tool_name = full_name[len(prefix):]
            return srv.call_tool(tool_name, args)
    return {"error": f"未知 MCP 工具: {full_name}"}


def get_mcp_status():
    """Status for UI: configured servers + running state + tool names + 开关。"""
    config = _load_config()
    configured = config.get("servers", {}) or {}
    state = _load_enabled()
    master = bool(state.get("master", True))
    prefs = state.get("servers") or {}
    servers = []
    seen = set()
    for name, cfg in configured.items():
        seen.add(name)
        srv = _mcp_servers.get(name)
        running = bool(srv and srv.process is not None and srv.process.poll() is None)
        tools = []
        if srv and srv.tools:
            for t in srv.tools:
                if isinstance(t, dict) and t.get("name"):
                    tools.append(t["name"])
        cfg = cfg if isinstance(cfg, dict) else {}
        pref = bool(prefs.get(name, True))
        servers.append({
            "name": name,
            "running": running,
            "enabled": pref,
            "effective": master and pref,
            "tools": tools,
            "tool_count": len(tools),
            "command": cfg.get("command", ""),
            "args": list(cfg.get("args") or []),
        })
    for name, srv in _mcp_servers.items():
        if name in seen:
            continue
        running = bool(srv.process is not None and srv.process.poll() is None)
        tools = [t["name"] for t in srv.tools if isinstance(t, dict) and t.get("name")]
        pref = bool(prefs.get(name, True))
        servers.append({
            "name": name,
            "running": running,
            "enabled": pref,
            "effective": master and pref,
            "tools": tools,
            "tool_count": len(tools),
            "command": srv.command,
            "args": list(srv.args or []),
        })
    return {
        "loaded": _mcp_loaded,
        "master": master,
        "configured_count": len(configured),
        "running_count": sum(1 for s in servers if s["running"]),
        "servers": servers,
    }


def list_mcp_servers():
    """List running MCP servers and their tools."""
    result = {}
    for name, srv in _mcp_servers.items():
        result[name] = {
            "tools": [t["name"] for t in srv.tools],
            "running": srv.process is not None and srv.process.poll() is None,
        }
    return result
