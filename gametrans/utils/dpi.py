"""DPI 感知设置。

必须在 QApplication 创建前调用：让 Qt 坐标与 mss 抓到的物理像素一致，
避免高分屏缩放导致框选/覆盖层错位。
"""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)


def enable_per_monitor_dpi_awareness() -> None:
    """按显示器感知 DPI（Windows 10 1607+）。失败时静默降级。"""
    try:
        # SetProcessDpiAwarenessContext(-4) = PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:  # pragma: no cover
        log.warning("DPI 感知设置失败: %s", exc)
