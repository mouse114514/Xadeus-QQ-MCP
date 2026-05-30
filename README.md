# Xadeus-QQ-MCP

> **由一位 13 岁初中生基于 [Amadeus-QQ-MCP](https://github.com/Sakura325/Amadeus-QQ-MCP) 深度优化改造。**

QQ MCP (Model Context Protocol) Server — 通过 NapCatQQ (OneBot v11) 协议连接 QQ，为 AI Agent 提供直接操控 QQ 的能力（收发消息、管理群聊、自动唤醒等）。

---

## 与 Amadeus-QQ-MCP 的差异

### 新增功能

| 功能 | 说明 |
|------|------|
| **Auto-Wake 自动唤醒** | QQ 消息匹配规则时自动激活 AI Agent 窗口并粘贴消息到输入框。**支持任意客户端，通过 `set_wake_config` 配置窗口标题即可绑定到 opencode、Cursor、Claude Desktop 等** |
| **Pending 防重复锁** | 唤醒后自动上锁，防止处理过程中重复唤醒。Agent 干完活后手动解锁，完全掌控唤醒节奏 |
| **规则持久化** | 唤醒规则自动保存到 `wake_rules.json`，重启 MCP Server 后自动加载 |
| **`wait_for_reply` 一体化** | `send_message` 默认自动等待回复，无需额外调用 `wait_for_reply` |
| **SKILL.md 指南** | 完整的 Agent 操作手册，规范对话流程 |

### 优化改进

- **回调式消息驱动**：不再轮询，消息入库时直接触发，零延迟
- **Win32 原生窗口激活**：`SetForegroundWindow` + `AttachThreadInput`，绕过 UIPI 限制
- **PowerShell 剪贴板方案**：可靠处理 Unicode 文本粘贴到 opencode
- **竞态修复**：`_pending` 在 `create_task` 前设置，杜绝并发穿锁
- **完整中文文档**：README、SKILL.md 均为中文

---

## 技术栈

- Python 3.12+ (httpx, aiohttp, FastMCP)
- NapCat.Shell 4.18.4 (QQ 9.9.26-44343)
- OneBot v11 协议 (WebSocket + HTTP API)
- Win32 API (ctypes) — 窗口激活、键盘模拟

## 架构

```
QQ ←→ NapCat (OneBot v11)
          ↓  WebSocket :3001 / HTTP :3000
    Xadeus-QQ-MCP (Python MCP Server)
          ↓  MCP 协议
    opencode Desktop (AI Agent)
```

## 快速开始

### 前置条件

1. 安装 [NapCat.Shell](https://github.com/NapNeko/NapCatQQ) 和 QQ
2. 配置 NapCat OneBot v11 (WebSocket :3001, HTTP :3000)

### 安装

```bash
# 克隆
git clone https://github.com/你的用户名/Xadeus-QQ-MCP
cd Xadeus-QQ-MCP

# 创建虚拟环境
uv venv
uv sync

# 启动
uv run python -m qq_agent_mcp --qq 你的QQ号
```

### opencode MCP 配置

编辑 `~/.config/opencode/opencode.json`:

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

## 唤醒目标配置

通过 `set_wake_config` 配置窗口标题，即可绑定到任意 AI Agent：

```json
{
  "window_title_patterns": ["opencode", "cursor", "claude"],
  "focus_shortcut": "ctrl+l"
}
```

配置文件保存在 `src/qq_agent_mcp/wake_config.json`。

## MCP 工具

| 分类 | 工具 | 说明 |
|------|------|------|
| 消息 | `send_message` | 发文本消息，支持分段、等待回复 |
| 消息 | `send_image` | 发图片 |
| 消息 | `send_voice` | 发语音 |
| 消息 | `wait_for_reply` | 等待新消息 |
| 上下文 | `get_recent_context` | 查看最近消息 |
| 上下文 | `batch_get_recent_context` | 批量查看 |
| 上下文 | `screenshot_chat` | 生成聊天截图 |
| 上下文 | `compress_context` | 压缩缓存 |
| 唤醒 | `add_wake_rule` | 添加唤醒规则 |
| 唤醒 | `set_wake_pending` | 手动管理 pending |
| 唤醒 | `get_wake_config` | 查看唤醒配置 |
| 唤醒 | `set_wake_config` | 配置目标窗口标题（**支持任意 AI Agent**） |
| 唤醒 | `diagnose_wake` | 诊断唤醒状态 |
| 系统 | `check_status` | 检查连接状态 |
| 系统 | `get_group_list` | 群列表 |
| 系统 | `get_friend_list` | 好友列表 |

## 授权

基于 Amadeus-QQ-MCP (MIT License) 改造。
