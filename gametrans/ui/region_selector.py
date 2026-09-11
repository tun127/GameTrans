"""屏幕框选覆盖层：进入全屏透明模式，用鼠标拖出识别区域。

返回物理像素绝对矩形 (left, top, width, height)，坐标与 mss 抓屏一致。
用户在覆盖层里看到的是全屏实时截图，因此即使游戏在运行也能框选字幕位置。
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import mss
from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QDialog

from ..utils.win32 import virtual_screen_rect

log = logging.getLogger(__name__)

Rect = Tuple[int, int, int, int]


def _screenshot_virtual() -> Optional[QPixmap]:
    """抓取整个虚拟屏幕（所有显示器）作为框选底图。"""
    l, t, r, b = virtual_screen_rect()
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    try:
        mss_factory = getattr(mss, "MSS", None) or mss.mss
        with mss_factory() as sct:
            monitor = {"left": l, "top": t, "width": w, "height": h}
            shot = sct.grab(monitor)
        img = QImage(shot.raw, shot.width, shot.height, QImage.Format_RGB32).copy()
        return QPixmap.fromImage(img)
    except Exception as exc:  # noqa: BLE001
        log.warning("截取屏幕失败: %s", exc)
        return None


class RegionSelector(QDialog):
    """覆盖全屏的半透明选框。鼠标左键拖拽，Esc/右键取消。

    必须继承 QDialog：select_screen_region 依赖 exec()/accept()/reject()
    进入模态事件循环；QWidget 没有这些方法。
    """

    def __init__(self, virtual_origin: Tuple[int, int], pixmap: Optional[QPixmap] = None) -> None:
        super().__init__(None)
        self._origin = virtual_origin          # 虚拟屏左上角在底图坐标系
        self._selection: Optional[QRect] = None
        self._start_pos = None
        self._background = pixmap

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 覆盖所有显示器组成的虚拟屏幕（物理像素，与截图像素 1:1）
        vs = virtual_screen_rect()
        self.setGeometry(vs[0], vs[1], vs[2] - vs[0], vs[3] - vs[1])
        self.setCursor(Qt.CrossCursor)

    # ---------- 事件 ----------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._start_pos = event.position().toPoint()
            self._selection = QRect(self._start_pos, self._start_pos)
            self.update()
        elif event.button() == Qt.RightButton:
            self.reject_self()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start_pos is not None and event.buttons() & Qt.LeftButton:
            p = event.position().toPoint()
            self._selection = QRect(self._start_pos, p).normalized()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._selection is not None:
            sel = self._selection.normalized()
            if sel.width() >= 24 and sel.height() >= 24:
                self.accept_rect(sel)
            else:
                self._selection = None
                self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.reject_self()
        else:
            super().keyPressEvent(event)

    # ---------- 动作 ----------
    def accept_rect(self, sel: QRect) -> None:
        # 底图 = 虚拟屏截图；全局坐标直接映射
        l = self._origin[0] + sel.left()
        t = self._origin[1] + sel.top()
        self._result = (l, t, sel.width(), sel.height())
        self.accept()
        self.close()

    def reject_self(self) -> None:
        self._result = None
        self.reject()
        self.close()

    def result_rect(self) -> Optional[Rect]:
        return getattr(self, "_result", None)

    # ---------- 绘制 ----------
    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 先整块置为透明，避免窗口自身背景把（未绘制的）选区涂成不透明黑
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        # 全屏截图底图（若抓取失败则纯暗背景）
        if self._background is not None:
            p.drawPixmap(0, 0, self._background)
        # 全屏暗色遮罩（覆盖式，稳定可靠；更暗以强化选区对比）
        p.fillRect(self.rect(), QColor(8, 10, 15, 150))
        # 顶部操作提示
        hint = "按住左键拖动，框住字幕区域 · Esc / 右键取消"
        p.setFont(self.font())
        fm = p.fontMetrics()
        hw = fm.horizontalAdvance(hint) + 24
        hx = (self.width() - hw) // 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 18, 24, 235))
        p.drawRoundedRect(hx, 14, hw, fm.height() + 14, 8, 8)
        p.setPen(QColor("#E8EAED"))
        p.drawText(hx + 12, 14 + fm.ascent() + 7, hint)

        # 选中区域：提亮一层 + 粗蓝框 + 白色 L 形四角把手（专业截图工具观感）
        if self._selection is not None:
            sel = self._selection.normalized()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 22))
            p.drawRect(sel)
            # 主题蓝粗描边 + 白色内圈
            p.setPen(QPen(QColor("#6C9CFF"), 3))
            p.drawRect(sel)
            p.setPen(QPen(QColor(255, 255, 255, 160), 1))
            p.drawRect(sel.adjusted(-4, -4, 4, 4))
            # 四角 L 形把手
            arm = 20
            h = 4
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#6C9CFF"))
            for cx, cy, dx, dy in (
                (sel.left(), sel.top(), 1, 1),
                (sel.right(), sel.top(), -1, 1),
                (sel.left(), sel.bottom(), 1, -1),
                (sel.right(), sel.bottom(), -1, -1),
            ):
                p.drawRect(QRect(min(cx, cx + dx * arm), min(cy, cy + dy * h),
                                 arm, h))
                p.drawRect(QRect(min(cx, cx + dx * h), min(cy, cy + dy * arm),
                                 h, arm))
            # 尺寸角标（半透明底 + 白字），放框外左上，放不下则框内
            tip = f"{sel.width()} × {sel.height()}"
            tfm = p.fontMetrics()
            tw = tfm.horizontalAdvance(tip) + 16
            th = tfm.height() + 8
            tx = sel.left()
            ty = sel.top() - th - 6
            if ty < 16:
                ty = sel.top() + 6
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(15, 18, 24, 235))
            p.drawRoundedRect(tx, ty, tw, th, 6, 6)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(tx + 8, ty + (th - tfm.height()) // 2 + tfm.ascent(), tip)
            # 中心辅助十字（轻微，帮助对齐）
            cx = sel.center().x()
            cy = sel.center().y()
            p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.DashLine))
            p.drawLine(cx, sel.top() + 4, cx, sel.bottom() - 4)
            p.drawLine(sel.left() + 4, cy, sel.right() - 4, cy)


def select_screen_region(parent: Optional[QWidget] = None) -> Optional[Rect]:
    """弹出全屏框选，返回物理像素绝对矩形，取消返回 None。"""
    vs = virtual_screen_rect()
    pix = _screenshot_virtual()
    sel = RegionSelector((vs[0], vs[1]), pix)
    sel.exec()
    return sel.result_rect()
