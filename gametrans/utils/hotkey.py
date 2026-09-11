"""全局热键：解析 + RegisterHotKey 注册，并在 Qt 消息循环里拦截 WM_HOTKEY。

用法：
    hotkey = GlobalHotkey()
    hotkey.set_hotkey("Ctrl+Shift+T")   # 返回是否注册成功
    hotkey.hotkey_pressed.connect(slot) # 需要 QApplication 事件循环
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import List, Optional, Tuple

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32

MOD_ALT = 0x1
MOD_CONTROL = 0x2
MOD_SHIFT = 0x4
MOD_WIN = 0x8
WM_HOTKEY = 0x0312

# 通用键名 -> VK
KEY_TO_VK = {
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46,
    "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A, "K": 0x4B, "L": 0x4C,
    "M": 0x4D, "N": 0x4E, "O": 0x4F, "P": 0x50, "Q": 0x51, "R": 0x52,
    "S": 0x53, "T": 0x54, "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58,
    "Y": 0x59, "Z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
    "SPACE": 0x20, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "INSERT": 0x2D,
    "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "MINUS": 0xBD, "EQUALS": 0xBB, "COMMA": 0xBC, "PERIOD": 0xBE,
    "SLASH": 0xBF, "BACKSLASH": 0xDC, "SEMICOLON": 0xBA, "QUOTE": 0xDE,
    "LBRACKET": 0xDB, "RBRACKET": 0xDD, "GRAVE": 0xC0,
}

VK_TO_KEY = {v: k for k, v in KEY_TO_VK.items()}

MOD_NAMES = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN}
MOD_TO_NAME = {MOD_CONTROL: "Ctrl", MOD_ALT: "Alt", MOD_SHIFT: "Shift", MOD_WIN: "Win"}


def parse_hotkey(text: str) -> Optional[Tuple[int, int]]:
    """解析 "Ctrl+Shift+T" -> (mods, vk)。失败返回 None。"""
    if not text or not isinstance(text, str):
        return None
    parts = [p.strip().lower() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = ""
    for p in parts:
        if p in MOD_NAMES:
            mods |= MOD_NAMES[p]
        else:
            key = p.upper()  # 键表以大写字母/数字为键，统一大写后匹配
    if not key or key not in KEY_TO_VK:
        return None
    if mods == 0:
        return None  # 必须有修饰键
    return mods, KEY_TO_VK[key]


def format_hotkey(mods: int, vk: int) -> str:
    names = [MOD_TO_NAME[m] for m in (MOD_CONTROL, MOD_ALT, MOD_SHIFT, MOD_WIN) if mods & m]
    names.append(VK_TO_KEY.get(vk, f"0x{vk:X}"))
    return "+".join(names)


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """跨线程安全的全局热键管理器。需在 QApplication 创建后实例化。"""

    hotkey_pressed = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._registered = False
        self._mods = 0
        self._vk = 0
        self._hotkey_id = 0x4001  # 唯一
        from PySide6.QtWidgets import QApplication
        QApplication.instance().installNativeEventFilter(self)

    def set_hotkey(self, text: str) -> bool:
        parsed = parse_hotkey(text)
        if parsed is None:
            log.warning("无法解析热键: %s", text)
            return False
        mods, vk = parsed
        with self._lock:
            self.unregister()
            ok = bool(user32.RegisterHotKey(None, self._hotkey_id, mods, vk))
            if ok:
                self._registered = True
                self._mods, self._vk = mods, vk
                log.info("热键已注册: %s", format_hotkey(mods, vk))
            else:
                log.warning("热键注册失败(可能被占用): %s", text)
            return ok

    def unregister(self) -> None:
        with self._lock:
            if self._registered:
                user32.UnregisterHotKey(None, self._hotkey_id)
                self._registered = False

    def nativeEventFilter(self, event_type, message) -> Tuple[bool, int]:
        try:
            if event_type == "windows_generic_MSG":
                msg = message[0]
                if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                    if self._registered:
                        self.hotkey_pressed.emit()
                    return True, 0
        except Exception:
            pass
        return False, 0

    def current_text(self) -> str:
        with self._lock:
            if not self._registered:
                return ""
            return format_hotkey(self._mods, self._vk)
