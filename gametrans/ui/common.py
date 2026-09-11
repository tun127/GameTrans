"""UI 公共工具：全局深色样式、颜色工具、Toast 提示。"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

ACCENT = "#6C9CFF"
BG_DARK = "#14161C"
BG_PANEL = "#1D2028"
BG_INPUT = "#191C23"
TEXT_MAIN = "#F2F4F8"
TEXT_DIM = "#98A2B3"
BORDER = "#2E3340"
OK_GREEN = "#3ECF8E"
WARN_YELLOW = "#F5B041"
ERR_RED = "#FF6B6B"

# 通用深色样式表（应用于 QApplication）
GLOBAL_QSS = f"""
QWidget {{
    background-color: transparent;
    color: {TEXT_MAIN};
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
}}
QDialog {{ background-color: {BG_DARK}; }}
QFrame#panel, QGroupBox {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QGroupBox {{
    margin-top: 12px;
    padding-top: 4px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_DIM};
    font-weight: normal;
}}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 15px; font-weight: bold; }}
QLabel#dim {{ color: {TEXT_DIM}; }}
QPushButton {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background-color: #1B1E22; }}
QPushButton:disabled {{ color: #6A6F76; }}
QPushButton#primary {{
    background-color: {ACCENT};
    border: none;
    color: white;
    font-weight: bold;
}}
QPushButton#primary:hover {{ background-color: #5D9AFF; }}
QPushButton#danger:hover {{ border-color: {ERR_RED}; color: {ERR_RED}; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}
QCheckBox, QRadioButton {{ spacing: 6px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QSlider::groove:horizontal {{
    height: 4px; background: {BORDER}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -5px 0;
    border-radius: 7px; background: {ACCENT};
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 4px; min-height: 24px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    background-color: {BG_DARK};
}}
QTabBar::tab {{
    background: transparent;
    padding: 6px 16px;
    color: {TEXT_DIM};
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: white; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: white; }}
QToolTip {{
    background-color: {BG_PANEL};
    color: {TEXT_MAIN};
    border: 1px solid {BORDER};
    padding: 4px 8px;
}}
"""


def apply_global_style(app: QApplication) -> None:
    app.setStyleSheet(GLOBAL_QSS)


def parse_color(text: str, default: str = "#FFFFFF") -> QColor:
    try:
        c = QColor(text)
        return c if c.isValid() else QColor(default)
    except Exception:
        return QColor(default)


def color_to_hex(color: QColor) -> str:
    return color.name().upper()


def make_shadow(widget: QWidget, radius: int = 24, alpha: int = 140) -> None:
    """给无边框窗加投影。"""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(radius)
    shadow.setOffset(0, 0)
    shadow.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(shadow)


# 提示条固定位置：贴在屏幕角落，避免挡在游戏画面中央
TOAST_DEFAULT_CORNER = "top-right"
TOAST_CORNERS = {
    "top-right": "右上角",
    "top-left": "左上角",
    "bottom-right": "右下角",
    "bottom-left": "左下角",
    "follow": "跟随控制条",
}


class Toast(QLabel):
    """临时提示条。

    必须做成**独立顶层窗口**：作为控制条（高约 65px）的子控件时，提示会被
    放到控制条外侧（y 为负），超出父窗口的部分会被 Qt 裁剪 —— 结果是提示
    完全看不见。独立窗口后默认**固定到屏幕右上角**（设置里可换角落），
    绝不挡在游戏画面中央。
    """

    def __init__(self, anchor: QWidget | None = None,
                 corner: str = TOAST_DEFAULT_CORNER) -> None:
        super().__init__(None)  # 顶层窗口：不受任何父窗口裁剪
        self._anchor = anchor  # 仅作定位参考，不建立父子关系
        self._corner = corner if corner in TOAST_CORNERS else TOAST_DEFAULT_CORNER
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(
            f"background-color: {BG_PANEL}; color: {TEXT_MAIN};"
            f"border: 1px solid {BORDER}; border-radius: 8px; padding: 10px 16px;"
        )
        self.setWordWrap(True)
        self.setMaximumWidth(460)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, ms: int = 3200, kind: str = "info") -> None:
        color = {"info": TEXT_MAIN, "ok": OK_GREEN, "warn": WARN_YELLOW, "err": ERR_RED}.get(kind, TEXT_MAIN)
        self.setText(text)
        self.setStyleSheet(
            f"background-color: {BG_PANEL}; color: {color};"
            f"border: 1px solid {BORDER}; border-radius: 8px; padding: 10px 16px;"
        )
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self._timer.start(ms)

    def set_corner(self, corner: str) -> None:
        """切换提示位置（设置里改动后即时生效，无需重启）。"""
        c = str(corner or TOAST_DEFAULT_CORNER)
        self._corner = c if c in TOAST_CORNERS else TOAST_DEFAULT_CORNER
        if self.isVisible():
            self._place()

    def _place(self) -> None:
        """把提示条放到屏幕角落（默认右上角），不挡画面中心。

        - corner=="follow"：沿用旧行为，贴在控制条下方（用户可能把它拖到中间，
          所以默认不用这种方式）。
        - 其它角落：贴屏幕边距 16px；若与控制条重叠，自动让开按钮。
        """
        anchor = self._anchor
        screen = None
        if anchor is not None:
            try:
                screen = anchor.screen()
            except Exception:  # noqa: BLE001
                screen = None
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        margin = 16

        if self._corner == "follow" and anchor is not None and anchor.isVisible():
            pos = anchor.mapToGlobal(QPoint(anchor.width() - self.width(),
                                            anchor.height() + 6))
            x, y = pos.x(), pos.y()
        else:
            corner = ("top-right" if self._corner == "follow" else self._corner)
            x = (area.left() + margin if corner.endswith("left")
                 else area.right() - self.width() - margin)
            y = (area.top() + margin if corner.startswith("top")
                 else area.bottom() - self.height() - margin)

            # 与控制条重叠时让开，避免盖住按钮
            if anchor is not None and anchor.isVisible():
                ar = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
                tr = QRect(int(x), int(y), self.width(), self.height())
                if ar.intersects(tr):
                    if int(y) < area.center().y():
                        y = ar.bottom() + 8      # 提示在上半屏 → 移到控制条下方
                    else:
                        y = ar.top() - self.height() - 8

        # 兜底：任何情况下都夹在屏幕可用区域内，保证一定能看到
        x = max(area.left() + 8, min(int(x), area.right() - self.width() - 8))
        y = max(area.top() + 8, min(int(y), area.bottom() - self.height() - 8))
        self.move(int(x), int(y))


def attach_toast(container: QWidget, corner: str = TOAST_DEFAULT_CORNER) -> Toast:
    """创建以 container 为参考的 Toast（独立窗口，固定屏幕角落）。"""
    return Toast(container, corner)


def form_row(label: str, widget: QWidget) -> QHBoxLayout:
    """设置表单里的一行：左标签右控件。"""
    lay = QHBoxLayout()
    lab = QLabel(label)
    lab.setObjectName("dim")
    lay.addWidget(lab)
    lay.addStretch(1)
    lay.addWidget(widget)
    return lay


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color: {BORDER};")
    return f
