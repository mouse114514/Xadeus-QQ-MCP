"""后台监控新消息，匹配规则后激活 opencode 窗口并发送唤醒指令。"""

import asyncio
import ctypes
import ctypes.wintypes
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from .context import ContextManager, Message

logger = logging.getLogger(__name__)

# ── Win32 API ──────────────────────────────────────────────
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVABLE = 0x0002

_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)

VK_CONTROL = 0x11
VK_L = 0x4C
VK_V = 0x56
VK_RETURN = 0x0D

KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_KEYUP = 0x0002


def _find_target_hwnd(patterns: list[str] | None = None) -> int | None:
    """找到匹配窗口标题的句柄。"""
    if patterns is None:
        patterns = ["opencode"]
    found = []

    def callback(hwnd: int, _) -> bool:
        if _user32.IsWindowVisible(hwnd):
            length = _user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            _user32.GetWindowTextW(hwnd, buf, length)
            title = buf.value.lower()
            for p in patterns:
                if p.lower() in title:
                    found.append(hwnd)
                    return True
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found[0] if found else None


def _is_opencode_focused() -> bool:
    """Check if the opencode window is currently the foreground window."""
    try:
        hwnd = _find_opencode_hwnd()
        if hwnd is None:
            return False
        foreground = _user32.GetForegroundWindow()
        return foreground == hwnd
    except Exception:
        return False


def _activate_window(hwnd: int) -> bool:
    """Bring window to foreground, handling UIPI via AttachThreadInput."""
    try:
        target_tid = _user32.GetWindowThreadProcessId(hwnd, None)
        current_tid = _kernel32.GetCurrentThreadId()
        _user32.AttachThreadInput(current_tid, target_tid, True)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        _user32.ShowWindow(hwnd, 5)  # SW_SHOW
        _user32.AttachThreadInput(current_tid, target_tid, False)
        return True
    except Exception as e:
        logger.warning("Failed to activate window: %s", e)
        return False


def _set_clipboard(text: str) -> bool:
    """Set clipboard via PowerShell."""
    import subprocess
    import base64
    try:
        b64 = base64.b64encode(text.encode("utf-16-le")).decode()
        cmd = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.Clipboard]::SetText("
            "[System.Text.Encoding]::Unicode.GetString("
            "[System.Convert]::FromBase64String('" + b64 + "')))"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, timeout=5.0,
        )
        return proc.returncode == 0
    except Exception as e:
        logger.warning("set_clipboard failed: %s", e)
        return False


def _send_key(vk: int, up: bool = False) -> None:
    """Send a single key event."""
    _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else KEYEVENTF_KEYDOWN, 0)


def _send_ctrl_combo(vk: int) -> None:
    """Send CTRL+<key> combination."""
    _send_key(VK_CONTROL)
    time.sleep(0.03)
    _send_key(vk)
    time.sleep(0.03)
    _send_key(vk, up=True)
    time.sleep(0.03)
    _send_key(VK_CONTROL, up=True)
    time.sleep(0.05)


def _type_via_clipboard(text: str, patterns: list[str] | None = None) -> bool:
    """Focus → CTRL+L → set clipboard → CTRL+V."""
    hwnd = _find_target_hwnd(patterns)
    if hwnd is None:
        logger.warning("opencode window not found")
        return False

    if not _activate_window(hwnd):
        return False

    time.sleep(0.15)
    _send_ctrl_combo(VK_L)  # 聚焦输入框
    time.sleep(0.1)

    if not _set_clipboard(text):
        return False

    time.sleep(0.05)
    _send_ctrl_combo(VK_V)  # 粘贴
    time.sleep(0.1)
    # 按 Enter 发送消息
    _send_key(VK_RETURN)
    time.sleep(0.03)
    _send_key(VK_RETURN, up=True)
    time.sleep(0.1)
    return True


# ── 规则系统 ──────────────────────────────────────────────

RULES_FILE = os.path.join(os.path.dirname(__file__), "wake_rules.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "wake_config.json")

TARGET_LABELS = {"group": "群聊", "private": "私聊"}


@dataclass
class WakeConfig:
    window_title_patterns: list[str] = field(default_factory=lambda: ["opencode"])
    focus_shortcut: str = "ctrl+l"  # 聚焦输入框的快捷键

    @classmethod
    def load(cls) -> "WakeConfig":
        try:
            if os.path.isfile(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return cls(
                    window_title_patterns=data.get("window_title_patterns", ["opencode"]),
                    focus_shortcut=data.get("focus_shortcut", "ctrl+l"),
                )
        except Exception as e:
            logger.warning("Failed to load wake config: %s", e)
        return cls()

    def save(self) -> None:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "window_title_patterns": self.window_title_patterns,
                    "focus_shortcut": self.focus_shortcut,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save wake config: %s", e)


@dataclass
class WakeRule:
    target_type: str  # "group" or "private"
    target_id: str | None  # None = 任意
    keywords: list[str] = field(default_factory=list)  # 空列表 = 任意消息
    enabled: bool = True
    ignore_if_focused: bool = True

    def label(self) -> str:
        parts = [TARGET_LABELS.get(self.target_type, self.target_type)]
        if self.target_id:
            parts.append(self.target_id)
        if self.keywords:
            parts.append(f"关键词={self.keywords}")
        return " ".join(parts)


class WakeMonitor:
    """后台监控消息，匹配规则后激活 opencode 窗口。"""

    def __init__(self, ctx: ContextManager, on_wake: Callable | None = None):
        self.ctx = ctx
        self.rules: list[WakeRule] = []
        self._running = False
        self._on_wake = on_wake
        self._pending = False
        self._auto_unlock_task: asyncio.Task | None = None
        self.config = WakeConfig.load()
        # 注册消息回调（消息入库时直接触发，无需轮询）
        ctx.set_message_callback(self._on_incoming_message)
        # 持久化规则加载
        self._load_rules()

    def _on_incoming_message(self, target_type: str, target_id: str, msg: Message) -> None:
        """Called by ContextManager for every incoming non-self message."""
        if not self._running:
            return
        if self._pending:
            return
        matched = self._matches_rule(target_type, target_id, msg)
        if matched is None:
            return
        self._pending = True
        asyncio.create_task(self._trigger(matched, target_type, target_id, msg))

    def add_rule(self, target_type: str, target_id: str | None = None,
                 keywords: list[str] | None = None,
                 ignore_if_focused: bool = True) -> int:
        rule = WakeRule(
            target_type=target_type,
            target_id=target_id,
            keywords=keywords or [],
            enabled=True,
            ignore_if_focused=ignore_if_focused,
        )
        idx = len(self.rules)
        self.rules.append(rule)
        self._save_rules()
        logger.info("Wake rule #%d added: %s", idx, rule)
        return idx

    def remove_rule(self, index: int) -> bool:
        if 0 <= index < len(self.rules):
            removed = self.rules.pop(index)
            self._save_rules()
            logger.info("Wake rule #%d removed: %s", index, removed)
            return True
        return False

    def list_rules(self) -> list[dict]:
        return [
            {
                "index": i,
                "label": r.label(),
                "target_type": r.target_type,
                "target_id": r.target_id,
                "keywords": r.keywords,
                "enabled": r.enabled,
                "ignore_if_focused": r.ignore_if_focused,
            }
            for i, r in enumerate(self.rules)
        ]

    def set_enabled(self, index: int, enabled: bool) -> bool:
        if 0 <= index < len(self.rules):
            self.rules[index].enabled = enabled
            self._save_rules()
            return True
        return False

    def set_enabled_all(self, enabled: bool) -> None:
        for r in self.rules:
            r.enabled = enabled
        self._save_rules()
        logger.info("Wake monitor %s", "enabled" if enabled else "disabled")

    # ── 持久化 ──

    def _save_rules(self) -> None:
        try:
            data = [
                {
                    "target_type": r.target_type,
                    "target_id": r.target_id,
                    "keywords": r.keywords,
                    "enabled": r.enabled,
                    "ignore_if_focused": r.ignore_if_focused,
                }
                for r in self.rules
            ]
            with open(RULES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save wake rules: %s", e)

    def _load_rules(self) -> None:
        try:
            if not os.path.isfile(RULES_FILE):
                return
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.rules = [
                WakeRule(
                    target_type=r["target_type"],
                    target_id=r.get("target_id"),
                    keywords=r.get("keywords", []),
                    enabled=r.get("enabled", True),
                    ignore_if_focused=r.get("ignore_if_focused", True),
                )
                for r in data
            ]
            logger.info("Loaded %d wake rules from %s", len(self.rules), RULES_FILE)
        except Exception as e:
            logger.warning("Failed to load wake rules: %s", e)

    # ── Lifecycle ──

    def start(self) -> None:
        self._running = True
        self._auto_unlock_task = asyncio.create_task(self._auto_unlock_loop())
        logger.info("Wake monitor started (%d rules)", len(self.rules))

    async def stop(self) -> None:
        self._running = False
        if self._auto_unlock_task is not None:
            self._auto_unlock_task.cancel()
            self._auto_unlock_task = None
        logger.info("Wake monitor stopped")

    def _matches_rule(self, target_type: str, target_id: str, msg: Message) -> WakeRule | None:
        if msg.is_self:
            return None
        for rule in self.rules:
            if not rule.enabled:
                continue
            if rule.target_type != target_type:
                continue
            if rule.target_id is not None and rule.target_id != target_id:
                continue
            if rule.keywords:
                matched = any(kw in msg.content for kw in rule.keywords)
                if not matched:
                    continue
            return rule
        return None

    def _format_wake_message(self, rule: WakeRule, target_type: str, target_id: str, msg: Message) -> str:
        return f"[MCP] {target_id} {msg.content}"

    @property
    def is_pending(self) -> bool:
        return self._pending

    def is_relevant(self, target_type: str, target_id: str, msg: "Message") -> bool:
        """检查消息是否匹配任意一条启用的规则。"""
        return self._matches_rule(target_type, target_id, msg) is not None

    def clear_pending(self) -> None:
        """Agent 调用 QQ 发送工具时调用此方法，解除 pending 允许下次唤醒。"""
        self._pending = False

    async def _auto_unlock_loop(self) -> None:
        """每 60 秒自动解锁 pending，防止 agent 崩溃后卡死。"""
        while self._running:
            await asyncio.sleep(60)
            if self._pending:
                logger.warning("Auto-unlocking pending (stuck for >60s)")
                self._pending = False

    def get_config(self) -> dict:
        return {
            "window_title_patterns": self.config.window_title_patterns,
            "focus_shortcut": self.config.focus_shortcut,
        }

    def set_config(self, window_title_patterns: list[str] | None = None,
                   focus_shortcut: str | None = None) -> None:
        if window_title_patterns is not None:
            self.config.window_title_patterns = window_title_patterns
        if focus_shortcut is not None:
            self.config.focus_shortcut = focus_shortcut
        self.config.save()

    def set_pending(self, pending: bool) -> None:
        """agent 手动管理 pending 状态。"""
        self._pending = pending
        logger.info("Wake pending set to %s", pending)

    async def _trigger(self, rule: WakeRule, target_type: str, target_id: str, msg: Message) -> None:
        text = self._format_wake_message(rule, target_type, target_id, msg)
        logger.info("Wake triggered: %s", text)
        if self._on_wake:
            self._on_wake(target_type, target_id, msg)

        loop = asyncio.get_event_loop()
        try:
            ok = await loop.run_in_executor(
                None, _type_via_clipboard, text, self.config.window_title_patterns,
            )
            logger.info("Wake activation result: %s", ok)
        except Exception as e:
            logger.error("Wake activation error: %s", e)
