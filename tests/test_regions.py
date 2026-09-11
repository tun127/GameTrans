"""区域模块测试：坐标归一化/还原、屏幕锚定、选择保存。"""
from __future__ import annotations

from gametrans.config import Config
from gametrans.core import regions
from tests.helpers import isolate_home


def _cfg():
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("anchor", "type", "screen")
    cfg.set("anchor", "monitor_index", 0)
    return cfg


def test_region_of_abs_normalizes():
    r = regions.region_of_abs((0, 0, 1000, 500), (100, 50, 500, 250))
    assert r["nx"] == 0.1 and r["ny"] == 0.1
    assert r["nw"] == 0.5 and r["nh"] == 0.5


def test_region_of_abs_clamps_when_out_of_base():
    r = regions.region_of_abs((0, 0, 100, 100), (50, 50, 500, 500))
    assert r["nw"] <= 0.5 + 1e-6 and r["nh"] <= 0.5 + 1e-6
    assert r["nx"] + r["nw"] <= 1.0 + 1e-6
    assert r["ny"] + r["nh"] <= 1.0 + 1e-6


def test_region_to_abs_roundtrip():
    l, t, w, h = regions.region_to_abs(
        (100, 200, 800, 600), {"nx": 0.25, "ny": 0.5, "nw": 0.5, "nh": 0.25})
    assert (l, t, w, h) == (300, 500, 400, 150)


def test_region_to_abs_none_returns_none():
    """region=None 表示"尚未框选"，应返回 None 而不是整个 base。"""
    assert regions.region_to_abs((10, 20, 100, 50), None) is None
    assert regions.region_to_abs((10, 20, 100, 50), {}) is None


def test_region_to_abs_clamps_overflow():
    l, t, w, h = regions.region_to_abs(
        (0, 0, 100, 100), {"nx": 0.9, "ny": 0.9, "nw": 0.5, "nh": 0.5})
    assert l + w <= 100 and t + h <= 100


def test_monitor_rect_available():
    rect = regions.monitor_rect(_cfg())
    assert rect is not None and rect[2] > 0 and rect[3] > 0


def test_target_is_foreground_with_screen_anchor():
    assert regions.target_is_foreground(_cfg()) is True


def test_current_base_rect_matches_monitor():
    cfg = _cfg()
    assert regions.current_base_rect(cfg) == regions.monitor_rect(cfg)


def test_save_abs_selection_roundtrip():
    cfg = _cfg()
    base = regions.monitor_rect(cfg)
    l, t, w, h = base
    sel = (l + int(w * 0.1), t + int(h * 0.2), int(w * 0.5), int(h * 0.25))
    assert regions.save_abs_selection(cfg, sel) is True
    region = cfg.get("region")
    assert region is not None
    assert abs(region["nx"] - 0.1) < 0.02, region
    back = regions.region_to_abs(base, region)
    assert abs(back[0] - sel[0]) <= 2 and abs(back[2] - sel[2]) <= 2


def test_snapshot_region_desc_is_text():
    cfg = _cfg()
    cfg.set("region", None)
    assert isinstance(regions.snapshot_region_desc(cfg), str)
    cfg.set("region", {"nx": 0.0, "ny": 0.4, "nw": 1.0, "nh": 0.2})
    assert regions.snapshot_region_desc(cfg)


def test_resolve_hwnd_for_unknown_title():
    cfg = _cfg()
    cfg.set("anchor", "type", "window")
    cfg.set("anchor", "title_contains", "绝对不存在的窗口标题_ZZZ")
    assert regions.resolve_hwnd(cfg) is None


def test_client_rect_invalid_hwnd():
    assert regions.client_rect(0) is None


def test_target_is_foreground_window_anchor_without_window():
    cfg = _cfg()
    cfg.set("anchor", "type", "window")
    cfg.set("anchor", "title_contains", "绝对不存在的窗口标题_ZZZ")
    assert regions.target_is_foreground(cfg) is False
