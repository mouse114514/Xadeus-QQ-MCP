#!/usr/bin/env python3
"""Xadeus-QQ-MCP 一键配置脚本

用法:
  python setup.py                            # 交互式
  python setup.py --qq 123456 --fast         # 快速配置（用检测到的值，跳过确认）
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


# ── Agent 检测 ──────────────────────────────────────────────────────

AGENT_CONFIGS = {
    "opencode": {
        "name": "opencode",
        "label": "opencode (桌面版)",
        "path": os.path.expanduser("~/.config/opencode/opencode.json"),
        "key_path": ["mcp", "qq-agent"],
    },
    "cursor": {
        "name": "cursor",
        "label": "Cursor",
        "path": ".cursor/mcp.json",
        "key_path": ["mcpServers", "qq-agent"],
    },
    "cursor_global": {
        "name": "cursor",
        "label": "Cursor (全局)",
        "path": os.path.expanduser("~/.cursor/mcp.json"),
        "key_path": ["mcpServers", "qq-agent"],
    },
    "claude": {
        "name": "claude",
        "label": "Claude Desktop",
        "path": os.path.expanduser("~/.config/claude/claude_desktop_config.json"),
        "key_path": ["mcpServers", "qq-agent"],
    },
    "windsurf": {
        "name": "windsurf",
        "label": "Windsurf",
        "path": os.path.expanduser("~/.windsurf/config.json"),
        "key_path": ["mcpServers", "qq-agent"],
    },
}


# ── 工具函数 ────────────────────────────────────────────────────────


def ask(question: str, default: str = "") -> str:
    if default:
        prompt = f"{question} [{default}]: "
    else:
        prompt = f"{question}: "
    try:
        return input(prompt).strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def confirm(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    ans = input(f"{question} [{hint}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def find_napcat() -> str | None:
    candidates = [
        os.path.expanduser("~/Desktop/work/napcat-qq"),
        os.path.expanduser("~/Desktop/napcat-qq"),
        r"C:\NapCatQQ",
        r"C:\Program Files\NapCatQQ",
    ]
    for p in candidates:
        if os.path.isfile(os.path.join(p, "NapCatWinBootMain.exe")):
            return p
    return None


def find_version_dir(napcat_path: str) -> str | None:
    versions = os.path.join(napcat_path, "versions")
    if not os.path.isdir(versions):
        return None
    for d in sorted(os.listdir(versions), reverse=True):
        full = os.path.join(versions, d)
        if os.path.isdir(full) and os.path.isfile(
            os.path.join(full, "resources", "app", "napcat", "napcat.mjs")
        ):
            return d
    return None


def detect_existing_ports(napcat_path: str, version_dir: str) -> dict:
    """从已存在的 onebot11_*.json 中读取端口配置。"""
    config_dir = os.path.join(
        napcat_path, "versions", version_dir, "resources", "app", "napcat", "config"
    )
    result = {"http": None, "ws": None, "qq": None}
    if not os.path.isdir(config_dir):
        return result
    for f in os.listdir(config_dir):
        if f.startswith("onebot11_") and f.endswith(".json"):
            fp = os.path.join(config_dir, f)
            try:
                with open(fp, encoding="utf-8") as fh:
                    data = json.load(fh)
                servers = data.get("network", {}).get("httpServers", [])
                for s in servers:
                    if s.get("enable") and s.get("host") != "0.0.0.0":
                        result["http"] = s["port"]
                servers = data.get("network", {}).get("websocketServers", [])
                for s in servers:
                    if s.get("enable"):
                        result["ws"] = s["port"]
                # extract qq from filename
                qq_part = f.replace("onebot11_", "").replace(".json", "")
                result["qq"] = qq_part
            except Exception:
                continue
    return result


def detect_agents() -> list[dict]:
    """检测本机已安装的 AI agent 及 MCP 配置。"""
    found = []
    for key, info in AGENT_CONFIGS.items():
        path = info["path"]
        # relative path -> resolve relative to project root
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        exists = os.path.isfile(path)
        has_existing = False
        if exists:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                # navigate key_path
                val = data
                for k in info["key_path"]:
                    if isinstance(val, dict):
                        val = val.get(k, {})
                    else:
                        val = {}
                        break
                has_existing = bool(val)
            except Exception:
                pass
        found.append({
            "key": key,
            "info": info,
            "exists": exists,
            "has_existing": has_existing,
            "path": path,
        })
    return found


# ── 配置生成 ────────────────────────────────────────────────────────


def build_run_bat(napcat_dir: str, qq: str, version: str) -> str:
    napcat_internal = os.path.join(
        napcat_dir, "versions", version, "resources", "app", "napcat"
    )
    napcat_internal_posix = napcat_internal.replace("\\", "/")
    load_js = napcat_internal_posix + "/loadNapCat.js"
    napcat_mjs = f"file:///{napcat_internal_posix}/napcat.mjs"
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title NapCatQQ",
        f'cd /d "{napcat_dir}"',
        f'> "{load_js}" echo (async () => {{await import("{napcat_mjs}".replace(/\\\\/g,"/"))}})()',
        f'start "NapCatQQ" "{os.path.join(napcat_dir, "NapCatWinBootMain.exe")}" "QQ.exe" -q {qq}',
        "exit",
    ]
    return "\r\n".join(lines)


def build_onebot11(http_port: int, ws_port: int) -> dict:
    return {
        "network": {
            "httpServers": [
                {
                    "name": "http",
                    "enable": True,
                    "port": http_port,
                    "host": "0.0.0.0",
                }
            ],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [
                {"name": "ws", "enable": True, "port": ws_port, "host": "0.0.0.0"}
            ],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
        "timeout": {
            "baseTimeout": 10000,
            "uploadSpeedKBps": 256,
            "downloadSpeedKBps": 256,
            "maxTimeout": 1800000,
        },
    }


def build_mcp_entry(qq: str, http_port: int, ws_port: int, groups: list[str] | None, friends: list[str] | None, agent_key: str | None = None) -> dict:
    python_exe = _find_python()
    cmd = [
        python_exe,
        "-m", "qq_agent_mcp",
        "--qq", qq,
        "--napcat-port", str(http_port),
        "--ws-port", str(ws_port),
    ]
    if groups:
        cmd += ["--groups", ",".join(groups)]
    if friends:
        cmd += ["--friends", ",".join(friends)]
    entry_type = "local" if (agent_key or "").startswith("opencode") else "stdio"
    return {
        "type": entry_type,
        "command": cmd[0],
        "args": cmd[1:],
        "enabled": True,
        "timeout": 120000,
    }


def _find_python() -> str:
    """从项目目录找 Python，优先虚拟环境。"""
    root = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(root, ".venv", "Scripts", "python.exe"),
        os.path.join(root, "venv", "Scripts", "python.exe"),
        shutil.which("python3") or "",
        shutil.which("python") or "",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    # last resort: whatever python this is running
    return sys.executable


# ── 写入 ────────────────────────────────────────────────────────────


def write_file(path: str, content: str, force: bool) -> bool:
    if os.path.isfile(path) and not force:
        if not confirm(f"  {path} 已存在，覆盖?", False):
            print(f"  - 跳过 {path}")
            return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(content.encode("utf-8"))
    print(f"  + {path}")
    return True


def write_json(path: str, data: dict, force: bool) -> bool:
    if os.path.isfile(path) and not force:
        if not confirm(f"  {path} 已存在，覆盖?", False):
            print(f"  - 跳过 {path}")
            return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  + {path}")
    return True


def update_agent_config(path: str, agent_key: str, entry: dict, force: bool) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    if not force and not confirm(f"  更新 {path} 中的 qq-agent 配置?", True):
        print(f"  - 跳过 {path}")
        return False

    # opencode format: { "mcp": { "qq-agent": {...} } }
    # others: { "mcpServers": { "qq-agent": {...} } }
    is_opencode = agent_key.startswith("opencode")
    if is_opencode:
        data.setdefault("mcp", {})["qq-agent"] = entry
        # cleanup any leftover from previous runs
        data.get("mcpServers", {}).pop("qq-agent", None)
        if not data.get("mcpServers"):
            data.pop("mcpServers", None)
    else:
        data.setdefault("mcpServers", {})["qq-agent"] = entry

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  + {path} (已更新)")
    return True


# ── 主流程 ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Xadeus-QQ-MCP 一键配置")
    parser.add_argument("--qq", help="机器人 QQ 号（不传则交互式输入）")
    parser.add_argument("--fast", action="store_true", help="快速模式：使用检测值，跳过所有确认")
    parser.add_argument("--napcat-path", help="NapCat 目录（不传则自动检测）")
    parser.add_argument("--http-port", type=int, help="HTTP 端口")
    parser.add_argument("--ws-port", type=int, help="WS 端口")
    parser.add_argument("--groups", help="监控群号，逗号分隔")
    parser.add_argument("--friends", help="监控好友 QQ，逗号分隔")

    args = parser.parse_args()
    force = args.fast

    print("=" * 52)
    print("  Xadeus-QQ-MCP 一键配置")
    print("=" * 52)
    print()

    # ── 1. NapCat 路径检测 ──
    napcat = args.napcat_path or find_napcat()
    if not napcat:
        print("! 未检测到 NapCat，请手动输入路径")
        napcat = ask("NapCat 安装目录", r"C:\Users\Administrator\Desktop\work\napcat-qq")
    elif not args.fast:
        print(f"  检测到 NapCat: {napcat}")
        if not confirm("使用此路径?", True):
            napcat = ask("NapCat 安装目录")
    else:
        print(f"  检测到 NapCat: {napcat}")

    if not os.path.isdir(napcat):
        print(f"! 目录不存在: {napcat}")
        sys.exit(1)

    # ── 2. 版本检测 ──
    ver = find_version_dir(napcat)
    if not ver:
        print("! 未检测到 NapCat 版本目录")
        subdirs = [d for d in os.listdir(os.path.join(napcat, "versions")) if os.path.isdir(os.path.join(napcat, "versions", d))] if os.path.isdir(os.path.join(napcat, "versions")) else []
        print(f"  可用: {', '.join(subdirs) or '(无)'}")
        ver = ask("版本目录名")
    elif not args.fast:
        print(f"  检测到版本: {ver}")
    else:
        print(f"  版本: {ver}")

    # ── 3. 端口自动检测 ──
    existing = detect_existing_ports(napcat, ver)
    if existing["http"]:
        default_http = existing["http"]
        default_ws = existing["ws"]
        print(f"  检测到已有配置: QQ={existing['qq']}, HTTP=:{default_http}, WS=:{default_ws}")
    else:
        default_http = 3000
        default_ws = 3001

    # ── 4. QQ ──
    qq = args.qq
    if not qq:
        qq = ask("机器人 QQ 号", existing.get("qq", ""))
    if not qq:
        print("! QQ 号不能为空")
        sys.exit(1)

    if not args.fast:
        http_port = int(ask("OneBot HTTP 端口", str(default_http)))
        ws_port = int(ask("OneBot WebSocket 端口", str(default_ws)))
    else:
        http_port = args.http_port or default_http
        ws_port = args.ws_port or default_ws

    groups = None
    if args.groups:
        groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    elif not args.fast:
        g = ask("监控的群号（逗号分隔，留空=全部）")
        groups = [x.strip() for x in g.split(",") if x.strip()] if g else None

    friends = None
    if args.friends:
        friends = [f.strip() for f in args.friends.split(",") if f.strip()]
    elif not args.fast:
        f = ask("监控的好友 QQ（逗号分隔，留空=全部）")
        friends = [x.strip() for x in f.split(",") if x.strip()] if f else None

    # ── 5. 生成 NapCat 配置 ──
    print()
    print("── 生成 NapCat 配置文件 ──")

    bat = build_run_bat(napcat, qq, ver)
    write_file(os.path.join(napcat, "run_napcat.bat"), bat, force)

    onebot = build_onebot11(http_port, ws_port)
    config_dir = os.path.join(napcat, "versions", ver, "resources", "app", "napcat", "config")
    write_json(os.path.join(config_dir, f"onebot11_{qq}.json"), onebot, force)

    # ── 6. 生成 start_mcp.bat ──
    print()
    print("── 生成项目辅助脚本 ──")
    python_exe = _find_python()
    root = os.path.dirname(os.path.abspath(__file__))
    mcp_cmd = (
        f'"{python_exe}" -m qq_agent_mcp --qq {qq} '
        f"--napcat-port {http_port} --ws-port {ws_port}"
    )
    if groups:
        mcp_cmd += f' --groups {",".join(groups)}'
    if friends:
        mcp_cmd += f' --friends {",".join(friends)}'
    start_bat = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "{root}"\r\n'
        f"echo Starting QQ MCP Server for QQ {qq}...\r\n"
        "echo Make sure NapCatQQ is running first!\r\n"
        "echo.\r\n"
        f"{mcp_cmd}\r\n"
        "pause\r\n"
    )
    write_file(os.path.join(root, "start_mcp.bat"), start_bat, force)

    # ── 7. 检测 AI Agent 并配置 MCP ──
    print()
    print("── 配置 AI Agent MCP ──")

    agents = detect_agents()
    valid_agents = [a for a in agents if a["exists"]]
    if not valid_agents:
        print("  未检测到已安装的 AI agent（opencode / Cursor / Claude Desktop 等）")
        print("  可手动复制 start_mcp.bat 中的命令到你的 agent MCP 配置")
    else:
        print(f"  检测到以下 agent:")
        for a in valid_agents:
            tag = " (已有 qq-agent 配置)" if a["has_existing"] else ""
            print(f"    {a['info']['label']}  {a['path']}{tag}")

        if not args.fast:
            selected_keys = []
            for a in valid_agents:
                if confirm(f"  配置 {a['info']['label']}?", a["has_existing"]):
                    selected_keys.append(a["key"])
        else:
            selected_keys = [a["key"] for a in valid_agents]

        for key in selected_keys:
            entry = build_mcp_entry(qq, http_port, ws_port, groups, friends, agent_key=key)
            info = AGENT_CONFIGS[key]
            path = info["path"]
            if not os.path.isabs(path):
                path = os.path.join(root, path)
            update_agent_config(path, key, entry, force)

    # ── 完成 ──
    print()
    print("=" * 52)
    print("  配置完成！")
    print(f"  QQ: {qq}")
    print(f"  NapCat: {napcat}")
    print(f"  HTTP: :{http_port}  WS: :{ws_port}")
    print()
    print("  后续步骤:")
    print(f"  1. 运行 {os.path.join(napcat, 'run_napcat.bat')} 启动 NapCat")
    print(f"  2. 如果更新了 opencode 配置，重启 opencode")
    print(f"  3. 或运行 start_mcp.bat 直接启动 MCP Server")
    print("=" * 52)


if __name__ == "__main__":
    main()
