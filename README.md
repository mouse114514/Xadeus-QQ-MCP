# Xadeus-QQ-MCP

> Built by a 15-year-old developer, based on [Amadeus-QQ-MCP](https://github.com/Sakura325/Amadeus-QQ-MCP).

QQ MCP (Model Context Protocol) Server — connects to QQ via NapCatQQ (OneBot v11), giving AI agents direct control over QQ (send/receive messages, group management, auto-wake on incoming messages, and more).

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-green)]()

---

## Features

| Feature | Description |
|---------|-------------|
| **Auto-Wake** | Incoming QQ messages matching rules trigger the AI agent window automatically. **Supports any client** — opencode, Cursor, Claude Desktop, Windsurf — configure via `set_wake_config` |
| **Dedup Lock** | Prevents duplicate wake during processing. Unlock manually when done |
| **Persistent Rules** | Wake rules saved to `wake_rules.json`, auto-loaded on restart |
| **`wait_for_reply` Built-in** | `send_message` waits for reply by default |
| **Group Moderation** | Mute, unmute, kick, set card, send notices |
| **File Sending** | Send any file from URL to QQ groups/private chats |
| **Message Recall** | Recall bot-sent messages |
| **Timer Scheduler** | Cron or interval-based timed wake |
| **One-Click Setup** | Auto-detect NapCat, configure ports, update agent configs |

## Architecture

```
QQ ←→ NapCat (OneBot v11)
          ↓  WebSocket :3001 / HTTP :3000
    Xadeus-QQ-MCP (Python MCP Server)
          ↓  MCP Protocol
    AI Agent (opencode / Cursor / Claude / ...)
```

## Quick Start

### Prerequisites

1. Install [NapCat.Shell](https://github.com/NapNeko/NapCatQQ) and QQ
2. Configure NapCat OneBot v11 (WebSocket :3001, HTTP :3000)

### One-Click Setup (Recommended)

```powershell
.\quickstart.ps1 -qq 你的QQ号
```

Auto-detects NapCat, configures HTTP:3000/WS:3001, creates Python venv,
writes QQ_OVERRIDE, generates NapCat start script, sets window wake patterns.

Options:
```powershell
.\quickstart.ps1 -qq 123456 -windowTitle "OC,opencode,cmd"   # Custom window patterns
.\quickstart.ps1 -configFile config.json                      # Config file mode
.\quickstart.ps1 -restart                                     # Kill stale MCP + wait for recovery
```

Default `-windowTitle`: `OC,opencode,Administrator,cmd,管理员`
(comma-separated substrings, matched case-insensitively against window titles)

Also available via Python:
```bash
python setup.py          # Interactive mode
python setup.py --qq YOUR_QQ --fast   # Non-interactive
```

### Manual Setup

```bash
git clone https://github.com/mouse114514/Xadeus-QQ-MCP
cd Xadeus-QQ-MCP

# Virtual env
uv venv
uv sync

# Start MCP Server
uv run python -m qq_agent_mcp --qq YOUR_QQ
```

### Configure Your AI Agent

> **Important**: The `--qq` argument passed by your AI agent config is **ignored** at runtime.
> Instead, set `QQ_OVERRIDE` environment variable before starting your agent:
> ```powershell
> $env:QQ_OVERRIDE = "YOUR_QQ"
> ```
> Or edit the fallback value in `src/qq_agent_mcp/__main__.py:QQ_OVERRIDE`.
>
> This works around AI agents that cache the MCP command at startup
> and ignore subsequent config file changes.

**opencode** — edit `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "qq-agent": {
      "type": "local",
      "command": [
        "C:\\path\\to\\.venv\\Scripts\\python.exe",
        "-m", "qq_agent_mcp",
        "--qq", "YOUR_QQ"
      ],
      "enabled": true,
      "timeout": 120000
    }
  }
}
```

**Cursor / Claude Desktop / Windsurf** — edit the respective MCP config file:

```json
{
  "mcpServers": {
    "qq-agent": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "qq_agent_mcp", "--qq", "YOUR_QQ"],
      "enabled": true
    }
  }
}
```

*Or just run `python setup.py` — it detects all installed agents automatically.*

### Wake Target Configuration

Use `set_wake_config` to bind to any AI agent window:

```json
{
  "window_title_patterns": ["opencode", "cursor", "claude"],
  "focus_shortcut": "ctrl+l"
}
```

Saved to `src/qq_agent_mcp/wake_config.json`.

## MCP Tools

### Messaging
| Tool | Description |
|------|-------------|
| `send_message` | Send text with segmentation and optional reply wait |
| `send_image` | Send image (base64) |
| `send_voice` | Send voice (base64) |
| `wait_for_reply` | Wait for new messages |

### Context & History
| Tool | Description |
|------|-------------|
| `get_recent_context` | Recent messages for a group/friend |
| `batch_get_recent_context` | Batch query multiple targets |
| `screenshot_chat` | Render chat screenshot (iPhone dark mode style) |
| `compress_context` | Archive old messages to summary |

### Wake System
| Tool | Description |
|------|-------------|
| `add_wake_rule` | Add auto-wake rule (group/private + keywords) |
| `remove_wake_rule` | Remove wake rule by index |
| `list_wake_rules` | List all wake rules |
| `set_wake_pending` | Lock/unlock wake to prevent duplicates |
| `set_wake_enabled` | Enable/disable all or specific rules |
| `set_wake_config` | Configure target window and focus shortcut |
| `get_wake_config` | View current wake config |
| `diagnose_wake` | Debug wake monitor state |

### Group Management
| Tool | Description |
|------|-------------|
| `get_group_list` | List joined groups |
| `get_group_member_list` | List group members |
| `get_group_member_info` | Get member details |
| `mute_member` | Mute a member |
| `unmute_member` | Unmute a member |
| `kick_member` | Kick a member |
| `set_member_card` | Set group nickname |
| `send_group_notice` | Send group notice |

### System
| Tool | Description |
|------|-------------|
| `check_status` | Check QQ and NapCat connection |
| `get_friend_list` | List friends |
| `recall_message` | Recall bot message |
| `send_file` | Send file from URL |
| `add_timer` | Add scheduled wake (cron/interval) |
| `remove_timer` | Remove timer |
| `list_timers` | List all timers |

## Known Issues & Workarounds

| Issue | Cause | Workaround |
|-------|-------|------------|
| **MCP won't restart** after crash/kill | opencode has restart backoff; after ~3 kills it stops retrying | Restart your AI agent, or `.\quickstart.ps1 -restart` |
| **Two MCP processes** always appear | FastMCP stdio transport spawns parent+child chain | Named mutex prevents duplicate typing (built-in fix) |
| **Config changes ignored** after editing `opencode.json` | opencode caches MCP command at startup | Restart opencode, or use `QQ_OVERRIDE` env var |
| **Wake message doubled** | Both processes try to type simultaneously | Fixed via Windows named mutex (`Local\XadeusQQ_MCP_WakeTyping`) |
| **Messages from same QQ ignored** | `is_self` filter in context.py and wake.py | Removed in current build |
| **Wake won't fire** even with matching rule | Lock (`_pending`) held from previous wake | Auto-unlock after 5 min, or call `set_wake_pending(false)` |

### Restart Helper

```powershell
.\quickstart.ps1 -restart
```

Kills stale MCP processes and waits for auto-restart. If the agent doesn't
recover, it prompts you to restart manually.

## Tech Stack

- Python 3.12+ (httpx, aiohttp, FastMCP)
- NapCat.Shell (QQ + OneBot v11)
- Win32 API (ctypes) — window activation, keyboard simulation

## License

Based on Amadeus-QQ-MCP (MIT License).

---

> **中文版**

# Xadeus-QQ-MCP

> **由一位 15 岁高中生基于 [Amadeus-QQ-MCP](https://github.com/Sakura325/Amadeus-QQ-MCP) 深度优化改造。**

QQ MCP (Model Context Protocol) Server — 通过 NapCatQQ (OneBot v11) 协议连接 QQ，为 AI Agent 提供直接操控 QQ 的能力（收发消息、管理群聊、自动唤醒等）。

---

## 功能亮点

| 功能 | 说明 |
|------|------|
| **Auto-Wake 自动唤醒** | QQ 消息匹配规则时自动激活 AI Agent 窗口。**支持任意客户端** — opencode、Cursor、Claude Desktop、Windsurf，通过 `set_wake_config` 配置窗口标题即可 |
| **Pending 防重复锁** | 唤醒后自动上锁，防止重复唤醒。Agent 干完活后手动解锁 |
| **规则持久化** | 唤醒规则自动保存到 `wake_rules.json`，重启后自动加载 |
| **`wait_for_reply` 一体化** | `send_message` 默认自动等待回复 |
| **群管理** | 禁言、解禁、踢人、设名片、发公告 |
| **文件发送** | 从 URL 下载文件发送到群/私聊 |
| **消息撤回** | 撤回机器人发送的消息 |
| **定时任务** | 支持 cron 和间隔两种模式的定时唤醒 |
| **一键配置** | 自动检测 NapCat、端口、多 Agent 配置 |

## 架构

```
QQ ←→ NapCat (OneBot v11)
          ↓  WebSocket :3001 / HTTP :3000
    Xadeus-QQ-MCP (Python MCP Server)
          ↓  MCP 协议
    AI Agent (opencode / Cursor / Claude / ...)
```

## 快速开始

### 前置条件

1. 安装 [NapCat.Shell](https://github.com/NapNeko/NapCatQQ) 和 QQ
2. 配置 NapCat OneBot v11 (WebSocket :3001, HTTP :3000)

### 一键配置（推荐）

```powershell
.\quickstart.ps1 -qq 你的QQ号
```

自动检测 NapCat、配置 HTTP:3000/WS:3001、创建 Python venv、
写入 QQ_OVERRIDE、生成 NapCat 启动脚本、设置窗口唤醒匹配模式。

选项：
```powershell
.\quickstart.ps1 -qq 123456 -windowTitle "OC,opencode,cmd"   # 自定义窗口匹配模式
.\quickstart.ps1 -configFile config.json                      # 配置文件模式
.\quickstart.ps1 -restart                                     # 杀死残留 MCP + 等待恢复
```

默认 `-windowTitle`：`OC,opencode,Administrator,cmd,管理员`
（逗号分隔，不区分大小写子串匹配窗口标题）

Python 版（功能相同）：
```bash
python setup.py                           # 交互模式
python setup.py --qq 你的QQ号 --fast       # 静默模式
```

### 手动安装

```bash
git clone https://github.com/mouse114514/Xadeus-QQ-MCP
cd Xadeus-QQ-MCP

# 虚拟环境
uv venv
uv sync

# 启动 MCP Server
uv run python -m qq_agent_mcp --qq 你的QQ号
```

### 配置 AI Agent

> **重要**：AI Agent 配置中的 `--qq` 参数在运行时**会被忽略**。
> 正确方式：启动 Agent 前设置环境变量：
> ```powershell
> $env:QQ_OVERRIDE = "你的QQ号"
> ```
> 或直接修改 `src/qq_agent_mcp/__main__.py:QQ_OVERRIDE` 的默认值。
>
> 这样做是为了绕过 AI Agent 缓存 MCP 命令的问题——
> Agent 只在启动时读取一次配置，改配置文件不生效。

**opencode** — 编辑 `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "qq-agent": {
      "type": "local",
      "command": [
        "C:\\path\\to\\.venv\\Scripts\\python.exe",
        "-m", "qq_agent_mcp",
        "--qq", "你的QQ号"
      ],
      "enabled": true,
      "timeout": 120000
    }
  }
}
```

**Cursor / Claude Desktop / Windsurf** — 编辑对应 MCP 配置文件:

```json
{
  "mcpServers": {
    "qq-agent": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "qq_agent_mcp", "--qq", "你的QQ号"],
      "enabled": true
    }
  }
}
```

*运行 `python setup.py` 可自动检测并配置所有已安装的 Agent。*

### 唤醒目标配置

通过 `set_wake_config` 配置窗口标题，即可绑定到任意 AI Agent：

```json
{
  "window_title_patterns": ["opencode", "cursor", "claude"],
  "focus_shortcut": "ctrl+l"
}
```

配置文件保存在 `src/qq_agent_mcp/wake_config.json`。

## MCP 工具一览

### 消息
| 工具 | 说明 |
|------|------|
| `send_message` | 发文本消息，支持分段、等待回复 |
| `send_image` | 发图片 |
| `send_voice` | 发语音 |
| `wait_for_reply` | 等待新消息 |

### 上下文
| 工具 | 说明 |
|------|------|
| `get_recent_context` | 查看最近消息 |
| `batch_get_recent_context` | 批量查看多目标 |
| `screenshot_chat` | 生成聊天截图（iPhone 深色模式） |
| `compress_context` | 压缩缓存 |

### 唤醒系统
| 工具 | 说明 |
|------|------|
| `add_wake_rule` | 添加唤醒规则（群/私聊 + 关键词） |
| `remove_wake_rule` | 删除唤醒规则 |
| `list_wake_rules` | 查看所有规则 |
| `set_wake_pending` | 锁定/解锁唤醒 |
| `set_wake_enabled` | 启用/禁用规则 |
| `set_wake_config` | 配置窗口标题和快捷键 |
| `get_wake_config` | 查看唤醒配置 |
| `diagnose_wake` | 诊断唤醒状态 |

### 群管理
| 工具 | 说明 |
|------|------|
| `get_group_list` | 群列表 |
| `get_group_member_list` | 群成员列表 |
| `get_group_member_info` | 成员详情 |
| `mute_member` | 禁言 |
| `unmute_member` | 解禁 |
| `kick_member` | 踢出 |
| `set_member_card` | 设群名片 |
| `send_group_notice` | 发群公告 |

### 系统
| 工具 | 说明 |
|------|------|
| `check_status` | 检查连接状态 |
| `get_friend_list` | 好友列表 |
| `recall_message` | 撤回消息 |
| `send_file` | 发送文件 |
| `add_timer` | 添加定时任务 |
| `remove_timer` | 删除定时任务 |
| `list_timers` | 查看所有定时任务 |

## 已知问题

| 问题 | 原因 | 解决方法 |
|------|------|----------|
| **MCP 被杀后无法自启** | opencode 有重启退避策略 | 重启 AI Agent，或 `.\quickstart.ps1 -restart` |
| **总是有两个 MCP 进程** | FastMCP stdio 产生父子进程链 | 内置命名互斥锁解决重复打字 |
| **改 opencode.json 不生效** | opencode 启动时缓存命令 | 重启 opencode，或用 `QQ_OVERRIDE` 环境变量 |
| **唤醒消息出现双倍字符** | 两个进程同时打字 | 已修复（Windows 命名互斥锁） |
| **同 QQ 号发消息不唤醒** | `is_self` 过滤器阻挡 | 已修复（移除 context.py/wake.py 过滤） |
| **匹配规则但不唤醒** | 唤醒锁 (`_pending`) 未释放 | 5 分钟自动解锁，或调用 `set_wake_pending(false)` |

### 重启助手

```powershell
.\quickstart.ps1 -restart
```

杀死残留 MCP 进程并等待自动重启。如果 Agent 不自动恢复，会提示你手动重启。

## 技术栈

- Python 3.12+ (httpx, aiohttp, FastMCP)
- NapCat.Shell (QQ + OneBot v11)
- Win32 API (ctypes) — 窗口激活、键盘模拟

## 授权

基于 Amadeus-QQ-MCP (MIT License) 改造。
