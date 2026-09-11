"""目标窗口选择对话框：列出当前可见窗口供用户挑选。"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ..utils.win32 import enumerate_windows

log = logging.getLogger(__name__)


class WindowPickerDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择目标游戏窗口")
        self.resize(520, 460)
        self._selected: Optional[dict] = None
        self._build()
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        tip = QLabel("选择要翻译的游戏窗口。选择后，识别区域将跟随该窗口移动与缩放。")
        tip.setObjectName("dim")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self.accept())
        root.addWidget(self.list, 1)

        row = QHBoxLayout()
        btn_refresh = QPushButton("重新扫描")
        btn_refresh.clicked.connect(self.refresh)
        row.addWidget(btn_refresh)
        row.addStretch(1)
        btns = QDialogButtonBox()
        b_ok = btns.addButton("确定", QDialogButtonBox.AcceptRole)
        b_ok.setObjectName("primary")
        btns.addButton("取消", QDialogButtonBox.RejectRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        row.addWidget(btns)
        root.addLayout(row)

    def refresh(self) -> None:
        self.list.clear()

        # 首项：整屏模式（不使用窗口跟随）
        item0 = QListWidgetItem("【不使用窗口】按显示器整屏框选（区域不随游戏移动）")
        item0.setData(Qt.UserRole, {"mode": "screen", "title": "", "hwnd": 0,
                                    "rect": (0, 0, 0, 0), "class": ""})
        self.list.addItem(item0)

        wins = enumerate_windows(skip_hidden=True)
        wins.sort(key=lambda w: w["title"].lower())
        for win in wins:
            title = win["title"].strip().replace("\n", " ")
            rect = win["rect"]
            item = QListWidgetItem(
                f"{title}   [{rect[2]-rect[0]}×{rect[3]-rect[1]}]"
            )
            win["mode"] = "window"
            item.setData(Qt.UserRole, win)
            item.setToolTip(f"窗口类：{win['class']}")
            self.list.addItem(item)

    def selected_window(self) -> Optional[dict]:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def accept(self) -> None:
        win = self.selected_window()
        if win is None:
            self.list.setFocus()
            return
        self._selected = win
        super().accept()

    def result_window(self) -> Optional[dict]:
        return self._selected


def pick_game_window(parent=None) -> Optional[dict]:
    """打开选择对话框，返回 {"hwnd", "title", "rect"} 或 None。"""
    dlg = WindowPickerDialog(parent)
    if dlg.exec() == QDialog.Accepted:
        return dlg.result_window()
    return None
