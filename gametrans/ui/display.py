"""显示层：把译文呈现给玩家。

两种模式（cfg.display.mode）：
- overlay（覆盖原文）：译文按 OCR 行框画在识别区域上，窗口无边框置顶、
  鼠标穿透、几何跟随锚定（游戏拖动/缩放时区域同步刷新）。
- side（独立小窗）：最近若干行译文显示在侧边置顶小窗，可拖动。

坐标约定：OCR 行坐标 box_norm 为相对识别区域 0..1 的归一化矩形；
overlay 窗口几何 == 识别区域物理矩形，因此 box_norm*window 即屏幕像素位置。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QApplication, QWidget

from ..config import Config
from ..const import MODE_OVERLAY, MODE_SIDE
from ..core import regions
from ..utils import win32
from .common import parse_color

log = logging.getLogger(__name__)


def _rect_from_norm(box, w: float, h: float) -> QRectF:
    x0, y0, x1, y1 = box
    return QRectF(x0 * w, y0 * h, max(1.0, (x1 - x0) * w), max(1.0, (y1 - y0) * h))


def _pick_font(d: dict, size_px: int) -> QFont:
    """按 display 配置选字体；空值回退微软雅黑（Windows 中文字体最稳）。"""
    fam = str(d.get("font_family", "") or "").strip()
    font = QFont(fam or "Microsoft YaHei UI")
    font.setPixelSize(size_px)
    # 轻微字间距：中文小字号下更易读（Netflix 式排版）
    font.setLetterSpacing(QFont.AbsoluteSpacing, 0.4)
    return font


class OverlayWindow(QWidget):
    """译文覆盖窗：画在识别区域上，鼠标点击穿透到下层游戏。"""

    def __init__(self, cfg: Config) -> None:
        super().__init__(None)
        self.cfg = cfg
        self._items: list = []  # [{"box": [x0,y0,x1,y1] 归一化 或 None, "text": str}]
        self._click_through_done = False
        self._capture_excluded = False  # WDA_EXCLUDEFROMCAPTURE 是否生效
        # 字号自适应结果缓存：paintEvent 每帧都要重绘，逐像素试字号会做上百次
        # 文本布局（长字幕下明显掉帧）。缓存键含文本/可用区尺寸/基准字号，
        # 任一变化都会重新计算，因此改了设置也能立即生效。
        self._size_cache: dict = {}

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    @property
    def capture_excluded(self) -> bool:
        """屏幕抓取是否已排除本窗口（OCR 将拿到纯净底层画面）。"""
        return self._capture_excluded

    # ---------- 数据 ----------
    def set_items(self, items: list) -> None:
        self._items = items
        self.update()

    # ---------- 事件 ----------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._click_through_done and self.winId():
            self._click_through_done = True
            win32.set_click_through(int(self.winId()), True)
            win32.set_always_on_top(int(self.winId()), True)
            # 关键：让译文层从屏幕捕获中消失——OCR 抓到的永远是底层原文，
            # 无需“眨眼”隐检，不闪、不回声、CPU 低。旧系统不支持则回退。
            self._capture_excluded = win32.set_capture_exclude(
                int(self.winId()), True)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        d = self.cfg.get("display", default={})
        hide_source = bool(d.get("hide_source", True))
        text_color = parse_color(d.get("text_color", "#FFFFFF"))
        bg_color = parse_color(d.get("bg_color", "#1A1A1A"))
        bg_alpha = int(d.get("bg_alpha", 170))
        corner = int(d.get("corner_radius", 6))
        base_font_px = int(d.get("font_size", 19))
        pad = int(d.get("padding", 6))

        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        for it in self._items:
            text = (it.get("text") or "").strip()
            box = it.get("box")
            if not text or not box:
                continue
            box_rect = _rect_from_norm(box, w, h)
            if box_rect.width() < 8 or box_rect.height() < 4:
                continue
            # 背景块整体外扩：完全盖住原文字区域，让用户一眼看出“哪块被翻译了”
            grow = max(3.0, pad / 2.0 + 2.0)
            bg_rect = box_rect.adjusted(-grow, -grow, grow, grow)
            inner = bg_rect.adjusted(pad, pad / 2, -pad, -pad / 2)

            # 字号双向自适应：目标“刚好填满选区”——
            # 放不下则缩小（下限 8）；放得下则逐步放大直到贴满（上限受块高约束）
            draw_flags = Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap
            irect = inner.toRect()

            def _fits(px: int) -> bool:
                b = QFontMetrics(_pick_font(d, px)).boundingRect(
                    irect, draw_flags, text)
                return b.width() <= irect.width() and b.height() <= irect.height()

            size = base_font_px
            cache_key = (text, irect.width(), irect.height(), base_font_px,
                         str(d.get("font_family", "") or ""))
            cached = self._size_cache.get(cache_key)
            if cached is not None:
                size = cached
            else:
                while size > 8 and not _fits(size):
                    size -= 1
                cap = max(base_font_px, min(150, irect.height()))
                # 放大步长取 4：渲染更快，视觉效果几乎无差别
                while size + 4 <= cap and _fits(size + 4):
                    size += 4
                if len(self._size_cache) > 256:
                    self._size_cache.clear()  # 防无限增长（字幕文本会不断变化）
                self._size_cache[cache_key] = size
            font = _pick_font(d, size)
            overflow = not _fits(size)
            if overflow:
                # 极长文本在最小字号仍放不下时，改顶对齐保证开头可见
                draw_flags = Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap

            # hide_source=True：近乎不透明，彻底盖住原文（沉浸式）；
            # False：低透明底，保留原文可见、译文叠加对照。
            bg_alpha_eff = max(bg_alpha, 240) if hide_source else int(bg_alpha * 0.35)
            bg = QColor(bg_color)
            bg.setAlpha(min(255, bg_alpha_eff))
            # 竖向微渐变（顶部略暗）：玻璃质感层次
            grad = QLinearGradient(bg_rect.topLeft(), bg_rect.bottomLeft())
            c_top = QColor(bg)
            c_top.setAlpha(int(bg.alpha() * 0.90))
            grad.setColorAt(0.0, c_top)
            grad.setColorAt(1.0, bg)
            # 柔和投影（先画，垫在主体之下）：让译文块“浮”在画面上
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 110))
            painter.drawRoundedRect(bg_rect.translated(0, 4), corner, corner)
            # 渐变玻璃主体
            painter.setBrush(grad)
            painter.drawRoundedRect(bg_rect, corner, corner)
            # 2px accent 描边：边界一眼可见
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(108, 156, 255, 130), 2))
            painter.drawRoundedRect(bg_rect, corner, corner)

            painter.setFont(font)
            # 柔和阴影（0,1）+ 细描边：任何底色上都清晰可读
            shadow = QColor(0, 0, 0, 170)
            painter.setPen(shadow)
            for dx, dy in ((0, 1), (-1, 0), (1, 0), (0, -1)):
                painter.drawText(inner.translated(dx, dy), draw_flags, text)
            painter.setPen(text_color)
            painter.drawText(inner, draw_flags, text)


class SideWindow(QWidget):
    """独立侧边小窗：显示最近若干行译文，可拖动。"""

    ROW_H = 26

    def __init__(self, cfg: Config) -> None:
        super().__init__(None)
        self.cfg = cfg
        self._lines: list = []
        self._drag_offset = None

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")
        self._apply_layout()

    def _apply_layout(self) -> None:
        d = self.cfg.get("display", default={})
        scale = float(self.cfg.get("ui_scale", default=1.25) or 1.0)
        w_px = int(d.get("side_width", 460))
        # 小窗字号/行高随界面倍率放大，与控制条视觉一致
        font_px = max(10, round(int(d.get("side_font_size", 16)) * scale))
        self._row_h = max(20, round(self.ROW_H * scale))
        self._font_px = font_px
        # 依据文本量估算所需行数（中文按字宽≈字号，留边距）
        text = getattr(self, "_text", "")
        per_line = max(6, (w_px - 24) // max(8, font_px))
        rows = 2
        if text:
            rows = 0
            for para in text.split("\n") or [""]:
                rows += max(1, -(-len(para) // per_line))
        rows = max(2, min(24, rows))
        row_h = max(getattr(self, "_row_h", self.ROW_H), font_px + 10)
        self.setFixedSize(w_px, rows * row_h + 20)

    def set_lines(self, lines: list) -> None:
        d = self.cfg.get("display", default={})
        max_lines = int(d.get("max_lines", 6))
        self._lines = lines[-max_lines:]
        self._text = "\n".join(self._lines)
        self._apply_layout()
        self.update()

    # ---------- 拖动 ----------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None

    # ---------- 绘制 ----------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        d = self.cfg.get("display", default={})
        text_color = parse_color(d.get("text_color", "#FFFFFF"))
        bg = parse_color(d.get("bg_color", "#1A1A1A"))
        bg.setAlpha(int(d.get("side_opacity", 0.94) * 255))

        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        # 1px 半透明描边（玻璃质感）
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 46), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        # 顶部 accent 色条（品牌识别）
        painter.setBrush(QColor(108, 156, 255, 210))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect().adjusted(14, 6, -14, -self.height() + 12),
                                2, 2)

        font = _pick_font(d, self._font_px)
        painter.setFont(font)
        painter.setPen(text_color)
        area = self.rect().adjusted(12, 10, -12, -10)
        painter.drawText(area,
                         Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                         getattr(self, "_text", ""))


class DisplayController:
    """按 display.mode 管理显示窗口；overlay 随锚定区域同步几何。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._window = None
        self._mode = str(cfg.get("display", default={}).get("mode", MODE_OVERLAY))
        self._timer = QTimer()
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._poll_geometry)
        self._items: list = []
        # 窗口句柄缓存：窗口锚定时 resolve_hwnd 会枚举全系统窗口（12 次/秒太浪费）。
        # 句柄本身很稳定，2 秒重解析一次即可；几何仍每 120ms 读一次，拖动跟手不受影响。
        self._hwnd_cache: Optional[int] = None
        self._hwnd_ts = 0.0

    # ---------- 生命周期 ----------
    def show(self) -> None:
        self._ensure_window()
        win = self._window
        if win is None:
            return
        if self.mode() == MODE_OVERLAY:
            # overlay 定位交给 _poll_geometry：立即可见时一次定位到位，避免
            # 默认 640x480 闪现；区域暂时缺失时保持隐藏，出现后自动显示。
            self._timer.start()
            self._poll_geometry()
        else:
            win.show()
            win.raise_()

    def hide(self) -> None:
        self._timer.stop()
        if self._window is not None:
            self._window.hide()

    def is_visible(self) -> bool:
        """译文层当前是否可见（用于"窗口不在前台"时暂停/恢复）。"""
        win = self._window
        return bool(win is not None and win.isVisible())

    def stop(self) -> None:
        self.hide()
        self._window = None
        self._mode = None

    def set_mode(self, mode: str) -> None:
        if mode == self._mode and self._window is not None:
            return
        self._mode = mode
        self._ensure_window()

    def mode(self) -> str:
        return self._mode or self.cfg.get("display", default={}).get("mode", MODE_OVERLAY)

    def _ensure_window(self) -> None:
        mode = self.mode()
        self._timer.stop()
        self._hwnd_cache = None  # 重建窗口时锚定可能已变，句柄缓存作废
        if self._window is not None:
            self._window.deleteLater()
            self._window = None
        if mode == MODE_SIDE:
            self._window = SideWindow(self.cfg)
            self._place_side()
        else:
            self._window = OverlayWindow(self.cfg)
        if self._items and mode == MODE_SIDE:
            self._window.set_lines([i["text"] for i in self._items if i.get("text")])
        elif self._items:
            self._window.set_items(self._items)
        if self._window is not None and self._window.isVisible():
            if mode == MODE_OVERLAY:
                self._timer.start()

    def _place_side(self) -> None:
        if self._window is None:
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self._window.move(geo.right() - self._window.width() - 16, geo.top() + 90)

    def clear_payload(self) -> None:
        """清空当前译文（字幕结束后调用，译文块随之消失）。"""
        self._items = []
        if self._window is None:
            return
        if self.mode() == MODE_SIDE:
            self._window.set_lines([])
        else:
            self._window.set_items([])

    def capture_excluded(self) -> bool:
        """当前 overlay 窗口是否已从屏幕捕获中排除（决定是否需要隐检回退）。"""
        win = self._window
        return bool(win is not None and getattr(win, "capture_excluded", False))

    def set_transparent(self, transparent: bool) -> None:
        """隐检回退用：把译文层变全透明（窗口保持原位，用户知道区域在哪）。"""
        win = self._window
        if win is not None:
            win.setWindowOpacity(0.0 if transparent else 1.0)

    def set_block(self, text: str) -> None:
        """整页/文档模式：一个大块覆盖整个识别区域，内含全部译文。"""
        text = (text or "").strip()
        self._items = [{"box": [0.0, 0.0, 1.0, 1.0], "text": text}] if text else []
        if self._window is None:
            return
        if self.mode() == MODE_SIDE:
            self._window.set_lines([ln for ln in text.splitlines() if ln.strip()])
        else:
            self._window.set_items(self._items)

    # ---------- 数据流 ----------
    def set_payload(self, lines, dst: list) -> None:
        """lines 为 OCR 行（含 box_norm/.text），dst 为等长译文。"""
        items = []
        for ln, zh in zip(lines, dst):
            text = (zh or "").strip()
            if not text:
                continue
            items.append({"box": ln.box_norm, "text": text})
        self._items = items
        if self._window is None:
            return
        if self.mode() == MODE_SIDE:
            self._window.set_lines([i["text"] for i in items])
        else:
            self._window.set_items(items)

    # ---------- overlay 跟随 ----------
    def _base_rect_cached(self):
        """锚定基准矩形。窗口锚定下带句柄缓存：resolve_hwnd 会枚举全系统窗口，
        每 120ms 调一次太浪费；句柄稳定，2 秒重解析一次足够。"""
        anchor = self.cfg.get("anchor", default={})
        if anchor.get("type") != "window":
            return regions.monitor_rect(self.cfg)
        now = time.monotonic()
        # 按时间节流（而不是判断句柄是否为空）：找不到窗口时同样只每 2 秒
        # 重试一次，否则会退化成"每 120ms 枚举一次全系统窗口"。
        if now - self._hwnd_ts > 2.0:
            self._hwnd_cache = regions.resolve_hwnd(self.cfg)
            self._hwnd_ts = now
        if self._hwnd_cache is None:
            return None
        return regions.client_rect(self._hwnd_cache)

    def _poll_geometry(self) -> None:
        win = self._window
        if win is None or self.mode() != MODE_OVERLAY:
            return
        base = self._base_rect_cached()
        rect = regions.region_to_abs(base, self.cfg.get("region")) if base else None
        if rect is None:
            win.hide()
            return
        l, t, w, h = rect
        if (l, t, w, h) != (win.x(), win.y(), win.width(), win.height()):
            win.setGeometry(l, t, w, h)
            win.raise_()
        if not win.isVisible():
            win.show()
            win.raise_()
