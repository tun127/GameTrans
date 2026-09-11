"""工具模块测试：热键解析、Win32 矩形/枚举、DPI 设置。"""
from __future__ import annotations

from gametrans.utils import win32
from gametrans.utils.hotkey import (
    KEY_TO_VK,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    format_hotkey,
    parse_hotkey,
)


def test_parse_hotkey_valid():
    parsed = parse_hotkey("Ctrl+Shift+T")
    assert parsed is not None
    mods, vk = parsed
    assert mods == MOD_CONTROL | MOD_SHIFT
    assert vk == KEY_TO_VK["T"]


def test_parse_hotkey_tolerant_to_spaces_and_case():
    assert parse_hotkey("ctrl + alt + f5") == (MOD_CONTROL | MOD_ALT, KEY_TO_VK["F5"])
    assert parse_hotkey("  WIN+T  ") == (MOD_WIN, KEY_TO_VK["T"])
    assert parse_hotkey("super+1") == (MOD_WIN, KEY_TO_VK["1"])


def test_parse_hotkey_rejects_invalid():
    assert parse_hotkey("") is None
    assert parse_hotkey(None) is None
    assert parse_hotkey("T") is None              # 无修饰键
    assert parse_hotkey("Ctrl+不存在") is None     # 未知键
    assert parse_hotkey("Ctrl+") is None


def test_format_hotkey_roundtrip():
    mods, vk = parse_hotkey("Ctrl+Shift+T")
    text = format_hotkey(mods, vk)
    assert "Ctrl" in text and "Shift" in text and "T" in text
    assert parse_hotkey(text) == (mods, vk)


def test_virtual_screen_rect():
    l, t, r, b = win32.virtual_screen_rect()
    assert r > l and b > t
    assert r - l >= 640 and b - t >= 480


def test_get_monitor_rects():
    rects = win32.get_monitor_rects()
    assert rects, "至少应枚举到一台显示器"
    for l, t, r, b in rects:
        assert r > l and b > t


def test_enumerate_windows_returns_list():
    wins = win32.enumerate_windows(skip_hidden=True)
    assert isinstance(wins, list)
    for w in wins:
        assert set(w) >= {"hwnd", "title", "class", "rect"}
        assert w["title"].strip()


def test_find_window_by_title_contains_empty():
    assert win32.find_window_by_title_contains("") is None


def test_window_monitor_index_falls_back():
    rects = win32.get_monitor_rects()
    assert win32.window_monitor_index(0, rects) >= 0  # 无效句柄不崩


def test_dpi_awareness_callable_twice():
    from gametrans.utils.dpi import enable_per_monitor_dpi_awareness
    enable_per_monitor_dpi_awareness()
    enable_per_monitor_dpi_awareness()  # 幂等，不抛异常


def test_win32_sets_do_not_crash():
    """置顶/穿透/排除捕获：对无效句柄应安全返回而不是抛异常。"""
    win32.set_always_on_top(0, True)
    win32.set_click_through(0, True)
    assert win32.set_capture_exclude(0, True) in (True, False)
    win32.set_click_through(0, False)
    assert win32.get_window_text(0) == ""
    assert win32.get_window_class(0) == ""
