"""Win32 辅助：窗口枚举/矩形/置顶/点击穿透，全部基于 ctypes，避免额外依赖。

所有坐标均为物理像素（进程已设为 DPI 感知）。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
DWMWA_CLOAKED = 14
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11  # Win10 2004+：窗口对屏幕捕获不可见（肉眼正常）
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def _rect_tuple(rect) -> Tuple[int, int, int, int]:
    return rect.left, rect.top, rect.right, rect.bottom


def set_always_on_top(hwnd: int, on: bool = True) -> None:
    try:
        insert = HWND_TOPMOST if on else HWND_NOTOPMOST
        user32.SetWindowPos(wintypes.HWND(hwnd), insert, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    except Exception:
        pass


def set_click_through(hwnd: int, on: bool = True) -> None:
    """让窗口不拦截鼠标点击（配合 Qt 的 WA_TransparentForMouseEvents）。"""
    try:
        style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        style = int(style)
        if on:
            style = style | WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            style = style & ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, style)
    except Exception:
        pass


def set_capture_exclude(hwnd: int, on: bool = True) -> bool:
    """把窗口标记为“对屏幕捕获不可见”（Win10 2004+ WDA_EXCLUDEFROMCAPTURE）。

    效果：用户肉眼正常看到窗口，但 mss/BitBlt 等屏幕抓取拿到的画面里
    完全没有该窗口 —— OCR 抓屏自动是纯净底层，译文层不会污染识别。
    返回是否设置成功（旧系统不支持时返回 False，上层需回退其它方案）。
    """
    try:
        affinity = WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE
        return bool(user32.SetWindowDisplayAffinity(
            wintypes.HWND(hwnd), wintypes.DWORD(affinity)))
    except Exception:
        return False


def is_visible_window(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))


def get_window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def get_window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """窗口屏幕矩形（含边框），物理像素。失败返回 None。"""
    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return _rect_tuple(rect)


def get_client_rect_on_screen(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """窗口客户区在屏幕上的矩形，物理像素。失败返回 None。"""
    rect = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom)


def _is_cloaked(hwnd: int) -> bool:
    try:
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED,
                                     ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return bool(cloaked.value)
    except Exception:
        return False


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def get_process_id(hwnd: int) -> int:
    """窗口所属进程 ID。"""
    pid = wintypes.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def is_foreground_related(hwnd: int) -> bool:
    """目标窗口是否处于前台（或前台窗口属于它：子窗口/弹窗/同进程）。

    用于"只翻译选定的游戏窗口"：切到浏览器/桌面后应暂停翻译，
    避免把别的窗口内容当游戏来翻。
    拿不到前台信息时返回 True（不阻断，宁可多翻也不要失效）。
    """
    if not hwnd:
        return False
    fg = foreground_window()
    if not fg:
        return True
    if fg == hwnd:
        return True
    try:
        if get_process_id(fg) == get_process_id(hwnd):
            return True
        # 前台窗口的 owner 链里含目标窗口（游戏的弹窗/子窗口场景）
        owner = int(user32.GetWindow(wintypes.HWND(fg), 4) or 0)  # GW_OWNER
        depth = 0
        while owner and depth < 8:
            if owner == hwnd:
                return True
            owner = int(user32.GetWindow(wintypes.HWND(owner), 4) or 0)
            depth += 1
    except Exception:  # noqa: BLE001
        return True
    return False


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, left + w, top + h


def enumerate_windows(skip_hidden: bool = True, min_title_len: int = 1) -> List[Dict]:
    """枚举可见顶层窗口，用于“选择要翻译的游戏窗口”。"""
    out: List[Dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        h = int(hwnd)
        if skip_hidden and not is_visible_window(h):
            return True
        if _is_cloaked(h):
            return True
        if user32.GetWindow(wintypes.HWND(h), 4):  # GW_OWNER -> 弹出窗忽略
            return True
        title = get_window_text(h)
        if len(title.strip()) < min_title_len:
            return True
        cls = get_window_class(h)
        rect = get_window_rect(h)
        if not rect or rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return True
        out.append({"hwnd": h, "title": title, "class": cls, "rect": rect})
        return True

    user32.EnumWindows(_cb, 0)
    return out


def find_window_by_title_contains(fragment: str) -> Optional[int]:
    if not fragment:
        return None
    for win in enumerate_windows():
        if fragment.lower() in win["title"].lower():
            return win["hwnd"]
    return None


def get_monitor_rects() -> List[Tuple[int, int, int, int]]:
    """所有显示器的边界矩形（物理像素），枚举顺序与系统显示器枚举一致。"""
    monitors: List[Tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def _cb(_monitor, _hdc, rect_ptr, _lparam):
        rect = rect_ptr.contents
        monitors.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, _cb, 0)
    return monitors


def window_monitor_index(hwnd: int, monitors: List[Tuple[int, int, int, int]]) -> int:
    """窗口中心落在哪个显示器上。"""
    rect = get_window_rect(hwnd)
    if not rect:
        return 0
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    best, best_area = 0, -1
    for i, (l, t, r, b) in enumerate(monitors):
        inter_w = max(0, min(r, rect[2]) - max(l, rect[0]))
        inter_h = max(0, min(b, rect[3]) - max(t, rect[1]))
        area = inter_w * inter_h
        if area > best_area:
            best, best_area = i, area
    if best_area > 0:
        return best
    for i, (l, t, r, b) in enumerate(monitors):
        if l <= cx < r and t <= cy < b:
            return i
    return 0


def monitor_containing_point(x: int, y: int,
                             monitors: List[Tuple[int, int, int, int]]) -> int:
    for i, (l, t, r, b) in enumerate(monitors):
        if l <= x < r and t <= y < b:
            return i
    return 0


def likely_fullscreen_window(hwnd: int, monitors: List[Tuple[int, int, int, int]]) -> bool:
    """启发式：窗口是否铺满某块显示器（无法 100% 判断独占全屏，仅用于提示）。"""
    rect = get_window_rect(hwnd)
    if not rect:
        return False
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    idx = window_monitor_index(hwnd, monitors)
    if idx >= len(monitors):
        return False
    l, t, r, b = monitors[idx]
    return w >= (r - l) * 0.98 and h >= (b - t) * 0.98
