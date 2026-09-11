"""显示层测试：坐标换算、字号自适应缓存、模式切换、锚定句柄缓存（offscreen）。"""
from __future__ import annotations

from gametrans.config import Config
from gametrans.core import regions
from gametrans.ui.display import (
    DisplayController,
    OverlayWindow,
    SideWindow,
    _pick_font,
    _rect_from_norm,
)
from tests.helpers import isolate_home, patched, qt_app


def _cfg() -> Config:
    home = isolate_home()
    return Config(home / "config.json")


# ---------- 纯函数 ----------
def test_rect_from_norm():
    r = _rect_from_norm([0.0, 0.0, 0.5, 0.25], 800, 400)
    assert (r.x(), r.y(), r.width(), r.height()) == (0.0, 0.0, 400.0, 100.0)


def test_rect_from_norm_min_size():
    r = _rect_from_norm([0.5, 0.5, 0.5, 0.5], 800, 400)
    assert r.width() >= 1.0 and r.height() >= 1.0  # 不产生 0 尺寸


def test_pick_font_fallback_family():
    f = _pick_font({}, 20)
    assert f.pixelSize() == 20
    f2 = _pick_font({"font_family": "SimSun"}, 26)
    assert f2.pixelSize() == 26


# ---------- Overlay 字号缓存 ----------
def test_overlay_font_size_cache_is_reused():
    app = qt_app()
    cfg = _cfg()
    win = OverlayWindow(cfg)
    win.resize(800, 220)
    win.set_items([{"box": [0.0, 0.0, 1.0, 1.0], "text": "龙在黎明时醒来。"}])
    win.show()
    app.processEvents()
    win.grab()  # 触发 paintEvent
    first = len(win._size_cache)
    assert first >= 1, "首次绘制后应写入字号缓存"
    win.grab()
    win.grab()
    assert len(win._size_cache) == first, "重复绘制应复用缓存，不重复计算"
    win.close()


def test_overlay_font_cache_key_includes_base_font():
    """改了基准字号后必须重新计算（缓存键包含基准字号）。"""
    app = qt_app()
    cfg = _cfg()
    win = OverlayWindow(cfg)
    win.resize(800, 220)
    win.set_items([{"box": [0.0, 0.0, 1.0, 1.0], "text": "龙在黎明时醒来。"}])
    win.show()
    app.processEvents()
    win.grab()
    n1 = len(win._size_cache)
    cfg.set("display", "font_size", 40)
    win.grab()
    assert len(win._size_cache) > n1, "基准字号变化后应产生新的缓存条目"
    win.close()


def test_overlay_paint_handles_empty_items():
    app = qt_app()
    win = OverlayWindow(_cfg())
    win.resize(400, 120)
    win.show()
    app.processEvents()
    win.grab()  # 无内容时不应抛异常
    win.close()


def test_overlay_skips_items_without_text_or_box():
    app = qt_app()
    win = OverlayWindow(_cfg())
    win.resize(400, 120)
    win.set_items([{"box": None, "text": "有字无框"},
                   {"box": [0, 0, 1, 1], "text": "   "}])
    win.show()
    app.processEvents()
    win.grab()  # 非法条目应被安全跳过
    win.close()


# ---------- DisplayController ----------
def test_display_controller_default_mode_overlay():
    qt_app()
    dc = DisplayController(_cfg())
    assert dc.mode() == "overlay"
    assert dc.is_visible() is False


def test_display_controller_mode_switch():
    app = qt_app()
    cfg = _cfg()
    dc = DisplayController(cfg)
    dc.set_mode("side")
    assert isinstance(dc._window, SideWindow)
    dc.set_mode("overlay")
    assert isinstance(dc._window, OverlayWindow)
    app.processEvents()


def test_display_controller_payload_filters_empty_translation():
    qt_app()
    dc = DisplayController(_cfg())
    from tests.helpers import fake_lines
    lines = fake_lines(["Hello", "World"])
    dc.set_payload(lines, ["你好", ""])
    assert len(dc._items) == 1 and dc._items[0]["text"] == "你好"


def test_display_controller_clear_payload():
    qt_app()
    dc = DisplayController(_cfg())
    from tests.helpers import fake_lines
    dc.set_payload(fake_lines(["Hi"]), ["你好"])
    assert dc._items
    dc.clear_payload()
    assert dc._items == []


def test_display_controller_screen_anchor_uses_monitor_rect():
    qt_app()
    cfg = _cfg()
    cfg.set("anchor", "type", "screen")
    dc = DisplayController(cfg)
    assert dc._base_rect_cached() == regions.monitor_rect(cfg)


def test_display_controller_caches_hwnd_lookup():
    """窗口锚定下不能每 120ms 枚举一次全系统窗口。"""
    qt_app()
    cfg = _cfg()
    cfg.set("anchor", "type", "window")
    cfg.set("anchor", "title_contains", "不存在的窗口标题_ZZZ")
    dc = DisplayController(cfg)

    calls = {"n": 0}
    real = regions.resolve_hwnd

    def counting(c):
        calls["n"] += 1
        return real(c)

    with patched(regions, "resolve_hwnd", counting):
        for _ in range(6):
            dc._base_rect_cached()
    assert calls["n"] == 1, f"句柄查询应被缓存，实际调用 {calls['n']} 次"


def test_display_controller_rebuild_clears_hwnd_cache():
    qt_app()
    cfg = _cfg()
    dc = DisplayController(cfg)
    dc._hwnd_cache = 12345
    dc.set_mode("side")
    assert dc._hwnd_cache is None, "重建窗口后句柄缓存应作废"


# ---------- SideWindow ----------
def test_side_window_scales_with_ui_scale():
    qt_app()
    cfg = _cfg()
    cfg.set("ui_scale", 1.0)
    cfg.set("display", "side_font_size", 16)
    small = SideWindow(cfg)
    cfg.set("ui_scale", 1.5)
    big = SideWindow(cfg)
    assert big._font_px > small._font_px
    assert big._row_h > small._row_h


def test_side_window_resizes_with_text():
    app = qt_app()
    win = SideWindow(_cfg())
    win.set_lines(["短句"])
    h1 = win.height()
    win.set_lines(["很长的一句话" * 30])
    assert win.height() > h1, "文本更多时窗口应变高"
    app.processEvents()
