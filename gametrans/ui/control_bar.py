"""极简悬浮控制条：常驻置顶小面板，负责开关翻译/框选区域/语言/显示模式/设置/退出。"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPaintEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from ..config import Config
from ..const import (
    LANG_AUTO,
    SRC_LANG_CYCLE,
    SRC_LANG_NAMES,
    MODE_OVERLAY,
    MODE_SIDE,
    MODE_NAMES,
    MODE_CYCLE,
)
from .common import ACCENT, BG_DARK, OK_GREEN, TEXT_DIM, TEXT_MAIN

log = logging.getLogger(__name__)

def build_qss(scale: float) -> str:
    """按界面缩放倍率生成样式表。

    所有尺寸都按倍率放大（字号/内边距/圆角/最小宽度），避免简单拉伸导致发虚。
    """
    s = float(scale or 1.0)
    fs = max(12, round(15 * s))         # 按钮字号
    fs_title = max(13, round(15 * s))   # 标题字号
    pad_v = max(5, round(7 * s))
    pad_h = max(8, round(13 * s))
    radius = max(6, round(8 * s))
    min_w = max(48, round(64 * s))
    return f"""
QToolButton {{
    background: transparent; border: none; border-radius: {radius}px;
    padding: {pad_v}px {pad_h}px; color: {TEXT_MAIN}; font-size: {fs}px;
}}
QToolButton:focus {{ outline: none; }}
QToolButton:hover {{ background: rgba(108,156,255,0.16); }}
QToolButton:pressed {{ background: rgba(108,156,255,0.28); }}
QToolButton#runOn {{
    color: white; font-weight: 700; min-width: {min_w}px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #6C9CFF, stop:1 #4A76E8);
    border: 1px solid rgba(255,255,255,0.30);
}}
QToolButton#runOn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #7FABFF, stop:1 #5583F0);
}}
QToolButton#runOff {{
    color: {ACCENT}; border: 1px solid rgba(108,156,255,0.55);
    font-weight: 700; min-width: {min_w}px;
}}
QToolButton#runOff:hover {{ background: rgba(108,156,255,0.14); }}
QLabel#title {{
    color: {ACCENT}; font-weight: 800; font-size: {fs_title}px;
    letter-spacing: 1.2px; padding: 0 {max(2, round(4 * s))}px;
}}
QToolButton#lang {{ color: {TEXT_DIM}; }}
"""


class ControlBar(QWidget):
    """控制条本体。通过信号与主控制器交互，不在内部直接处理业务逻辑。"""

    # 用户动作信号
    toggle_requested = Signal(bool)   # 是否开启翻译
    region_requested = Signal()       # 请求框选识别区域
    pick_window_requested = Signal()  # 请求选择目标窗口
    language_changed = Signal(str)    # 新源语言
    mode_changed = Signal(str)        # 新显示模式
    settings_requested = Signal()     # 打开设置
    quit_requested = Signal()         # 退出程序

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = config
        self._drag_offset = None
        self._scale = float(config.get("ui_scale", default=1.25) or 1.0)

        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._build()
        self.apply_scale(self._scale)  # 高度/样式/间距一次性按倍率应用
        self._load_state()
        self.set_running(False)  # 初始为“开始”态（文本/样式就位）
        log.info("控制条初始化完成（缩放 %.2f）", self._scale)

    # ---------- UI ----------
    def _build(self) -> None:
        self.lab_title = QLabel("GameTrans")
        self.lab_title.setObjectName("title")

        self.btn_run = QToolButton()
        self.btn_run.setCheckable(True)
        self.btn_run.setObjectName("runOff")
        self.btn_run.setToolTip("开始/停止翻译")
        self.btn_run.clicked.connect(self._on_run_clicked)

        self.btn_pick = QToolButton()
        self.btn_pick.setObjectName("lang")
        self.btn_pick.setText("整屏")
        self.btn_pick.setToolTip("选择要跟随的游戏窗口（默认整屏；选窗口后拖动/缩放会自动跟随）")
        self.btn_pick.clicked.connect(self.pick_window_requested.emit)

        self.btn_region = QToolButton()
        self.btn_region.setText("框选区域")
        self.btn_region.setToolTip("用鼠标框选字幕出现的屏幕区域")
        self.btn_region.clicked.connect(self.region_requested.emit)

        self.btn_lang = QToolButton()
        self.btn_lang.setObjectName("lang")
        self.btn_lang.setToolTip("切换源语言：自动 / 英文 / 日文")
        self.btn_lang.clicked.connect(self._on_lang_clicked)

        self.btn_mode = QToolButton()
        self.btn_mode.setObjectName("lang")
        self.btn_mode.setToolTip("切换显示模式：覆盖原文 / 独立小窗")
        self.btn_mode.clicked.connect(self._on_mode_clicked)

        self.btn_settings = QToolButton()
        self.btn_settings.setText("设置")
        self.btn_settings.clicked.connect(self.settings_requested.emit)

        self.btn_quit = QToolButton()
        self.btn_quit.setText("退出")
        self.btn_quit.setToolTip("退出 GameTrans")
        self.btn_quit.clicked.connect(self.quit_requested.emit)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 6, 12, 6)  # 具体数值由 apply_scale 按倍率重设
        lay.setSpacing(4)
        lay.addWidget(self.lab_title)
        lay.addSpacing(6)
        lay.addWidget(self.btn_run)
        lay.addWidget(self.btn_pick)
        lay.addWidget(self.btn_region)
        lay.addWidget(self.btn_lang)
        lay.addWidget(self.btn_mode)
        lay.addSpacing(4)
        lay.addWidget(self.btn_settings)
        lay.addWidget(self.btn_quit)

    def apply_scale(self, scale: float) -> None:
        """设置里改了界面倍率后即时生效（无需重启）。"""
        s = float(scale or 1.0)
        self._scale = s
        self.setStyleSheet(build_qss(s))
        # 高度随倍率放大：52(1.0) / 65(1.25) / 78(1.5)
        self.setFixedHeight(max(40, round(52 * s)))
        lay = self.layout()
        if lay is not None:
            mg = max(8, round(14 * s))
            mv = max(4, round(6 * s))
            lay.setContentsMargins(mg, mv, max(6, round(12 * s)), mv)
            lay.setSpacing(max(3, round(4 * s)))
            lay.invalidate()
        self.adjustSize()

    # ---------- 状态 ----------
    def _load_state(self) -> None:
        lang = self.cfg.get("source_lang", default=LANG_AUTO)
        mode = self.cfg.get("display", default={}).get("mode", MODE_OVERLAY)
        self.btn_lang.setText(SRC_LANG_NAMES.get(lang, "自动"))
        self.btn_mode.setText(MODE_NAMES.get(mode, "覆盖原文"))

    def set_running(self, running: bool) -> None:
        self.btn_run.blockSignals(True)
        self.btn_run.setChecked(running)
        if running:
            self.btn_run.setText("停止")
            self.btn_run.setObjectName("runOn")
        else:
            self.btn_run.setText("开始")
            self.btn_run.setObjectName("runOff")
        self.btn_run.style().unpolish(self.btn_run)
        self.btn_run.style().polish(self.btn_run)
        self.btn_run.blockSignals(False)

    def set_region_label(self, text: str) -> None:
        """显示当前已框选的区域描述，例如『已框选 640×180』。"""
        self.btn_region.setText(text)

    def set_pick_label(self, text: str, tooltip: str = "") -> None:
        """显示当前锚定方式：『整屏』或目标窗口名。"""
        self.btn_pick.setText(text or "整屏")
        if tooltip:
            self.btn_pick.setToolTip(tooltip)

    # ---------- 事件 ----------
    def _on_run_clicked(self, checked: bool) -> None:
        self.toggle_requested.emit(checked)

    def _on_lang_clicked(self) -> None:
        current = self.cfg.get("source_lang", default=LANG_AUTO)
        idx = SRC_LANG_CYCLE.index(current) if current in SRC_LANG_CYCLE else 0
        nxt = SRC_LANG_CYCLE[(idx + 1) % len(SRC_LANG_CYCLE)]
        self.cfg.set("source_lang", nxt)
        self.btn_lang.setText(SRC_LANG_NAMES[nxt])
        self.language_changed.emit(nxt)

    def _on_mode_clicked(self) -> None:
        current = self.cfg.get("display", default={}).get("mode", MODE_OVERLAY)
        idx = MODE_CYCLE.index(current) if current in MODE_CYCLE else 0
        nxt = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
        self.cfg.set("display", "mode", nxt)
        self.btn_mode.setText(MODE_NAMES[nxt])
        self.mode_changed.emit(nxt)

    # ---------- 拖动 ----------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None

    # ---------- 绘制 ----------
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        r = max(8, round(12 * self._scale))  # 圆角随界面倍率放大
        # 半透明玻璃底 + 1px 亮描边
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(30, 33, 38, 242))
        painter.drawRoundedRect(rect, r, r)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#3C414A"), 1))
        painter.drawRoundedRect(rect, r, r)
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), r - 1, r - 1)
