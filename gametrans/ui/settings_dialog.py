"""设置面板：单窗口多分组，保存时写回 Config。"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..const import (
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_URL,
    ENGINE_AUTO,
    ENGINE_LOCAL,
    ENGINE_LOCAL_FIRST,
    ENGINE_API,
    ENGINE_NAMES,
    LANG_AUTO,
    LANG_EN,
    LANG_JA,
    MODE_OVERLAY,
    MODE_SIDE,
    MODE_NAMES,
    SPEED_PRESETS,
)
from ..utils.hotkey import parse_hotkey
from .common import color_to_hex, parse_color, ACCENT, TOAST_CORNERS, form_row

log = logging.getLogger(__name__)


class ColorButton(QPushButton):
    """显示颜色的按钮，点击弹取色器。"""

    def __init__(self, color_hex: str) -> None:
        super().__init__()
        self._color = parse_color(color_hex)
        self.setFixedSize(64, 26)
        self._refresh()
        self.clicked.connect(self._pick)

    def _refresh(self) -> None:
        self.setStyleSheet(
            f"QPushButton {{ background-color: {color_to_hex(self._color)};"
            f"border: 1px solid #555; border-radius: 4px; }}"
        )

    def _pick(self) -> None:
        c = QColorDialog.getColor(self._color, self, "选择颜色")
        if c.isValid():
            self._color = c
            self._refresh()

    def hex(self) -> str:
        return color_to_hex(self._color)


class SettingsDialog(QDialog):
    """设置主面板（模态）。读取当前 Config 填充，点保存一次性写回。"""

    settings_applied = Signal()

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = config
        self.setWindowTitle("GameTrans 设置")
        # 面板本身也放大：控件更疏朗，长文本（忽略短语/游戏背景）更好编辑
        self.setMinimumSize(660, 720)
        self.resize(720, 800)
        self._build()
        self._load()

    # ---------- UI ----------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._tab_capture(), "识别与捕获")
        tabs.addTab(self._tab_translate(), "翻译引擎")
        tabs.addTab(self._tab_display(), "显示")
        tabs.addTab(self._tab_other(), "词库与杂项")
        root.addWidget(tabs, 1)

        btns = QDialogButtonBox()
        b_save = btns.addButton("保存", QDialogButtonBox.AcceptRole)
        b_save.setObjectName("primary")
        btns.addButton("取消", QDialogButtonBox.RejectRole)
        btns.addButton("恢复默认", QDialogButtonBox.ResetRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        btns.clicked.connect(self._on_btn)
        root.addWidget(btns)

    def _scrolled(self, widget: QWidget) -> QWidget:
        """把分组内容包进滚动区，避免窗口小时被截断。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def _tab_capture(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)

        gb = QGroupBox("识别来源")
        f = QFormLayout(gb)
        self.cmb_lang = QComboBox()
        self.cmb_lang.addItem("自动判断（推荐）", LANG_AUTO)
        self.cmb_lang.addItem("英文", LANG_EN)
        self.cmb_lang.addItem("日文", LANG_JA)
        f.addRow("源语言", self.cmb_lang)

        self.cmb_monitor = QComboBox()
        f.addRow("使用的显示器", self.cmb_monitor)
        self.cmb_monitor.setEnabled(False)  # 预留：多显示器
        self._monitor_hint = QLabel("多显示器支持将在捕获模块实现")
        self._monitor_hint.setObjectName("dim")
        self.chk_foreground = QCheckBox(
            "仅当目标窗口在前台时翻译（切到浏览器/桌面自动暂停）")
        self.chk_foreground.setToolTip(
            "先用控制条『整屏/窗口』按钮选好目标窗口再开启。\n"
            "开启后只有当该窗口处于前台时才抓屏翻译，切走即暂停。")
        f.addRow("", self.chk_foreground)
        v.addWidget(gb)

        gb2 = QGroupBox("OCR 精度")
        f2 = QFormLayout(gb2)
        self.spin_min_conf = QDoubleSpinBox()
        self.spin_min_conf.setRange(0.0, 1.0)
        self.spin_min_conf.setSingleStep(0.05)
        f2.addRow("最低置信度", self.spin_min_conf)
        self.spin_diff = QDoubleSpinBox()
        self.spin_diff.setRange(0.0, 1.0)
        self.spin_diff.setSingleStep(0.01)
        self.spin_diff.setToolTip("字幕翻页的实测变化量约 0.04~0.06，默认 0.03 能稳定触发又过滤微抖")
        f2.addRow("画面变化阈值", self.spin_diff)
        self.cmb_ocr_speed = QComboBox()
        self.cmb_ocr_speed.addItem("最快（省 CPU，小字可能漏识别）", "fast")
        self.cmb_ocr_speed.addItem("均衡（推荐）", "balanced")
        self.cmb_ocr_speed.addItem("精细（小字更全，更耗 CPU）", "high")
        f2.addRow("识别速度", self.cmb_ocr_speed)
        self.cmb_ocr_threads = QComboBox()
        self.cmb_ocr_threads.addItem("1 线程（最省 CPU · 约 0.8 秒/次 · 占 1 核）", 1)
        self.cmb_ocr_threads.addItem("2 线程（省 CPU · 约 0.6 秒/次 · 占 3 核）", 2)
        self.cmb_ocr_threads.addItem("4 线程（快 · 约 0.2 秒/次 · 占 10 核）", 4)
        self.cmb_ocr_threads.addItem("8 线程（最快但很占 · 占 16 核）", 8)
        self.cmb_ocr_threads.setToolTip(
            "OCR 识别占用的 CPU 线程数。不限制时 ONNX 运行时会用满所有逻辑核心\n"
            "（实测 20 核机器单次识别占 ≈18 核），是 CPU 被吃满的主因。\n"
            "括号内为实测数据（识别速度=最快）。玩着游戏建议 1~2 线程。")
        f2.addRow("OCR 线程数", self.cmb_ocr_threads)
        self.spin_ocr_interval = QSpinBox()
        self.spin_ocr_interval.setRange(150, 3000)
        self.spin_ocr_interval.setSingleStep(50)
        self.spin_ocr_interval.setSuffix(" ms")
        self.spin_ocr_interval.setToolTip(
            "两次识别之间的最小间隔。若小于单次识别耗时，OCR 线程会连轴转、\n"
            "CPU 长期满载；600ms 兼顾响应速度与占用。")
        f2.addRow("识别间隔", self.spin_ocr_interval)
        lbl_ocr = QLabel("改动「线程数」或「识别速度」，保存后会重建 OCR 引擎（下次识别生效）。")
        lbl_ocr.setObjectName("dim")
        lbl_ocr.setWordWrap(True)
        f2.addRow("", lbl_ocr)
        v.addWidget(gb2)

        gb3 = QGroupBox("过滤与稳定")
        f3 = QFormLayout(gb3)
        self.spin_min_len = QSpinBox()
        self.spin_min_len.setRange(1, 20)
        self.spin_min_len.setSuffix(" 字符")
        self.spin_min_len.setToolTip("短于该长度的行不翻译，用于过滤单字符噪点")
        f3.addRow("最小行长", self.spin_min_len)
        self.spin_settle = QSpinBox()
        self.spin_settle.setRange(0, 2000)
        self.spin_settle.setSingleStep(50)
        self.spin_settle.setSuffix(" ms")
        self.spin_settle.setToolTip(
            "画面变化后等它稳定这么久再识别，避免读到滚动/打字机的半个句子；"
            "0=关闭（变化即识别）。最长等待 800ms，快速翻页也不会漏")
        f3.addRow("稳定等待", self.spin_settle)
        self.edit_ignore = QTextEdit()
        self.edit_ignore.setPlaceholderText(
            "一行一个，或用逗号分隔。命中的整行不翻译。\n"
            "例：Dify, 超频, 淘宝热卖, FPS")
        self.edit_ignore.setFixedHeight(76)
        f3.addRow("忽略短语", self.edit_ignore)
        lbl_ig = QLabel("用于屏蔽屏幕上不需要翻译的固定文字：水印、监控悬浮窗、UI 标签等。")
        lbl_ig.setObjectName("dim")
        lbl_ig.setWordWrap(True)
        f3.addRow("", lbl_ig)
        v.addWidget(gb3)

        v.addStretch(1)
        return self._scrolled(panel)

    def _tab_translate(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)

        gb = QGroupBox("引擎策略")
        f = QFormLayout(gb)
        self.cmb_engine = QComboBox()
        self.cmb_engine.addItem(ENGINE_NAMES[ENGINE_LOCAL_FIRST], ENGINE_LOCAL_FIRST)
        self.cmb_engine.addItem(ENGINE_NAMES[ENGINE_AUTO], ENGINE_AUTO)
        self.cmb_engine.addItem(ENGINE_NAMES[ENGINE_LOCAL], ENGINE_LOCAL)
        self.cmb_engine.addItem(ENGINE_NAMES[ENGINE_API], ENGINE_API)
        self.cmb_engine.setToolTip(
            "本地优先（默认）：用显卡跑本地模型，本地不可用时才转 API\n"
            "API 优先：先用 API，失败再转本地\n"
            "仅本地：本地不可用即停止（不会用 CPU）\n"
            "仅 API：只用 API")
        f.addRow("翻译引擎", self.cmb_engine)
        self.lbl_engine_hint = QLabel(
            "API：支持任意 OpenAI 兼容服务（DeepSeek/OpenAI/Kimi/通义/智谱/硅基流动/"
            "Ollama 等）。填好下方 Base URL、模型名与 API Key 即可；自动模式下 API 优先，"
            "失败再回落本地离线翻译。"
        )
        self.lbl_engine_hint.setObjectName("dim")
        self.lbl_engine_hint.setWordWrap(True)
        f.addRow("", self.lbl_engine_hint)
        v.addWidget(gb)

        gb_ds = QGroupBox("OpenAI 兼容 API（可填任意服务商）")
        f2 = QFormLayout(gb_ds)
        self.edit_key = QLineEdit()
        self.edit_key.setEchoMode(QLineEdit.Password)
        self.edit_key.setPlaceholderText("sk-… 留空则只用本地")
        f2.addRow("API Key", self.edit_key)
        self.btn_test = QPushButton("测试连接")
        self.btn_test.clicked.connect(self._on_test_api)
        f2.addRow("", self.btn_test)
        self.edit_base = QLineEdit()
        self.edit_base.setPlaceholderText("默认 https://api.deepseek.com/v1 ；Ollama 本地填 http://127.0.0.1:11434/v1")
        f2.addRow("Base URL", self.edit_base)
        self.edit_model = QLineEdit()
        self.edit_model.setPlaceholderText("如 deepseek-chat / gpt-4o-mini / kimi-k2 / qwen-plus / glm-4-flash")
        f2.addRow("模型名", self.edit_model)
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(3, 180)
        self.spin_timeout.setSuffix(" 秒")
        f2.addRow("超时", self.spin_timeout)
        self.lbl_test_result = QLabel("")
        self.lbl_test_result.setObjectName("dim")
        self.lbl_test_result.setWordWrap(True)
        f2.addRow("", self.lbl_test_result)
        v.addWidget(gb_ds)

        gb_local = QGroupBox("本地离线模型（NLLB-200，英/日→中）")
        f3 = QFormLayout(gb_local)
        self.edit_model_dir = QLineEdit()
        self.btn_browse_dir = QPushButton("浏览…")
        self.btn_browse_dir.clicked.connect(self._on_browse_model_dir)
        row = QHBoxLayout()
        row.addWidget(self.edit_model_dir, 1)
        row.addWidget(self.btn_browse_dir)
        wrap = QWidget()
        wrap.setLayout(row)
        f3.addRow("模型目录", wrap)
        self.cmb_nllb_variant = QComboBox()
        self.cmb_nllb_variant.addItem("1.3B（推荐：质量更好，约1.3GB，显存 +1.9GB）",
                                      "nllb-200-distilled-1.3B")
        self.cmb_nllb_variant.addItem("600M（更省显存：约2.3GB，显存 +1.1GB，输出偶有破碎）",
                                      "nllb-200-distilled-600M")
        self.cmb_nllb_variant.setToolTip(
            "切换后无需重启：下一批字幕会自动用新模型重新加载（首次约 5~15 秒）")
        f3.addRow("模型规格", self.cmb_nllb_variant)
        self.cmb_device = QComboBox()
        self.cmb_device.addItem("自动（使用显卡 CUDA）", "auto")
        self.cmb_device.addItem("强制使用显卡（CUDA/GPU）", "cuda")
        self.cmb_device.addItem("仅 CPU（会占满 CPU 核心，慎用）", "cpu")
        self.cmb_device.setToolTip(
            "默认使用显卡（已在你机器上实测 22ms/句，比 CPU 快约 5.5 倍）。\n"
            "显卡不可用时程序会停止翻译并提示，不会自动改用 CPU。\n"
            "确实想用 CPU 时请手动选择『仅 CPU』。")
        f3.addRow("推理设备", self.cmb_device)
        v.addWidget(gb_local)

        gb_ctx = QGroupBox("上下文增强（API 引擎）")
        f4 = QFormLayout(gb_ctx)
        self.edit_game_info = QLineEdit()
        self.edit_game_info.setPlaceholderText(
            "例：中世纪奇幻 RPG，主角叫 Arthur，场景在北方要塞")
        f4.addRow("内容背景", self.edit_game_info)
        lbl_gi = QLabel("告诉模型这是什么内容，译法/人名/语气会更贴合。")
        lbl_gi.setObjectName("dim")
        lbl_gi.setWordWrap(True)
        f4.addRow("", lbl_gi)
        self.spin_ctx = QSpinBox()
        self.spin_ctx.setRange(0, 8)
        self.spin_ctx.setSuffix(" 句")
        self.spin_ctx.setToolTip("带上最近几句已翻原文作参考，保持人名/术语前后一致；0=关闭")
        f4.addRow("参考上文", self.spin_ctx)
        v.addWidget(gb_ctx)

        v.addStretch(1)
        return self._scrolled(panel)

    def _tab_display(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)

        gb = QGroupBox("显示模式")
        f = QFormLayout(gb)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem(MODE_NAMES[MODE_OVERLAY], MODE_OVERLAY)
        self.cmb_mode.addItem(MODE_NAMES[MODE_SIDE], MODE_SIDE)
        self.cmb_mode.currentIndexChanged.connect(lambda _i: self._refresh_mode_fields())
        f.addRow("默认模式", self.cmb_mode)
        v.addWidget(gb)

        gb_ui = QGroupBox("界面缩放")
        fu = QFormLayout(gb_ui)
        self.cmb_ui_scale = QComboBox()
        self.cmb_ui_scale.addItem("100% 紧凑", 1.0)
        self.cmb_ui_scale.addItem("125% 推荐", 1.25)
        self.cmb_ui_scale.addItem("150% 大字号", 1.5)
        self.cmb_ui_scale.setToolTip("同时放大控制条（高度/字号/间距）与侧边小窗字号")
        fu.addRow("缩放倍率", self.cmb_ui_scale)
        self.cmb_toast_pos = QComboBox()
        for key, name in TOAST_CORNERS.items():
            self.cmb_toast_pos.addItem(name, key)
        self.cmb_toast_pos.setToolTip(
            "打游戏时提示条出现的位置。默认固定在右上角，不会挡住画面中心；\n"
            "「跟随控制条」是旧行为（控制条被拖到中间时提示也会到中间）。")
        fu.addRow("提示位置", self.cmb_toast_pos)
        lbl_ui = QLabel("保存后立即生效，无需重启。识别区域与译文字号仍按各自设置。")
        lbl_ui.setObjectName("dim")
        lbl_ui.setWordWrap(True)
        fu.addRow("", lbl_ui)
        v.addWidget(gb_ui)

        # —— 覆盖层样式 ——
        self.gb_overlay = QGroupBox("覆盖原文样式")
        fo = QFormLayout(self.gb_overlay)
        self.cmb_font = QComboBox()
        self.cmb_font.setEditable(True)
        self.cmb_font.addItems(self._font_choices())
        self.cmb_font.currentTextChanged.connect(lambda _t: self._update_font_preview())
        fo.addRow("字体", self.cmb_font)
        self.spin_font = QSpinBox()
        self.spin_font.setRange(10, 40)
        self.spin_font.setValue(24)
        self.spin_font.setToolTip("参考字号；译文会自动缩放，尽量刚好填满框选区域")
        self.spin_font.valueChanged.connect(lambda _v: self._update_font_preview())
        fo.addRow("字号", self.spin_font)
        self.lbl_font_preview = QLabel("龙在黎明时醒来。 Dragon at dawn")
        self.lbl_font_preview.setWordWrap(False)
        self.lbl_font_preview.setAlignment(Qt.AlignCenter)
        self.lbl_font_preview.setStyleSheet("padding: 6px; border: 1px dashed #3C414A;")
        fo.addRow("预览", self.lbl_font_preview)
        self.btn_text_color = ColorButton("#FFFFFF")
        fo.addRow("文字颜色", self.btn_text_color)
        self.btn_bg_color = ColorButton("#1A1A1A")
        fo.addRow("背景颜色", self.btn_bg_color)
        self.slider_alpha = QSlider(Qt.Horizontal)
        self.slider_alpha.setRange(0, 255)
        self.slider_alpha.setFixedWidth(180)
        self.lbl_alpha_val = QLabel("")
        row_alpha = QHBoxLayout()
        row_alpha.addWidget(self.slider_alpha)
        row_alpha.addWidget(self.lbl_alpha_val)
        wrap_a = QWidget()
        wrap_a.setLayout(row_alpha)
        fo.addRow("背景不透明", wrap_a)
        self.chk_hide_src = QCheckBox("译文直接盖住原文（沉浸式）")
        fo.addRow("", self.chk_hide_src)
        self.chk_follow = QCheckBox("跟随窗口移动（需通过『目标窗口』锚定后自动开启）")
        self.chk_follow.setEnabled(False)  # 由『目标窗口』按钮决定，此处仅展示状态
        fo.addRow("", self.chk_follow)
        v.addWidget(self.gb_overlay)

        # —— 小窗样式 ——
        self.gb_side = QGroupBox("独立小窗样式")
        fs = QFormLayout(self.gb_side)
        self.spin_side_w = QSpinBox()
        self.spin_side_w.setRange(240, 900)
        self.spin_side_w.setSuffix(" px")
        fs.addRow("窗口宽度", self.spin_side_w)
        self.spin_side_font = QSpinBox()
        self.spin_side_font.setRange(10, 40)
        fs.addRow("字号", self.spin_side_font)
        self.chk_side_topmost = QCheckBox("保持最前")
        fs.addRow("", self.chk_side_topmost)
        v.addWidget(self.gb_side)

        v.addStretch(1)
        return self._scrolled(panel)

    def _tab_other(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)

        gb0 = QGroupBox("数据目录")
        f0 = QFormLayout(gb0)
        self.lbl_data_dir = QLabel("")
        self.lbl_data_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_data_dir.setWordWrap(True)
        f0.addRow("当前位置", self.lbl_data_dir)
        self.btn_open_data = QPushButton("打开目录")
        self.btn_open_data.clicked.connect(self._on_open_data_dir)
        f0.addRow("", self.btn_open_data)
        lbl0 = QLabel("配置、模型、译文缓存、词库、日志都放在这里（默认在系统盘之外）。"
                      "想换位置：设置环境变量 GAMETRANS_HOME 后重启本程序。")
        lbl0.setObjectName("dim")
        lbl0.setWordWrap(True)
        f0.addRow("", lbl0)
        v.addWidget(gb0)

        gb = QGroupBox("热键")
        f = QFormLayout(gb)
        self.chk_hotkey_enable = QCheckBox("启用全局热键")
        f.addRow("", self.chk_hotkey_enable)
        self.edit_hotkey = QKeySequenceEdit()
        f.addRow("开/关翻译", self.edit_hotkey)
        lbl = QLabel("例如 Ctrl+Shift+T。热键录制需要同时包含修饰键（Ctrl/Alt/Shift/Win）。")
        lbl.setObjectName("dim")
        lbl.setWordWrap(True)
        f.addRow("", lbl)
        v.addWidget(gb)

        gb2 = QGroupBox("翻译缓存与词库")
        f2 = QFormLayout(gb2)
        self.chk_cache = QCheckBox("自动缓存译文（相同文本只翻译一次）")
        f2.addRow("", self.chk_cache)
        self.edit_glossary = QLineEdit()
        self.btn_browse_glossary = QPushButton("浏览…")
        self.btn_browse_glossary.clicked.connect(self._on_browse_glossary)
        row = QHBoxLayout()
        row.addWidget(self.edit_glossary, 1)
        row.addWidget(self.btn_browse_glossary)
        wrap = QWidget()
        wrap.setLayout(row)
        f2.addRow("人工词库文件", wrap)
        lbl2 = QLabel("格式：每行『原文|译文』或『原文=译文』，见词库文件头部注释。")
        lbl2.setObjectName("dim")
        lbl2.setWordWrap(True)
        f2.addRow("", lbl2)
        self.btn_open_glossary = QPushButton("打开当前词库")
        self.btn_open_glossary.clicked.connect(self._on_open_glossary)
        f2.addRow("", self.btn_open_glossary)
        v.addWidget(gb2)

        gb3 = QGroupBox("性能")
        f3 = QFormLayout(gb3)
        self.cmb_speed = QComboBox()
        self.cmb_speed.addItem("越快越好（默认）", "fast")
        self.cmb_speed.addItem("均衡", "balanced")
        self.cmb_speed.addItem("省资源", "saver")
        self.cmb_speed.currentIndexChanged.connect(lambda _i: self._refresh_speed_hint())
        f3.addRow("速度预设", self.cmb_speed)
        self.lbl_speed_hint = QLabel("")
        self.lbl_speed_hint.setObjectName("dim")
        self.lbl_speed_hint.setWordWrap(True)
        f3.addRow("", self.lbl_speed_hint)
        v.addWidget(gb3)

        v.addStretch(1)
        return self._scrolled(panel)

    def _refresh_speed_hint(self) -> None:
        preset = self.cmb_speed.currentData()
        names = {
            "fast": "33ms 采样 / 60ms 去抖 / 120ms 稳定（响应最快，占用略高）",
            "balanced": "66ms 采样 / 120ms 去抖 / 250ms 稳定",
            "saver": "150ms 采样 / 250ms 去抖 / 500ms 稳定（最省资源）",
        }
        self.lbl_speed_hint.setText(names.get(preset, ""))

    def _refresh_mode_fields(self) -> None:
        """根据当前显示模式启用/置灰对应分组，并切换封面提示。"""
        mode = self.cmb_mode.currentData()
        is_overlay = mode == MODE_OVERLAY
        self.gb_overlay.setEnabled(is_overlay)
        self.gb_side.setEnabled(not is_overlay)

    # ---------- 载入 / 保存 ----------
    def _load(self) -> None:
        cfg = self.cfg
        self.lbl_data_dir.setText(str(cfg.data_dir))

        def find_index(combo, value):
            for i in range(combo.count()):
                if combo.itemData(i) == value:
                    return i
            return 0

        # 识别与捕获
        self.cmb_lang.setCurrentIndex(find_index(self.cmb_lang, cfg.get("source_lang", default=LANG_AUTO)))
        self.spin_min_conf.setValue(float(cfg.get("ocr_min_conf", default=0.45)))
        self.spin_diff.setValue(float(cfg.get("frame_diff_threshold", default=0.03)))
        ocr_cfg = cfg.get("ocr", default={}) or {}
        self.cmb_ocr_speed.setCurrentIndex(
            find_index(self.cmb_ocr_speed, ocr_cfg.get("speed", "fast")))
        self.cmb_ocr_threads.setCurrentIndex(
            find_index(self.cmb_ocr_threads, int(ocr_cfg.get("threads", 1) or 1)))
        self.spin_ocr_interval.setValue(int(cfg.get("ocr_interval_ms", default=600)))
        self.spin_min_len.setValue(int(cfg.get("min_line_len", default=2)))
        self.spin_settle.setValue(int(cfg.get("settle_ms", default=300)))
        self.chk_foreground.setChecked(bool(cfg.get("only_foreground", default=True)))
        self.edit_ignore.setPlainText(str(cfg.get("ignore_phrases", default="") or ""))

        # 翻译引擎
        self.cmb_engine.setCurrentIndex(find_index(self.cmb_engine, cfg.get("engine", default=ENGINE_AUTO)))
        ds = cfg.get("deepseek", default={})
        self.edit_key.setText(ds.get("api_key", ""))
        self.edit_base.setText(ds.get("base_url", DEEPSEEK_DEFAULT_URL))
        self.edit_model.setText(ds.get("model", DEEPSEEK_DEFAULT_MODEL))
        self.spin_timeout.setValue(int(ds.get("timeout_s", 15)))
        self.edit_model_dir.setText(cfg.get("model_dir", default=""))
        self.cmb_nllb_variant.setCurrentIndex(
            find_index(self.cmb_nllb_variant, cfg.get("nllb", default={}).get("variant", "nllb-200-distilled-600M"))
        )
        self.cmb_device.setCurrentIndex(find_index(self.cmb_device, cfg.get("nllb", default={}).get("device", "auto")))
        self.edit_game_info.setText(str(cfg.get("game_info", default="") or ""))
        self.spin_ctx.setValue(int(cfg.get("context_pieces", default=3)))

        # 显示
        disp = cfg.get("display", default={})
        self.cmb_ui_scale.setCurrentIndex(
            find_index(self.cmb_ui_scale, float(cfg.get("ui_scale", default=1.25) or 1.0)))
        self.cmb_toast_pos.setCurrentIndex(
            find_index(self.cmb_toast_pos, str(cfg.get("toast_pos", default="top-right"))))
        self.cmb_mode.setCurrentIndex(find_index(self.cmb_mode, disp.get("mode", MODE_OVERLAY)))
        fam = str(disp.get("font_family", "") or "").strip()
        if fam:
            self.cmb_font.setCurrentText(fam)
        else:
            self.cmb_font.setCurrentIndex(0)
        self.spin_font.setValue(int(disp.get("font_size", 19)))
        self.btn_text_color._color = parse_color(disp.get("text_color", "#FFFFFF"))
        self.btn_text_color._refresh()
        self.btn_bg_color._color = parse_color(disp.get("bg_color", "#1A1A1A"))
        self.btn_bg_color._refresh()
        self.slider_alpha.setValue(int(disp.get("bg_alpha", 170)))
        self._update_alpha_label()
        self.chk_hide_src.setChecked(bool(disp.get("hide_source", True)))
        self.chk_follow.setChecked(bool(cfg.get("anchor", default={}).get("type", "screen") == "window"))
        self.spin_side_w.setValue(int(disp.get("side_width", 460)))
        self.spin_side_font.setValue(int(disp.get("side_font_size", 16)))
        self.chk_side_topmost.setChecked(True)
        self._update_font_preview()
        self._refresh_mode_fields()

        # 热键 / 缓存 / 词库 / 性能
        self.chk_hotkey_enable.setChecked(bool(cfg.get("hotkey_enabled", default=True)))
        self.edit_hotkey.setKeySequence(QKeySequence(cfg.get("hotkey", default="Ctrl+Shift+T")))
        self.chk_cache.setChecked(bool(cfg.get("auto_cache", default=True)))
        self.edit_glossary.setText(str(cfg.get("glossary_path", default="")))
        self.cmb_speed.setCurrentIndex(find_index(self.cmb_speed, cfg.get("speed_preset", default="fast")))
        self._refresh_speed_hint()

    @staticmethod
    def _font_choices() -> list:
        """“（自动）+ 常用中文字体 + 系统已装字体”，便于直接挑选。"""
        prefer = ["微软雅黑", "Microsoft YaHei UI", "Microsoft YaHei", "黑体",
                  "SimHei", "宋体", "SimSun", "楷体", "KaiTi", "等线",
                  "DengXian", "思源黑体", "Source Han Sans SC",
                  "Noto Sans CJK SC", "HarmonyOS Sans SC", "OPPO Sans",
                  "PingFang SC", "Arial", "Segoe UI"]
        ordered = ["（自动·微软雅黑）"]
        try:
            fams = [f for f in QFontDatabase.families() if not f.startswith("@")]
            for p in prefer:
                for f in fams:
                    if f.lower() == p.lower() and f not in ordered:
                        ordered.append(f)
                        break
            ordered += [f for f in fams if f not in ordered][:400]
        except Exception:  # noqa: BLE001
            ordered += [f for f in prefer if f not in ordered]
        return ordered

    def _update_alpha_label(self) -> None:
        self.lbl_alpha_val.setText(str(self.slider_alpha.value()))

    def _update_font_preview(self) -> None:
        fam = self.cmb_font.currentText().strip()
        if fam in ("", "（自动·微软雅黑）"):
            fam = "Microsoft YaHei UI"
        size = self.spin_font.value()
        color = color_to_hex(self.btn_text_color._color)
        self.lbl_font_preview.setStyleSheet(
            f"padding: 6px; border: 1px dashed #3C414A; color: {color};"
            f"font-family: '{fam}'; font-size: {size}px;"
        )

    def accept(self) -> None:
        # 热键合法性校验
        text = self.edit_hotkey.keySequence().toString(QKeySequence.PortableText)
        if self.chk_hotkey_enable.isChecked() and not parse_hotkey(text):
            QMessageBox.warning(self, "热键无效",
                                f"热键 {text} 无法注册，需要 Ctrl/Alt/Shift/Win 与一个按键，"
                                f"例如 Ctrl+Shift+T。")
            return
        self._save()
        super().accept()
        self.settings_applied.emit()

    def _save(self) -> None:
        cfg = self.cfg
        disp = cfg.get("display", default={})

        # 语言 / OCR
        cfg.set("source_lang", self.cmb_lang.currentData())
        cfg.set("ocr_min_conf", round(self.spin_min_conf.value(), 2))
        cfg.set("frame_diff_threshold", round(self.spin_diff.value(), 2))
        cfg.set("ocr", "speed", self.cmb_ocr_speed.currentData())
        cfg.set("ocr", "threads", int(self.cmb_ocr_threads.currentData() or 1))
        cfg.set("ocr_interval_ms", self.spin_ocr_interval.value())
        cfg.set("min_line_len", self.spin_min_len.value())
        cfg.set("settle_ms", self.spin_settle.value())
        cfg.set("only_foreground", self.chk_foreground.isChecked())
        cfg.set("ignore_phrases", self.edit_ignore.toPlainText().strip())

        # 引擎
        cfg.set("engine", self.cmb_engine.currentData())
        cfg.set("deepseek", "api_key", self.edit_key.text().strip())
        cfg.set("deepseek", "base_url", self.edit_base.text().strip() or DEEPSEEK_DEFAULT_URL)
        cfg.set("deepseek", "model", self.edit_model.text().strip() or DEEPSEEK_DEFAULT_MODEL)
        cfg.set("deepseek", "timeout_s", self.spin_timeout.value())
        cfg.set("model_dir", self.edit_model_dir.text().strip())
        cfg.set("nllb", "variant", self.cmb_nllb_variant.currentData())
        cfg.set("nllb", "device", self.cmb_device.currentData())
        cfg.set("game_info", self.edit_game_info.text().strip())
        cfg.set("context_pieces", self.spin_ctx.value())

        # 显示
        mode = self.cmb_mode.currentData()
        cfg.set("display", "mode", mode)
        fam = self.cmb_font.currentText().strip()
        cfg.set("display", "font_family",
                "" if fam in ("", "（自动·微软雅黑）") else fam)
        cfg.set("display", "font_size", self.spin_font.value())
        cfg.set("display", "text_color", self.btn_text_color.hex())
        cfg.set("display", "bg_color", self.btn_bg_color.hex())
        cfg.set("display", "bg_alpha", self.slider_alpha.value())
        cfg.set("display", "hide_source", self.chk_hide_src.isChecked())
        cfg.set("display", "side_width", self.spin_side_w.value())
        cfg.set("display", "side_font_size", self.spin_side_font.value())
        cfg.set("ui_scale", float(self.cmb_ui_scale.currentData() or 1.0))
        cfg.set("toast_pos", str(self.cmb_toast_pos.currentData() or "top-right"))

        # 热键
        cfg.set("hotkey", self.edit_hotkey.keySequence().toString(QKeySequence.PortableText))
        cfg.set("hotkey_enabled", self.chk_hotkey_enable.isChecked())

        # 缓存 / 词库 / 性能
        cfg.set("auto_cache", self.chk_cache.isChecked())
        cfg.set("glossary_path", self.edit_glossary.text().strip())
        cfg.set("speed_preset", self.cmb_speed.currentData())
        preset = SPEED_PRESETS[self.cmb_speed.currentData()]
        for k, v in preset.items():
            cfg.set(k, v)
        log.info("设置已保存")

    # ---------- 动作 ----------
    def _on_browse_model_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择本地模型目录", self.edit_model_dir.text())
        if d:
            self.edit_model_dir.setText(d)

    def _on_browse_glossary(self) -> None:
        start = str(self.cfg.glossary_file())
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择词库文件", start, "文本文件 (*.txt);;所有文件 (*)"
        )
        if path:
            self.edit_glossary.setText(path)

    def _on_open_glossary(self) -> None:
        path = Path(self.cfg.glossary_file())
        import os
        os.startfile(str(path))  # noqa: S606 Windows

    def _on_open_data_dir(self) -> None:
        import os
        os.startfile(str(self.cfg.data_dir))  # noqa: S606 Windows

    def _on_test_api(self) -> None:
        """连通性测试：先 GET /models，不支持则回退一次最小 chat 请求。"""
        import requests

        key = self.edit_key.text().strip()
        base = (self.edit_base.text().strip() or DEEPSEEK_DEFAULT_URL).rstrip("/")
        model = self.edit_model.text().strip() or DEEPSEEK_DEFAULT_MODEL
        if not key:
            self.lbl_test_result.setText("请先填写 API Key")
            return
        self.lbl_test_result.setText("测试中…")
        self.btn_test.setEnabled(False)
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        try:
            headers = {"Authorization": f"Bearer {key}"}
            # 1) 尽量走 /models
            try:
                r = requests.get(f"{base}/models", headers=headers, timeout=8)
                if r.ok:
                    self.lbl_test_result.setText("✓ 连接成功（服务可用）")
                    return
                if r.status_code not in (404, 405, 403):
                    self.lbl_test_result.setText(
                        f"× /models HTTP {r.status_code}: {r.text[:120]}")
                    return
            except requests.RequestException:
                pass  # 部分服务无 /models，走 2)
            # 2) 回退：最小 chat 请求验证 Key/模型
            body = {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            r2 = requests.post(f"{base}/chat/completions", json=body,
                               headers={**headers, "Content-Type": "application/json"},
                               timeout=15)
            if r2.ok:
                self.lbl_test_result.setText(f"✓ 连接成功（模型 {model} 可用）")
            else:
                self.lbl_test_result.setText(
                    f"× chat HTTP {r2.status_code}: {r2.text[:160]}")
        except Exception as exc:  # noqa: BLE001
            self.lbl_test_result.setText(f"× 连接失败：{exc}")
        finally:
            self.btn_test.setEnabled(True)

    def _on_btn(self, btn) -> None:
        if btn.text() == "恢复默认":
            ret = QMessageBox.question(
                self, "恢复默认", "确定把所有设置恢复到默认值？已保存配置将丢失。"
            )
            if ret == QMessageBox.Yes:
                self.cfg.reset()
                self._load()
