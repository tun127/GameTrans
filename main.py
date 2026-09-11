#!/usr/bin/env python3
"""GameTrans 入口：Python 运行方式为 `python main.py`。"""
from __future__ import annotations

import os
import sys


def main() -> int:
    # 统一像素体系：禁用 Qt 高分屏自动缩放，
    # 让 Qt 窗口几何/鼠标坐标与 mss 抓屏、Win32 API 保持一致（物理像素）。
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

    # DPI 感知必须在 QApplication 创建前设置
    from gametrans.utils.dpi import enable_per_monitor_dpi_awareness
    enable_per_monitor_dpi_awareness()

    from PySide6.QtWidgets import QApplication
    from gametrans.config import Config
    from gametrans.app import GameTransApp
    from gametrans.ui.common import apply_global_style

    app = QApplication(sys.argv)
    app.setApplicationName("GameTrans")
    app.setQuitOnLastWindowClosed(False)  # 允许托盘/控制条常驻

    apply_global_style(app)

    config = Config()
    controller = GameTransApp(config)
    controller.run()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
