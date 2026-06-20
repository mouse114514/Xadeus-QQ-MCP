"""后台监控新消息，匹配规则后激活 opencode 窗口并发送唤醒指令。"""

import asyncio
import json
import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .context import ContextManager, Message

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()
_IS_WINDOWS = _SYSTEM == "Windows"
_IS_MACOS = _SYSTEM == "Darwin"
_IS_LINUX = _SYSTEM == "Linux"

# ── Thread lock for clipboard/window operations ──────────
_CLIPBOARD_LOCK = threading.Lock()

# ── Inter-process mutex (prevent duplicate typing) ─────
_MUTEX_LOCK_FILE = None

def _acquire_typing_mutex() -> bool:
    global _MUTEX_LOCK_FILE
    try:
        if _IS_WINDOWS:
            import ctypes
            h = ctypes.windll.kernel32.CreateMutexW(None, False,
                "Local\\XadeusQQ_MCP_WakeTyping")
            r = ctypes.windll.kernel32.WaitForSingleObject(h, 0)
            _MUTEX_LOCK_FILE = h
            return r != 0x00000102  # WAIT_TIMEOUT
        else:
            import fcntl
            path = os.path.join(os.path.dirname(__file__), "wake_typing.lock")
            _MUTEX_LOCK_FILE = open(path, "w")
            fcntl.flock(_MUTEX_LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
    except (IOError, BlockingIOError, ImportError, AttributeError):
        return False

def _release_typing_mutex() -> None:
    global _MUTEX_LOCK_FILE
    if _IS_WINDOWS:
        try:
            ctypes.windll.kernel32.ReleaseMutex(_MUTEX_LOCK_FILE)
            ctypes.windll.kernel32.CloseHandle(_MUTEX_LOCK_FILE)
        except Exception:
            pass
    else:
        try:
            import fcntl
            if _MUTEX_LOCK_FILE is not None:
                fcntl.flock(_MUTEX_LOCK_FILE.fileno(), fcntl.LOCK_UN)
                _MUTEX_LOCK_FILE.close()
        except Exception:
            pass
    _MUTEX_LOCK_FILE = None

# ── Clipboard helpers ──────────────────────────────────────
def _set_clipboard(text: str) -> bool:
    try:
        if _IS_WINDOWS:
            import base64
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
        elif _IS_MACOS:
            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                                  capture_output=True, timeout=5.0)
            return proc.returncode == 0
        elif _IS_LINUX:
            for tool in ["xclip", "wl-clipboard"]:
                cmd = ["xclip", "-selection", "clipboard"] if tool == "xclip" else ["wl-copy"]
                try:
                    proc = subprocess.run(cmd, input=text.encode("utf-8"),
                                          capture_output=True, timeout=5.0)
                    return proc.returncode == 0
                except FileNotFoundError:
                    continue
            return False
        return False
    except Exception as e:
        logger.warning("set_clipboard failed: %s", e)
        return False

_last_paste_text = ""
_last_paste_time = 0.0

# ═══════════════════════════════════════════════════════════
#  Platform-specific: Windows (keybd_event char-by-char)
# ═══════════════════════════════════════════════════════════
if _IS_WINDOWS:
    import ctypes
    import ctypes.wintypes
    import subprocess

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)

    VK_CONTROL = 0x11
    VK_RETURN = 0x0D

    KEYEVENTF_KEYDOWN = 0x0000
    KEYEVENTF_KEYUP = 0x0002

    def _win_find_hwnd(patterns):
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

    def _win_activate(hwnd):
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
            logger.warning("_win_activate failed: %s", e)
            return False

    def _win_send_key(vk, up=False):
        _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else KEYEVENTF_KEYDOWN, 0)

    _KEY_NAME_VK = {
        "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
        "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
        "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
        "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
        "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59,
        "z": 0x5A,
        "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
        "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
        "return": 0x0D, "enter": 0x0D, "tab": 0x09, "escape": 0x1B,
        "backspace": 0x08, "delete": 0x2E,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    }

    def _win_send_shortcut(shortcut):
        parts = shortcut.lower().split("+")
        if len(parts) < 2:
            return False
        mods = parts[:-1]; key = parts[-1]
        vk = _KEY_NAME_VK.get(key)
        if vk is None:
            return False
        mod_vks = []
        for m in mods:
            if m in ("ctrl",):
                mod_vks.append(VK_CONTROL)
            elif m in ("shift",):
                mod_vks.append(0x10)
            elif m in ("alt", "win", "cmd"):
                mod_vks.append(0x12 if m == "alt" else 0x5B)
        for mvk in mod_vks:
            _win_send_key(mvk); time.sleep(0.03)
        _win_send_key(vk); time.sleep(0.03)
        _win_send_key(vk, up=True); time.sleep(0.03)
        for mvk in reversed(mod_vks):
            _win_send_key(mvk, up=True); time.sleep(0.03)
        time.sleep(0.05)
        return True

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
                    ("dwExtraInfo", ctypes.c_void_p)]
    class _INPUT_U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT)]
    class _INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint), ("u", _INPUT_U)]

    def _win_type_text(text, patterns, focus_shortcut):
        hwnd = _win_find_hwnd(patterns)
        if hwnd is None:
            logger.warning("opencode window not found")
            return False
        if not _win_activate(hwnd):
            return False
        time.sleep(0.3)
        if focus_shortcut:
            _win_send_shortcut(focus_shortcut)
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
                    _win_send_key(0x10); time.sleep(0.03)
                _win_send_key(vk); time.sleep(0.03)
                _win_send_key(vk, up=True); time.sleep(0.03)
                if shift & 1:
                    _win_send_key(0x10, up=True); time.sleep(0.03)
                time.sleep(0.05)
            else:
                ki = _KEYBDINPUT(0xE7, cp, 0x0004, 0, None)
                inp = _INPUT(1, _INPUT_U(ki))
                for _ in range(3):
                    r = _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
                    if r == 1:
                        break
                    time.sleep(0.05)
                time.sleep(0.08)

        time.sleep(0.2)
        _win_send_key(VK_RETURN); time.sleep(0.05)
        _win_send_key(VK_RETURN, up=True); time.sleep(0.1)
        return True

    # Alias for code that still references these
    _find_target_hwnd = _win_find_hwnd

# ═══════════════════════════════════════════════════════════
#  Platform-specific: macOS (osascript + pbcopy)
# ═══════════════════════════════════════════════════════════
elif _IS_MACOS:
    import subprocess

    def _mac_find_and_activate(patterns):
        import json
        pat_json = json.dumps(patterns)
        script = f'''
        set patterns to {pat_json}
        tell application "System Events"
            set allProc to every process whose background only is false
            repeat with p in allProc
                try
                    set winTitle to name of front window of p
                    repeat with pat in patterns
                        if winTitle contains pat then
                            set frontmost of p to true
                            return name of p
                        end if
                    end repeat
                end try
            end repeat
        end tell
        return ""
        '''
        try:
            proc = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=10.0)
            return proc.stdout.strip()
        except Exception as e:
            logger.warning("_mac_find_and_activate failed: %s", e)
            return ""

    _MAC_MOD_MAP = {
        "ctrl": "command down", "cmd": "command down",
        "shift": "shift down", "alt": "option down", "option": "option down",
    }
    _MAC_KEY_MAP = {
        "return": "return", "enter": "return", "tab": "tab",
        "escape": "escape", "backspace": "delete", "delete": "forward delete",
        "up": "up arrow", "down": "down arrow",
        "left": "left arrow", "right": "right arrow",
    }

    def _mac_send_shortcut(shortcut):
        parts = shortcut.lower().split("+")
        if len(parts) == 0:
            return False
        mods = parts[:-1]; key = parts[-1]
        is_special = key in _MAC_KEY_MAP
        mapped = _MAC_KEY_MAP.get(key, key)
        # Escape quotes for AppleScript string
        safe = mapped.replace('"', '\\"') if not is_special else mapped
        if mods:
            mod_str = ", ".join(_MAC_MOD_MAP[m] for m in mods if m in _MAC_MOD_MAP)
            if is_special:
                script = f'tell application "System Events" to keystroke {safe} using {{{mod_str}}}'
            else:
                script = f'tell application "System Events" to keystroke "{safe}" using {{{mod_str}}}'
        else:
            if is_special:
                script = f'tell application "System Events" to keystroke {safe}'
            else:
                script = f'tell application "System Events" to keystroke "{safe}"'
        try:
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=5.0)
            return True
        except Exception as e:
            logger.warning("_mac_send_shortcut failed: %s", e)
            return False

    def _mac_type_text(text, patterns, focus_shortcut):
        app_name = _mac_find_and_activate(patterns)
        if not app_name:
            logger.warning("No matching window found on macOS")
            return False
        time.sleep(0.3)
        if focus_shortcut:
            _mac_send_shortcut(focus_shortcut)
            time.sleep(0.15)
        _set_clipboard(text)
        time.sleep(0.15)
        _mac_send_shortcut("cmd+v")
        time.sleep(0.3)
        _mac_send_shortcut("return")
        time.sleep(0.1)
        return True

    _find_target_hwnd = lambda *a: None

# ═══════════════════════════════════════════════════════════
#  Platform-specific: Linux (xdotool + xclip)
# ═══════════════════════════════════════════════════════════
elif _IS_LINUX:
    import subprocess
    import shutil

    def _linux_find_and_activate(patterns):
        if not shutil.which("xdotool"):
            logger.warning("xdotool not found, cannot activate window")
            return None
        try:
            for p in patterns:
                proc = subprocess.run(
                    ["xdotool", "search", "--name", p, "--limit", "1"],
                    capture_output=True, text=True, timeout=5.0,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    wid = proc.stdout.strip()
                    subprocess.run(["xdotool", "windowactivate", wid],
                                   capture_output=True, timeout=5.0)
                    return wid
        except Exception as e:
            logger.warning("_linux_find_and_activate failed: %s", e)
        return None

    def _linux_send_shortcut(wid, shortcut):
        parts = shortcut.lower().split("+")
        if len(parts) == 0:
            return False
        mods = parts[:-1]; key = parts[-1]
        xmod_map = {"ctrl": "ctrl", "shift": "shift", "alt": "alt",
                    "cmd": "super", "win": "super"}
        xmods = "+".join(xmod_map[m] for m in mods if m in xmod_map)
        combo = f"{xmods}+{key}" if xmods else key
        try:
            subprocess.run(["xdotool", "key", "--window", str(wid), combo],
                           capture_output=True, timeout=5.0)
            return True
        except Exception as e:
            logger.warning("_linux_send_shortcut failed: %s", e)
            return False

    def _linux_type_text(text, patterns, focus_shortcut):
        wid = _linux_find_and_activate(patterns)
        if wid is None:
            logger.warning("No matching window found on Linux")
            return False
        time.sleep(0.3)
        if focus_shortcut:
            # Translate ctrl+l → ctrl+l (xdotool uses same notation)
            _linux_send_shortcut(wid, focus_shortcut)
            time.sleep(0.15)
        _set_clipboard(text)
        time.sleep(0.15)
        _linux_send_shortcut(wid, "ctrl+v")
        time.sleep(0.3)
        subprocess.run(["xdotool", "key", "--window", str(wid), "Return"],
                       capture_output=True, timeout=5.0)
        time.sleep(0.1)
        return True

    _find_target_hwnd = lambda *a: None

else:
    logger.warning("Unsupported platform: %s", _SYSTEM)
    _find_target_hwnd = lambda *a: None


# ── Dispatch to platform-specific typing ─────────────────
def _type_text_platform(text: str, patterns: list[str] | None = None,
                        focus_shortcut: str = "ctrl+l") -> bool:
    if _IS_WINDOWS:
        return _win_type_text(text, patterns, focus_shortcut)
    elif _IS_MACOS:
        return _mac_type_text(text, patterns, focus_shortcut)
    elif _IS_LINUX:
        return _linux_type_text(text, patterns, focus_shortcut)
    else:
        logger.warning("No typing implementation for %s", _SYSTEM)
        return False


def _type_via_keyboard(text: str, patterns: list[str] | None = None,
                       focus_shortcut: str = "ctrl+l") -> bool:
    global _last_paste_text, _last_paste_time

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

        ok = _type_text_platform(text, patterns, focus_shortcut)
        if not ok:
            return False

        _last_paste_text = text
        _last_paste_time = time.time()
        return True
    finally:
        _release_typing_mutex()
        _CLIPBOARD_LOCK.release()


def _type_via_clipboard(text, patterns=None) -> bool:
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
                    "window_title_patterns": self.window_title_patterns,
                    "focus_shortcut": self.focus_shortcut,
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
