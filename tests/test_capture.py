"""捕获引擎测试：帧差异去重与"稳定判定"（不启动线程、不抓屏）。"""
from __future__ import annotations

import time

import numpy as np

from gametrans.config import Config
from gametrans.core.capture import CaptureEngine
from tests.helpers import isolate_home


def _engine(on_frame=None, settle_ms: int = 100, threshold: float = 0.03,
            interval_ms: int = 33):
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("settle_ms", settle_ms)
    cfg.set("frame_diff_threshold", threshold)
    cfg.set("capture_interval_ms", interval_ms)
    out: list = []
    eng = CaptureEngine(cfg, on_frame=on_frame or (lambda img, rect: out.append((img, rect))))
    return eng, cfg, out


def _frame(value: int = 0, w: int = 64, h: int = 64) -> np.ndarray:
    f = np.zeros((h, w, 3), np.uint8)
    f[:] = value
    return f


RECT = (0, 0, 64, 64)


# ---------- 帧差异 ----------
def test_first_frame_always_changed():
    eng, _cfg, _ = _engine()
    assert eng._changed(_frame(0)) is True, "无基准时首帧必须触发（启动即有字幕）"


def test_same_frame_not_changed():
    eng, _cfg, _ = _engine()
    eng._changed(_frame(10))
    assert eng._changed(_frame(10)) is False


def test_big_change_detected():
    eng, _cfg, _ = _engine()
    eng._changed(_frame(0))
    assert eng._changed(_frame(255)) is True


def test_reset_diff_forces_next_frame():
    eng, _cfg, _ = _engine()
    eng._changed(_frame(10))
    assert eng._changed(_frame(10)) is False
    eng.reset_diff()
    assert eng._changed(_frame(10)) is True


def test_small_change_below_threshold_ignored():
    eng, _cfg, _ = _engine(threshold=0.5)
    eng._changed(_frame(0))
    assert eng._changed(_frame(10)) is False, "低于阈值的变化不该触发"


# ---------- 稳定判定 ----------
def test_settle_zero_emits_immediately():
    eng, _cfg, out = _engine(settle_ms=0)
    eng._feed(_frame(1), RECT)
    assert len(out) == 1, "settle=0 时应立即出帧"


def test_settle_delays_emit_until_stable():
    eng, _cfg, out = _engine(settle_ms=80)
    eng._feed(_frame(1), RECT)          # 变化 → 暂存，不立即送
    assert out == [], "画面刚变化时不应立即送 OCR"
    eng._feed(_frame(1), RECT)          # 画面静止：开始累积稳定时长
    time.sleep(0.02)
    eng._feed(_frame(1), RECT)
    assert out == [], "稳定时长不足时不应送 OCR"
    time.sleep(0.12)
    eng._feed(_frame(1), RECT)
    assert len(out) == 1, "稳定超过 settle 后应送 OCR"
    assert isinstance(out[0][0], np.ndarray)


def test_continuous_change_forces_emit_within_max_wait():
    """画面一直变（动画/滚动）时，必须在最长等待内强制出帧，不能永远等下去。"""
    eng, _cfg, out = _engine(settle_ms=200)  # 最长等待 = max(0.8, 0.6) = 0.8s
    t0 = time.monotonic()
    value = 0
    while time.monotonic() - t0 < 3.0 and not out:
        value = (value + 40) % 255
        eng._feed(_frame(value), RECT)
        time.sleep(0.03)
    elapsed = time.monotonic() - t0
    assert out, "持续变化时未能强制出帧（会永远识别不到内容）"
    assert elapsed < 2.5, f"强制出帧太慢: {elapsed:.2f}s"


def test_reset_clears_pending_frame():
    eng, _cfg, out = _engine(settle_ms=500)
    eng._feed(_frame(1), RECT)
    assert eng._pending is not None
    eng.reset_diff()
    assert eng._pending is None and eng._stable_since is None
    time.sleep(0.05)
    eng._feed(_frame(1), RECT)   # 重置后首帧视为变化 → 仍不立即送（settle>0）
    assert out == []


def test_frame_without_callback_is_safe():
    """没有回调（或已停机）时喂帧不应抛异常。"""
    eng, _cfg, _out = _engine(settle_ms=0)
    eng._on_frame = None
    eng._feed(_frame(3), RECT)


def test_prev_frame_shape_change_is_safe():
    """分辨率变化（换显示器/缩放窗口）时不应报错。"""
    eng, _cfg, _ = _engine()
    eng._changed(_frame(0, 64, 64))
    eng._changed(_frame(0, 32, 32))  # 形状不同，直接视为变化
