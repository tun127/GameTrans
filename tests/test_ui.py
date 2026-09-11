"""UI 组件测试：提示条定位、控制条缩放、设置面板往返（offscreen，不弹窗）。"""
from __future__ import annotations

from gametrans.config import Config
from gametrans.ui.common import TOAST_CORNERS, Toast, color_to_hex, parse_color
from gametrans.ui.control_bar import ControlBar, build_qss
from tests.helpers import isolate_home, qt_app


def _cfg() -> Config:
    home = isolate_home()
    return Config(home / "config.json")


# ---------- 颜色工具 ----------
def test_color_roundtrip():
    assert color_to_hex(parse_color("#6C9CFF")).upper() == "#6C9CFF"


# ---------- 提示条 ----------
def test_toast_corner_options_exist():
    assert "top-right" in TOAST_CORNERS and "top-left" in TOAST_CORNERS
    assert "follow" in TOAST_CORNERS


def _screen():
    return qt_app().primaryScreen().availableGeometry()


def test_toast_defaults_to_top_right_corner():
    """不显示控制条时，提示条直接落屏幕右上角（不与任何窗口重叠）。"""
    app = qt_app()
    cfg = _cfg()
    bar = ControlBar(cfg)  # 不 show：isVisible()=False，不触发避让逻辑
    ag = _screen()
    toast = Toast(bar, cfg.get("toast_pos"))
    toast.show_message("测试提示", ms=200)
    assert toast.x() + toast.width() >= ag.right() - 60, "应贴右上角"
    assert toast.y() <= ag.top() + 60, "应在屏幕上方"
    toast.hide()


def test_toast_stays_in_corner_even_if_bar_centered():
    """控制条被拖到屏幕正中且可见时，提示条仍必须待在角落（不挡画面中间）。"""
    app = qt_app()
    cfg = _cfg()
    bar = ControlBar(cfg)
    bar.show()
    ag = _screen()
    bar.move(ag.center().x() - bar.width() // 2, ag.center().y() - bar.height() // 2)
    toast = Toast(bar, "top-right")
    toast.show_message("测试提示", ms=200)
    assert toast.x() > ag.center().x(), "提示条不能出现在画面中间"
    toast.hide()
    bar.close()


def test_toast_corner_switching():
    app = qt_app()
    cfg = _cfg()
    bar = ControlBar(cfg)  # 不 show
    ag = _screen()
    toast = Toast(bar, "top-right")
    toast.show_message("x", ms=200)

    toast.set_corner("top-left")
    assert toast.x() <= ag.left() + 60
    toast.set_corner("bottom-left")
    assert toast.y() >= ag.bottom() - toast.height() - 60
    toast.set_corner("bottom-right")
    assert toast.x() >= ag.right() - toast.width() - 60
    toast.set_corner("不存在的角落")
    assert toast._corner == "top-right", "非法值应回退默认"
    toast.hide()


def test_toast_follow_mode_sits_below_bar():
    """follow 模式（旧行为）：贴控制条下方。需要控制条可见才生效。"""
    app = qt_app()
    cfg = _cfg()
    bar = ControlBar(cfg)
    bar.show()
    bar.move(200, 200)
    toast = Toast(bar, "follow")
    toast.show_message("x", ms=200)
    assert abs(toast.y() - (bar.y() + bar.height() + 6)) < 60, \
        f"toast.y={toast.y()} bar.bottom={bar.y() + bar.height()}"
    toast.hide()
    bar.close()


# ---------- 控制条 ----------
def test_control_bar_scale_heights():
    qt_app()
    bar = ControlBar(_cfg())
    for scale, expect in ((1.0, 52), (1.25, 65), (1.5, 78)):
        bar.apply_scale(scale)
        assert bar.height() == expect, f"scale={scale} 高度={bar.height()}"


def test_control_bar_uses_configured_scale():
    cfg = _cfg()
    cfg.set("ui_scale", 1.5)
    qt_app()
    bar = ControlBar(cfg)
    assert bar.height() == 78


def test_build_qss_scales_font():
    assert "font-size: 15px" in build_qss(1.0)
    assert "font-size: 19px" in build_qss(1.25)
    assert "font-size: 22px" in build_qss(1.5)
    # 标题是 QLabel（曾误写成 QToolButton#title 导致样式不生效）
    assert "QLabel#title" in build_qss(1.0)


def test_control_bar_running_state():
    qt_app()
    bar = ControlBar(_cfg())
    bar.set_running(True)
    assert bar.btn_run.objectName() == "runOn"
    bar.set_running(False)
    assert bar.btn_run.objectName() == "runOff"


# ---------- 设置面板 ----------
def test_settings_dialog_loads_defaults():
    qt_app()
    cfg = _cfg()
    from gametrans.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(cfg, None)
    assert dlg.cmb_ocr_threads.currentData() == 1
    assert dlg.spin_ocr_interval.value() == 600
    assert dlg.spin_settle.value() == 300
    assert dlg.spin_min_len.value() == 2
    assert dlg.cmb_toast_pos.currentData() == "top-right"
    assert dlg.cmb_ocr_speed.currentData() == "fast"
    dlg.deleteLater()


def test_settings_dialog_roundtrip():
    qt_app()
    cfg = _cfg()
    from gametrans.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(cfg, None)
    dlg.spin_ocr_interval.setValue(900)
    dlg.spin_settle.setValue(150)
    dlg.spin_min_len.setValue(3)
    dlg.edit_ignore.setPlainText("Dify, 超频")
    dlg.edit_game_info.setText("中世纪奇幻 RPG")
    dlg.spin_ctx.setValue(5)
    idx = [dlg.cmb_toast_pos.itemData(i) for i in range(dlg.cmb_toast_pos.count())].index("top-left")
    dlg.cmb_toast_pos.setCurrentIndex(idx)
    dlg._save()

    assert cfg.get("ocr_interval_ms") == 900
    assert cfg.get("settle_ms") == 150
    assert cfg.get("min_line_len") == 3
    assert cfg.get("ignore_phrases") == "Dify, 超频"
    assert cfg.get("game_info") == "中世纪奇幻 RPG"
    assert cfg.get("context_pieces") == 5
    assert cfg.get("toast_pos") == "top-left"
    dlg.deleteLater()


def test_settings_ui_scale_roundtrip():
    qt_app()
    cfg = _cfg()
    from gametrans.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(cfg, None)
    idx = [dlg.cmb_ui_scale.itemData(i) for i in range(dlg.cmb_ui_scale.count())].index(1.5)
    dlg.cmb_ui_scale.setCurrentIndex(idx)
    dlg._save()
    assert cfg.get("ui_scale") == 1.5
    dlg.deleteLater()


def test_settings_reload_sees_saved_values():
    qt_app()
    cfg = _cfg()
    from gametrans.ui.settings_dialog import SettingsDialog
    dlg1 = SettingsDialog(cfg, None)
    dlg1.spin_ocr_interval.setValue(750)
    dlg1._save()
    dlg1.deleteLater()
    dlg2 = SettingsDialog(cfg, None)
    assert dlg2.spin_ocr_interval.value() == 750
    dlg2.deleteLater()
