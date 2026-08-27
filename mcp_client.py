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
MARKETPLACE_FILE = "mcp_marketplace.json"

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
            _kill_process_tree(self.process)
            self.process = None
            self._initialized = False
            self.tools = []

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


def _kill_process_tree(proc):
    """终止进程及其子进程（Windows 上 node/playwright 会留 chrome 子进程）。"""
    if not proc or proc.poll() is not None:
        return
    pid = proc.pid
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


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


def _config_write_path():
    for path in _config_candidates():
        if os.path.isfile(path):
            return path
    return os.path.join(_config_home(), SERVER_FILE)


def _save_config(config):
    path = _config_write_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _project_dir():
    return _config_home()


def _valid_server_name(name):
    import re
    return bool(name) and re.match(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$", name)


def _run_cmd(cmd, cwd=None, timeout=600):
    """Run subprocess; return (ok, message)."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd or _project_dir(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if r.returncode == 0:
            return True, (r.stdout or "").strip()
        err = (r.stderr or r.stdout or "").strip()
        return False, err or f"exit {r.returncode}"
    except subprocess.TimeoutExpired:
        return False, "安装超时"
    except Exception as e:
        return False, str(e)


def _ensure_venv_mcp_python():
    venv = os.path.join(_project_dir(), ".venv-mcp")
    if os.name == "nt":
        py = os.path.join(venv, "Scripts", "python.exe")
    else:
        py = os.path.join(venv, "bin", "python")
    if not os.path.isfile(py):
        ok, msg = _run_cmd([sys.executable, "-m", "venv", venv], timeout=120)
        if not ok:
            return None, f"创建 .venv-mcp 失败: {msg}"
    if not os.path.isfile(py):
        return None, "找不到 .venv-mcp 里的 Python"
    return py, None


def _npm_pkg_installed(pkg):
    """Check if an npm package exists under node_modules (ignore version suffix)."""
    name = pkg.split("@")[0] if pkg.startswith("@") is False else pkg
    # @scope/name@version → @scope/name ; name@version → name
    if pkg.startswith("@"):
        parts = pkg.split("@")
        # ['', 'scope/name'] or ['', 'scope/name', '1.2.3'] — actually split: '', 'scope', 'name' or with version
        # Better: strip last @version if present after scope
        if pkg.count("@") >= 2:
            # @scope/pkg@version
            name = "@" + pkg[1:].rsplit("@", 1)[0]
        else:
            name = pkg
    else:
        name = pkg.split("@")[0]
    path = os.path.join(_project_dir(), "node_modules", *name.split("/"))
    return os.path.isdir(path)


def _install_npm_packages(packages, post_install=None):
    if not packages:
        return True, None
    need = [p for p in packages if not _npm_pkg_installed(p)]
    if not need:
        print(f"[mcp] npm 包已存在，跳过安装: {packages}")
    else:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        print(f"[mcp] npm install {need} …")
        ok, msg = _run_cmd([npm, "install", "--save"] + list(need), timeout=600)
        if not ok:
            return False, f"npm install 失败: {msg}"
    if post_install:
        # chromium 等：已装可跳过；失败仍返回错误
        ok2, msg2 = _run_cmd(post_install, timeout=600)
        if not ok2:
            return False, f"安装后步骤失败: {msg2}"
    return True, None


def _install_pip_packages(packages):
    if not packages:
        return True, None
    py, err = _ensure_venv_mcp_python()
    if not py:
        return False, err
    ok, msg = _run_cmd([py, "-m", "pip", "install", "-U", "pip"], timeout=180)
    if not ok:
        return False, f"pip 升级失败: {msg}"
    ok, msg = _run_cmd([py, "-m", "pip", "install"] + list(packages), timeout=600)
    if not ok:
        return False, f"pip install 失败: {msg}"
    return True, None


def _load_marketplace_catalog():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, MARKETPLACE_FILE)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("items") or [])
    except Exception:
        return []


def get_mcp_marketplace():
    """市场目录 + 是否已安装。"""
    configured = (_load_config().get("servers") or {}).keys()
    items = []
    for raw in _load_marketplace_catalog():
        if not isinstance(raw, dict):
            continue
        name = raw.get("name") or raw.get("id") or ""
        items.append({
            "id": raw.get("id") or name,
            "name": name,
            "title": raw.get("title") or name,
            "description": raw.get("description", ""),
            "category": raw.get("category", "其它"),
            "install_type": raw.get("install_type", ""),
            "packages": list(raw.get("packages") or []),
            "env_vars": list(raw.get("env_vars") or []),
            "setup_hint": raw.get("setup_hint", ""),
            "installed": name in configured,
        })
    return {"items": items}


def add_mcp_server(name, server_cfg, install_type=None, packages=None, post_install=None, env=None):
    """写入 mcp_servers.json，可选安装依赖，然后重载并启用。"""
    if not _valid_server_name(name):
        return {"error": "名称须为字母开头，仅含字母数字 _ -"}
    config = _load_config()
    servers = config.setdefault("servers", {})
    if name in servers:
        return {"error": f"已存在 MCP 服务器: {name}"}
    if not isinstance(server_cfg, dict):
        return {"error": "server 配置无效"}
    entry = {
        "command": server_cfg.get("command", ""),
        "args": list(server_cfg.get("args") or []),
        "env": dict(server_cfg.get("env") or {}),
    }
    if env and isinstance(env, dict):
        for k, v in env.items():
            if v is not None and str(v).strip():
                entry["env"][k] = str(v).strip()
    if not entry["command"]:
        return {"error": "缺少启动命令 command"}

    cmd = entry["command"]
    if "blender-mcp.exe" in cmd and os.name != "nt":
        entry["command"] = ".venv-mcp/bin/blender-mcp"

    if packages:
        it = (install_type or "").lower()
        if it == "npm":
            ok, err = _install_npm_packages(packages, post_install=post_install)
        elif it == "pip":
            ok, err = _install_pip_packages(packages)
        else:
            return {"error": f"未知安装类型: {install_type}"}
        if not ok:
            return {"error": err}

    servers[name] = entry
    _save_config(config)

    global _mcp_loaded
    if _mcp_loaded:
        reload_mcp_servers()
    else:
        load_mcp_servers()

    state = _load_enabled()
    state.setdefault("servers", {})[name] = True
    _save_enabled(state)
    if is_mcp_server_enabled(name):
        _start_named(name, entry)

    status = get_mcp_status()
    status["success"] = True
    status["message"] = f"已添加 MCP: {name}"
    return status


def add_mcp_from_marketplace(item_id, env=None):
    """从市场条目一键添加。"""
    item_id = (item_id or "").strip()
    if not item_id:
        return {"error": "缺少 id"}
    catalog = _load_marketplace_catalog()
    item = None
    for raw in catalog:
        if raw.get("id") == item_id or raw.get("name") == item_id:
            item = raw
            break
    if not item:
        return {"error": f"市场里没有: {item_id}"}
    name = item.get("name") or item.get("id")
    server = item.get("server") or {}
    env_vars = item.get("env_vars") or []
    merged_env = dict(server.get("env") or {})
    user_env = env or {}
    for ev in env_vars:
        key = ev.get("key")
        if not key:
            continue
        val = user_env.get(key) or merged_env.get(key) or ""
        if ev.get("required") and not str(val).strip():
            return {"error": f"请填写 {ev.get('label') or key}"}
        if str(val).strip():
            merged_env[key] = str(val).strip()
    server = dict(server)
    server["env"] = merged_env
    return add_mcp_server(
        name,
        server,
        install_type=item.get("install_type"),
        packages=item.get("packages"),
        post_install=item.get("post_install"),
        env=merged_env,
    )


def remove_mcp_server(name):
    """从配置移除并停止进程（不删 node_modules）。"""
    name = (name or "").strip()
    config = _load_config()
    servers = config.get("servers") or {}
    if name not in servers:
        return {"error": f"未配置: {name}"}
    _stop_named(name)
    del servers[name]
    config["servers"] = servers
    _save_config(config)
    state = _load_enabled()
    if name in state.get("servers", {}):
        del state["servers"][name]
        _save_enabled(state)
    status = get_mcp_status()
    status["success"] = True
    status["message"] = f"已移除 MCP: {name}"
    return status


def _enabled_path():
    return os.path.join(_config_home(), ENABLED_FILE)


def _load_enabled():
    """UI 开关：master 总闸 + 各服务器默认开。缺文件 / 空文件 / 损坏 = 全开。"""
    path = _enabled_path()
    state = {"master": True, "servers": {}}
    if not os.path.isfile(path):
        return state
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if not text:
            return state
        raw = json.loads(text)
        if isinstance(raw, dict):
            if "master" in raw:
                state["master"] = bool(raw["master"])
            servers = raw.get("servers")
            if isinstance(servers, dict):
                state["servers"] = {str(k): bool(v) for k, v in servers.items()}
    except Exception as e:
        print(f"[mcp] 读取 {path} 失败，使用默认全开: {e}")
    return state


def _save_enabled(state):
    path = _enabled_path()
    payload = {
        "master": bool(state.get("master", True)),
        "servers": {str(k): bool(v) for k, v in (state.get("servers") or {}).items()},
    }
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        print(f"[mcp] 开关已写入 {path}: master={payload['master']}")
    except Exception as e:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        print(f"[mcp] 写入 {path} 失败: {e}")
        raise


def is_mcp_server_enabled(name):
    state = _load_enabled()
    if not state.get("master", True):
        return False
    return bool(state.get("servers", {}).get(name, True))


def _stop_named(name):
    with _mcp_lock:
        srv = _mcp_servers.pop(name, None)
    if srv:
        try:
            srv.stop()
        except Exception:
            pass


def _reconcile_mcp_state():
    """关掉开关仍留在内存里的 MCP 进程。"""
    with _mcp_lock:
        names = list(_mcp_servers.keys())
    for name in names:
        if not is_mcp_server_enabled(name):
            _stop_named(name)


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
    n = load_mcp_servers()
    _reconcile_mcp_state()
    return n


def set_mcp_master(enabled):
    """总闸：关则停掉全部；开则只拉起各服务器开关仍为开的。"""
    state = _load_enabled()
    state["master"] = bool(enabled)
    try:
        _save_enabled(state)
    except Exception as e:
        return {"error": f"无法写入 mcp_enabled.json: {e}"}
    config = _load_config().get("servers", {}) or {}
    if not enabled:
        with _mcp_lock:
            names = list(_mcp_servers.keys())
        for name in names:
            _stop_named(name)
        _reconcile_mcp_state()
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
    try:
        _save_enabled(state)
    except Exception as e:
        return {"error": f"无法写入 mcp_enabled.json: {e}"}
    if not is_mcp_server_enabled(name):
        _stop_named(name)
        _reconcile_mcp_state()
        return get_mcp_status()
    ok = _start_named(name, config[name])
    _reconcile_mcp_state()
    status = get_mcp_status()
    if not ok:
        status["error"] = f"{name} 启动失败，看控制台 [mcp] 日志"
    return status


def get_mcp_tool_defs():
    """Get flat tool definitions matching register_tool (name/description/parameters)."""
    _reconcile_mcp_state()
    defs = []
    for name, srv in _mcp_servers.items():
        if not is_mcp_server_enabled(name):
            continue
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
    _reconcile_mcp_state()
    for sname, srv in _mcp_servers.items():
        if not is_mcp_server_enabled(sname):
            continue
        prefix = f"mcp_{sname}__"
        if full_name.startswith(prefix):
            tool_name = full_name[len(prefix):]
            return srv.call_tool(tool_name, args)
    return {"error": f"未知 MCP 工具: {full_name}"}


def get_mcp_status():
    """Status for UI: configured servers + running state + tool names + 开关。"""
    _reconcile_mcp_state()
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
        effective = master and pref
        servers.append({
            "name": name,
            "running": running,
            "enabled": pref,
            "effective": effective,
            "tools": tools if effective else [],
            "tool_count": len(tools) if effective else 0,
            "command": cfg.get("command", ""),
            "args": list(cfg.get("args") or []),
        })
    for name, srv in _mcp_servers.items():
        if name in seen:
            continue
        running = bool(srv.process is not None and srv.process.poll() is None)
        tools = [t["name"] for t in srv.tools if isinstance(t, dict) and t.get("name")]
        pref = bool(prefs.get(name, True))
        effective = master and pref
        servers.append({
            "name": name,
            "running": running,
            "enabled": pref,
            "effective": effective,
            "tools": tools if effective else [],
            "tool_count": len(tools) if effective else 0,
            "command": srv.command,
            "args": list(srv.args or []),
        })
    return {
        "loaded": _mcp_loaded,
        "master": master,
        "configured_count": len(configured),
        "running_count": sum(1 for s in servers if s["running"] and s["effective"]),
        "enabled_path": _enabled_path(),
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
