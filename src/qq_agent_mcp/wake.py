"""后台监控新消息，匹配规则后激活 opencode 窗口并发送唤醒指令。"""

import asyncio
import json
import logging
import os
import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .context import ContextManager, Message

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# ── Thread lock for clipboard/window operations ──────────
_CLIPBOARD_LOCK = threading.Lock()

# ── Inter-process named mutex (prevent duplicate typing from MCP duplicates) ──
MUTEX_NAME = "Local\\XadeusQQ_MCP_WakeTyping"
_mutex_handle = None

# ── Win32 API (Windows only) ──────────────────────────────
if _IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

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

    def _acquire_typing_mutex() -> bool:
        global _mutex_handle
        if _mutex_handle is None:
            _mutex_handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
        WAIT_TIMEOUT = 0x00000102
        result = _kernel32.WaitForSingleObject(_mutex_handle, 0)
        return result != WAIT_TIMEOUT

    def _release_typing_mutex() -> None:
        _kernel32.ReleaseMutex(_mutex_handle)

    def _find_target_hwnd(patterns=None):
        if patterns is None:
            patterns = ["opencode"]
        found = []

        def callback(hwnd, _):
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

    def _is_opencode_focused():
        try:
            hwnd = _find_target_hwnd()
            if hwnd is None:
                return False
            foreground = _user32.GetForegroundWindow()
            return foreground == hwnd
        except Exception:
            return False

    def _activate_window(hwnd):
        try:
            target_tid = _user32.GetWindowThreadProcessId(hwnd, None)
            current_tid = _kernel32.GetCurrentThreadId()
            _user32.AttachThreadInput(current_tid, target_tid, True)
            _user32.SetForegroundWindow(hwnd)
            _user32.BringWindowToTop(hwnd)
            _user32.ShowWindow(hwnd, 5)
            _user32.AttachThreadInput(current_tid, target_tid, False)
            return True
        except Exception as e:
            logger.warning("Failed to activate window: %s", e)
            return False

    def _set_clipboard(text):
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

    def _send_key(vk, up=False):
        _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else KEYEVENTF_KEYDOWN, 0)

    def _send_ctrl_combo(vk):
        _send_key(VK_CONTROL)
        time.sleep(0.03)
        _send_key(vk)
        time.sleep(0.03)
        _send_key(vk, up=True)
        time.sleep(0.03)
        _send_key(VK_CONTROL, up=True)
        time.sleep(0.05)

    _last_paste_text = ""
    _last_paste_time = 0.0

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_uint),
            ("time", ctypes.c_uint),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_uint),
            ("u", _INPUT_UNION),
        ]

    INPUT_KEYBOARD = 1
    KEYEVENTF_UNICODE = 0x0004
    VK_PACKET = 0xE7

    _SENDINPUT_BUFFER = (INPUT * 2)()
    _SENDINPUT_KI = KEYBDINPUT()
    _SENDINPUT_SIZE = ctypes.sizeof(INPUT)

    _GetLastError = ctypes.windll.kernel32.GetLastError

    def _send_unicode_sendinput(cp):
        _SENDINPUT_KI.wVk = VK_PACKET
        _SENDINPUT_KI.wScan = cp
        _SENDINPUT_KI.dwFlags = KEYEVENTF_UNICODE
        _SENDINPUT_KI.time = 0
        _SENDINPUT_KI.dwExtraInfo = None

        _SENDINPUT_BUFFER[0].type = INPUT_KEYBOARD
        _SENDINPUT_BUFFER[0].u.ki = _SENDINPUT_KI

        _SENDINPUT_KI.dwFlags = KEYEVENTF_UNICODE | 2
        _SENDINPUT_BUFFER[1].type = INPUT_KEYBOARD
        _SENDINPUT_BUFFER[1].u.ki = _SENDINPUT_KI

        result = _user32.SendInput(2, _SENDINPUT_BUFFER, _SENDINPUT_SIZE)
        if result != 2:
            logger.warning("SendInput failed for U+%04X (result=%d, gle=%d)",
                           cp, result, _GetLastError())
        return result == 2

    def _send_shortcut(shortcut):
        parts = shortcut.lower().split("+")
        if len(parts) < 2:
            return False
        mods = parts[:-1]
        key = parts[-1]
        vk = _key_name_to_vk(key)
        if vk is None:
            return False
        mod_vks = []
        for m in mods:
            if m == "ctrl":
                mod_vks.append(VK_CONTROL)
            elif m == "shift":
                mod_vks.append(0x10)
            elif m == "alt":
                mod_vks.append(0x12)
        for mvk in mod_vks:
            _send_key(mvk)
            time.sleep(0.03)
        _send_key(vk)
        time.sleep(0.03)
        _send_key(vk, up=True)
        time.sleep(0.03)
        for mvk in reversed(mod_vks):
            _send_key(mvk, up=True)
            time.sleep(0.03)
        time.sleep(0.05)
        return True

    def _key_name_to_vk(name):
        mapping = {
            "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
            "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
            "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
            "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
            "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59,
            "z": 0x5A,
            "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
            "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
        }
        return mapping.get(name)

    def _type_via_console(text, hwnd, focus_shortcut="ctrl+l"):
        if not _activate_window(hwnd):
            return False
        time.sleep(0.3)
        _send_shortcut(focus_shortcut)
        time.sleep(0.15)

        for ch in text:
            cp = ord(ch)
            if cp <= 127:
                result = _user32.VkKeyScanW(ctypes.c_short(cp))
                vk = result & 0xFF
                shift = (result >> 8) & 0xFF
                if vk == 0xFF and shift == 0xFF:
                    continue
                if shift & 1:
                    _user32.keybd_event(0x10, 0, 0, 0)
                    time.sleep(0.03)
                _user32.keybd_event(vk, 0, 0, 0)
                time.sleep(0.03)
                _user32.keybd_event(vk, 0, 2, 0)
                time.sleep(0.03)
                if shift & 1:
                    _user32.keybd_event(0x10, 0, 2, 0)
                time.sleep(0.05)
            else:
                for _ in range(3):
                    if _send_unicode_sendinput(cp):
                        break
                    time.sleep(0.05)
                time.sleep(0.08)

        time.sleep(0.2)
        _send_key(VK_RETURN)
        time.sleep(0.05)
        _send_key(VK_RETURN, up=True)
        time.sleep(0.1)
        return True

else:
    # ── Non-Windows stubs ─────────────────────────────────
    def _acquire_typing_mutex() -> bool:
        return True

    def _release_typing_mutex() -> None:
        pass

    def _find_target_hwnd(patterns=None):
        return None

    def _is_opencode_focused() -> bool:
        return False

    def _activate_window(hwnd) -> bool:
        return False

    def _set_clipboard(text) -> bool:
        return False

    def _send_key(vk, up=False):
        pass

    def _send_ctrl_combo(vk):
        pass

    def _send_shortcut(shortcut) -> bool:
        return False

    def _key_name_to_vk(name):
        return None

    def _type_via_console(text, hwnd, focus_shortcut="ctrl+l") -> bool:
        return False


def _type_via_keyboard(text, patterns=None, focus_shortcut="ctrl+l") -> bool:
    """Type text directly into console (WriteConsoleInputW)."""
    global _last_paste_text, _last_paste_time

    if not _IS_WINDOWS:
        logger.warning("Wake typing is only supported on Windows")
        return False

    acquired = _CLIPBOARD_LOCK.acquire(blocking=False)
    if not acquired:
        logger.warning("Console type operation already in progress, skipping")
        return False

    if not _acquire_typing_mutex():
        logger.info("Another MCP process is already typing, skipping")
        _CLIPBOARD_LOCK.release()
        return False

    try:
        now = time.time()
        if text == _last_paste_text and (now - _last_paste_time) < 3.0:
            logger.warning("Duplicate paste suppressed (same text within 3s)")
            return False

        hwnd = _find_target_hwnd(patterns)
        if hwnd is None:
            logger.warning("opencode window not found")
            return False

        if not _type_via_console(text, hwnd, focus_shortcut):
            logger.warning("Console input injection failed")
            return False

        _last_paste_text = text
        _last_paste_time = time.time()
        return True
    finally:
        _release_typing_mutex()
        _CLIPBOARD_LOCK.release()


def _type_via_clipboard(text, patterns=None) -> bool:
    """Backwards-compat wrapper (callers pass (text, patterns), no shortcut)."""
    return _type_via_keyboard(text, patterns, "ctrl+l")


# ── 规则系统 ──────────────────────────────────────────────

RULES_FILE = os.path.join(os.path.dirname(__file__), "wake_rules.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "wake_config.json")

TARGET_LABELS = {"group": "群聊", "private": "私聊"}


@dataclass
class WakeConfig:
    window_title_patterns: list[str] = field(default_factory=lambda: ["opencode"])
    focus_shortcut: str = "ctrl+l"

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
                    "window_title_patterns": self.config.window_title_patterns,
                    "focus_shortcut": self.config.focus_shortcut,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save wake config: %s", e)


@dataclass
class WakeRule:
    target_type: str
    target_id: str | None
    keywords: list[str] = field(default_factory=list)
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
        self._pending_lock_time: float = 0.0
        self._pending_timeout: float = 300.0
        self._auto_unlock_task: asyncio.Task | None = None
        self._woke_ids: set[str] = set()
        self._waiting_for_reply: bool = False
        self.config = WakeConfig.load()
        ctx.set_message_callback(self._on_incoming_message)
        self._load_rules()

    def _wake_key(self, target_type: str, target_id: str, msg: Message) -> str:
        import hashlib
        raw = f"{target_type}:{target_id}:{msg.sender_id}:{msg.content}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _on_incoming_message(self, target_type: str, target_id: str, msg: Message) -> None:
        if not self._running:
            return

        if self._pending and time.time() - self._pending_lock_time > self._pending_timeout:
            logger.warning("Manual lock expired (%ds), force unlocking", self._pending_timeout)
            self._pending = False

        matched = self._matches_rule(target_type, target_id, msg)
        if matched is None:
            return

        if self._pending:
            return

        if self._waiting_for_reply:
            logger.info("Waiting for reply, locked")
            return

        wake_key = self._wake_key(target_type, target_id, msg)
        if wake_key in self._woke_ids:
            logger.debug("Duplicate wake skipped (key=%s)", wake_key)
            return
        self._woke_ids.add(wake_key)
        if len(self._woke_ids) > 100:
            self._woke_ids = set(list(self._woke_ids)[-100:])
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
        sender_id = msg.sender_id
        sender_name = "".join(c if 32 <= ord(c) < 127 else "?" for c in (msg.sender_name or ""))
        return f"[MCP wake from {target_type} {target_id} sender={sender_id}({sender_name})] call get_recent_context to see all messages then reply"

    @property
    def is_pending(self) -> bool:
        return self._pending

    def is_relevant(self, target_type: str, target_id: str, msg: "Message") -> bool:
        return self._matches_rule(target_type, target_id, msg) is not None

    def clear_pending(self) -> None:
        self._pending = False

    async def _auto_unlock_loop(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            if self._pending and time.time() - self._pending_lock_time > self._pending_timeout:
                logger.warning("Auto-unlocking manual lock (stuck for >%ds)", self._pending_timeout)
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
        self._pending = pending
        if pending:
            self._pending_lock_time = time.time()
        logger.info("Wake pending set to %s", pending)

    async def wake_with_message(self, text: str) -> bool:
        loop = asyncio.get_event_loop()
        try:
            fn = lambda: _type_via_keyboard(
                text, self.config.window_title_patterns, self.config.focus_shortcut)
            ok = await loop.run_in_executor(None, fn)
            logger.info("Wake via message: %s (ok=%s)", text, ok)
            return ok
        except Exception as e:
            logger.error("Wake via message error: %s", e)
            return False

    async def _trigger(self, rule: WakeRule, target_type: str, target_id: str, msg: Message) -> None:
        text = self._format_wake_message(rule, target_type, target_id, msg)
        logger.info("Wake triggered: %s", text)
        if self._on_wake:
            self._on_wake(target_type, target_id, msg)

        try:
            ok = _type_via_keyboard(text, self.config.window_title_patterns, self.config.focus_shortcut)
            logger.info("Wake activation result: %s", ok)
        except Exception as e:
            logger.error("Wake activation error: %s", e)
            ok = False

    def mark_reply_sent(self, target_type: str, target_id: str) -> None:
        pass

    def set_waiting_for_reply(self, waiting: bool) -> None:
        self._waiting_for_reply = waiting
