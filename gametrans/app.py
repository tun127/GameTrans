"""主控制器：组装配置、热键、控制条、设置面板与各功能模块。

里程碑规划：
- M1：本文件 + 控制条 + 设置面板（本阶段）
- M2~M7：capture/ocr/translate/display 模块通过 _pipeline 挂进来
"""
from __future__ import annotations

import difflib
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import List, Optional

import cv2
from PySide6.QtCore import QObject, QRect, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .config import Config
from .const import (
    DEFAULT_HOTKEY,
    ENGINE_API,
    ENGINE_AUTO,
    ENGINE_LOCAL_FIRST,
    MODE_OVERLAY,
)
from .core import regions
from .core.capture import CaptureEngine
from .core.ocr import OcrWorker
from .core.translate import TranslateWorker
from .ui.common import Toast, apply_global_style
from .ui.control_bar import ControlBar
from .ui.display import DisplayController
from .ui.region_selector import select_screen_region
from .ui.settings_dialog import SettingsDialog
from .ui.window_picker import pick_game_window
from .utils.hotkey import GlobalHotkey

log = logging.getLogger(__name__)

# 日志轮转参数：单文件 5MB，保留 3 份历史（总占用上限约 20MB）
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


class _SafeRotatingFileHandler(RotatingFileHandler):
    """写日志失败（文件被占用/只读/磁盘满）时静默降级。

    默认行为是每条日志都往 stderr 打一次完整 traceback（不影响程序，但很吵）。
    这里只在首次失败时提示一句，之后不再重复刷屏，程序照常运行。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._warned = False

    def handleError(self, record) -> None:  # noqa: N802
        if not self._warned:
            self._warned = True
            print("[GameTrans] 日志写入失败（文件被占用或无权限），已静默降级，"
                  "不影响使用。", file=sys.stderr)


class GameTransApp(QObject):
    """应用主控制器。"""

    # 识别到文本（列表[OCRResult]），从 OCR 线程发往 UI 线程
    ocr_lines = Signal(object)
    # 译文就绪：携带 (lines, 译文列表, 源语言)，从翻译线程发往 UI 线程
    translations_ready = Signal(object)
    # 引擎/运行告警（翻译线程 → UI，如 API 回退提示）
    engine_warning = Signal(str)
    # 引擎致命错误（显卡/模型不可用）：收到后直接停止翻译，不做任何降级
    engine_fatal = Signal(str)

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.cfg = config
        self.running = False  # 翻译管线是否在运行（M2+ 接入）
        # 每次启动都回到"全新选择"状态：框选区域与目标窗口都不保留。
        # （画面/窗口每次会变，旧选择极易错位，重新选一次最稳）
        self.cfg.set("region", None)
        self.cfg.set("anchor", {
            "type": "screen",
            "title_contains": "",
            "class_name": "",
            "monitor_index": 0,
        })

        # 初始化日志
        self._setup_logging()

        self.bar = ControlBar(config)
        # 提示条固定在屏幕角落，不跟着控制条挡住游戏画面中心
        self.toast = Toast(self.bar, str(config.get("toast_pos", default="top-right")))
        self.settings_dialog: Optional[SettingsDialog] = None
        # M5 显示层：overlay/side 由控制条切换，控制器常驻
        self._display = DisplayController(config)

        self.hotkey = GlobalHotkey()
        self._register_hotkey()

        # 逐里程碑填充的模块引用
        self._capture_engine: Optional[CaptureEngine] = None
        self._ocr_worker: Optional[OcrWorker] = None
        self._translator = None

        self._last_translation = None  # (lines, dst, src_lang)，M5 显示层读取
        self._last_src_key: Optional[str] = None  # 最近一次已投翻译的原文(归一化去重)

        # 译文稳定显示 + 低频“隐检”：译文盖上后保持静止（不闪），
        # 每 RECHECK_INTERVAL_MS 才隐藏 ~80ms 让 OCR 看一眼底层，
        # 感知字幕消失/换句。检查频率低，CPU 占用也随之下降。
        self.RECHECK_INTERVAL_MS = 4000
        self._recheck_timer = QTimer()
        self._recheck_timer.setSingleShot(True)
        self._recheck_timer.timeout.connect(self._on_recheck)
        self._peeking = False
        self._page_mode = False  # 整页/文档模式：自动降采样省 CPU
        self._empty_streak = 0  # 连续读空计数（截图遮罩防抖）
        self._inactive_notified = False  # "窗口不在前台"是否已提示过
        # 译文最短保持时间：刚渲染的译文不会被立刻替换/清空，消除“闪一下就没”
        self.MIN_HOLD_MS = 2600
        self._last_render_ts = 0.0
        self._last_render_src_key: Optional[str] = None

        # 翻译线程常驻（引擎懒加载，模型只加载一次），管线起停不影响它
        self._translate_worker = TranslateWorker(
            config,
            on_done=lambda lines, dst, src_lang: self.translations_ready.emit(
                (lines, dst, src_lang)
            ),
            on_status=self.engine_warning.emit,
            on_fatal=self.engine_fatal.emit,
        )
        self._translate_worker.start()

        # 全屏检测提示（运行中每 5s 巡检，独占全屏时译文层可能无法置顶）
        self._fs_timer = QTimer()
        self._fs_timer.setInterval(5000)
        self._fs_timer.timeout.connect(self._check_fullscreen_once)
        self._fs_warn_ts = 0.0

        self._bind_signals()
        self._refresh_bar_labels()
        log.info("GameTransApp 初始化完成")

    # ---------- 信号绑定 ----------
    def _bind_signals(self) -> None:
        self.bar.toggle_requested.connect(self.on_toggle_translate)
        self.bar.region_requested.connect(self.on_region_request)
        self.bar.pick_window_requested.connect(self.on_pick_window)
        self.bar.settings_requested.connect(self.open_settings)
        self.bar.quit_requested.connect(self.quit)
        self.bar.language_changed.connect(self._on_language_changed)
        self.bar.mode_changed.connect(self._on_mode_changed)
        self.hotkey.hotkey_pressed.connect(self.on_hotkey)
        # OCR 线程 → UI 线程；翻译线程 → UI 线程
        self.ocr_lines.connect(self.on_ocr_lines)
        self.translations_ready.connect(self._on_translation_done)
        self.engine_warning.connect(self._on_engine_warning)
        self.engine_fatal.connect(self._on_engine_fatal)

    # ---------- 主入口 ----------
    def show(self) -> None:
        self._position_control_bar()
        self.bar.show()

    def _position_control_bar(self) -> None:
        """放在主屏幕右上角（避开顶部任务栏区域）。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        bar_w = self.bar.sizeHint().width()
        self.bar.move(geo.right() - bar_w - 16, geo.top() + 12)

    def run(self) -> None:
        """应用事件循环入口。"""
        self.show()

    # ---------- 交互回调 ----------
    def on_toggle_translate(self, want_on: bool) -> None:
        if want_on:
            if not self._has_region():
                self.toast.show_message(
                    "请先框选字幕出现区域（点『框选区域』）", ms=3500, kind="warn"
                )
                self.bar.set_running(False)
                return
            block_reason = self._translation_block_reason()
            if block_reason:
                self.toast.show_message(block_reason, ms=4000, kind="warn")
                self.bar.set_running(False)
                return
            self.running = True
            self._start_pipeline()
        else:
            self.running = False
            self._stop_pipeline()
        self.bar.set_running(self.running)

    def _api_configured(self) -> bool:
        # API 连接配置统一存在 cfg["deepseek"] 节点（历史命名，内容通用）
        key = self.cfg.get("deepseek", default={}).get("api_key", "")
        return bool(key and str(key).strip())

    def _translation_block_reason(self) -> str:
        """启动翻译前的可用性检查：返回阻止原因字符串，无问题返回空串。"""
        engine = str(self.cfg.get("engine", default=ENGINE_LOCAL_FIRST))
        has_api = self._api_configured()
        if engine == ENGINE_API:
            if not has_api:
                return "当前为『仅 API』但未配置 API Key，请到设置填写（DeepSeek/OpenAI/Kimi 等均可）或改用本地模式"
            return ""
        if engine == ENGINE_AUTO and has_api:
            return ""  # API 优先，无需本地模型

        # 以下模式需要本地引擎（local_first / local / auto 无 Key）
        from .core.translate import LocalTranslator  # 延迟导入
        lt = LocalTranslator(self.cfg)
        model_ok = lt.model_dir() is not None
        device = str(self.cfg.get("nllb", default={}).get("device", "auto")).lower()
        gpu_ok = device == "cpu" or lt.cuda_available()
        if model_ok and gpu_ok:
            return ""
        # 本地不可用：本地优先模式可自动转 API，其余给明确原因
        if has_api and engine in (ENGINE_LOCAL_FIRST, ENGINE_AUTO):
            return ""
        if not model_ok:
            return "本地翻译模型未就绪：请先下载模型，或在设置里配置 API Key（设置→翻译引擎）"
        return ("未检测到可用显卡（CUDA），本地翻译无法启动。为避免占满 CPU，"
                "程序不会自动改用 CPU；可在设置里配置 API Key（自动转 API），"
                "或手动选择『仅 CPU』")

    def _has_region(self) -> bool:
        """区域有效性按绝对像素判定（字幕行可以很细，不能只用归一化比例）。"""
        region = self.cfg.get("region")
        if not region:
            return False
        base = regions.current_base_rect(self.cfg)
        rect = regions.region_to_abs(base, region) if base else None
        if rect is None:
            return False
        return rect[2] >= 24 and rect[3] >= 10

    def on_region_request(self) -> None:
        """弹出全屏框选。若翻译运行中先暂停管线，避免框选遮罩被 OCR 误识别。"""
        was_running = self.running
        if was_running:
            self._stop_pipeline()
        try:
            abs_rect = select_screen_region(self.bar)
            if abs_rect is None:
                self.toast.show_message("已取消框选", ms=1200, kind="info")
                return
            ok = regions.save_abs_selection(self.cfg, abs_rect)
            if not ok:
                self.toast.show_message(
                    "无法定位目标窗口，请先点『目标窗口』重新选择", ms=3500, kind="warn"
                )
                return
            self._refresh_bar_labels()
            self.toast.show_message("识别区域已设置", ms=1500, kind="ok")
        finally:
            if was_running:
                # 恢复管线（新引擎首帧强制识别一次，等价于 reset_diff）
                if self._has_region():
                    self._start_pipeline()
                else:
                    self.running = False
                    self.bar.set_running(False)

    def on_pick_window(self) -> None:
        """选择目标窗口；也可切回整屏模式。运行中先暂停管线。"""
        was_running = self.running
        if was_running:
            self._stop_pipeline()
        win = pick_game_window(self.bar)
        if win is None:
            # 取消选择：若之前正在翻译且旧区域仍有效，恢复运行
            if was_running and self._has_region():
                self._start_pipeline()
            return
        old_type = self.cfg.get("anchor", default={}).get("type", "screen")
        if win.get("mode") == "screen":
            self.cfg.set("anchor", "type", "screen")
            self.cfg.set("anchor", "title_contains", "")
        else:
            self.cfg.set("anchor", "type", "window")
            self.cfg.set("anchor", "title_contains", win["title"])
        new_type = self.cfg.get("anchor", default={}).get("type", "screen")
        if old_type != new_type:
            # 锚定方式变化会导致原区域坐标系失效，需重新框选
            self.cfg.set("region", None)
            self.toast.show_message("已切换锚定方式，请重新框选字幕区域", ms=3200, kind="warn")
        else:
            self.toast.show_message("目标窗口已更新", ms=1500, kind="ok")
        self._refresh_bar_labels()
        if was_running:
            # 区域仍有效则恢复；锚定切换导致区域失效则保持停止，等重新框选
            if self._has_region():
                self._start_pipeline()
            else:
                self.running = False
                self.bar.set_running(False)

    def _on_language_changed(self, _lang: str) -> None:
        """源语言切换：清空差异基准与去重签名，让新语言尽快重新识别生效。"""
        if self._capture_engine is not None:
            self._capture_engine.reset_diff()
        if self._ocr_worker is not None:
            self._ocr_worker.reset_signature()
        self._translate_worker.reset()

    def _on_mode_changed(self, _mode: str) -> None:
        """显示模式切换：重建译文层；运行中立即按新模式重新展示。"""
        self._recheck_timer.stop()
        self._display.stop()
        self._display.set_mode(_mode)
        if self.running:
            self._display.show()
            if self._last_translation is not None:
                lines, dst, _lang = self._last_translation
                self._display.set_payload(lines, dst)
                if len(lines) >= 3:
                    self._display.set_block("\n".join(z for z in dst if z))
                self._schedule_recheck()

    def _on_engine_warning(self, msg: str) -> None:
        """翻译引擎告警（API 回退等），非运行状态忽略。"""
        if not self.running:
            return
        self.toast.show_message(msg, ms=5000, kind="warn")

    def _on_engine_fatal(self, msg: str) -> None:
        """引擎致命错误（显卡/模型不可用）：直接停止翻译并提示。

        绝不降级到 CPU —— CPU 跑本地模型会把所有核心打满，是明确禁止的行为。
        """
        log.error("[FATAL] %s", msg)
        if self.running:
            self.running = False
            self._stop_pipeline()
            self.bar.set_running(False)
        self.toast.show_message(msg, ms=9000, kind="err")

    def _check_fullscreen_once(self) -> None:
        """巡检：前台为铺满显示器的窗口时提示独占全屏风险（节流 40s）。"""
        if not self.running or not self.cfg.get("check_fullscreen", default=True):
            return
        from .utils import win32  # 延迟导入
        fg = win32.foreground_window()
        if not fg:
            return
        monitors = win32.get_monitor_rects()
        if win32.likely_fullscreen_window(fg, monitors):
            now = time.monotonic()
            if now - self._fs_warn_ts > 40.0:
                self._fs_warn_ts = now
                self.toast.show_message(
                    "检测到前台为全屏窗口：若译文层不可见且游戏为『独占全屏』，"
                    "请将游戏切换为无边框窗口/窗口化", ms=6000, kind="warn",
                )

    def _refresh_bar_labels(self) -> None:
        anchor = self.cfg.get("anchor", default={})
        if anchor.get("type") == "window":
            title = anchor.get("title_contains", "")
            short = title[:10] + "…" if len(title) > 10 else title
            self.bar.set_pick_label(short or "窗口", f"当前跟随窗口：{title}")
        else:
            self.bar.set_pick_label(
                "整屏", "当前为整屏锚定；点此选择要跟随的游戏窗口")
        self.bar.set_region_label(regions.snapshot_region_desc(self.cfg))

    def on_hotkey(self) -> None:
        """全局热键：切换翻译开关。"""
        if not self.cfg.get("hotkey_enabled", default=True):
            return
        self.on_toggle_translate(not self.running)

    def open_settings(self) -> None:
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self.cfg, self.bar)
            self.settings_dialog.settings_applied.connect(self._on_settings_applied)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _on_settings_applied(self) -> None:
        """设置保存后：重新注册热键、刷新标签、应用界面缩放、重载翻译引擎。"""
        self._register_hotkey()
        self._refresh_bar_labels()
        self.bar.apply_scale(float(self.cfg.get("ui_scale", default=1.25) or 1.0))
        self._position_control_bar()  # 高度变了，重贴右上角
        # 提示条位置可改（角落/跟随控制条），保存后立即生效
        self.toast.set_corner(str(self.cfg.get("toast_pos", default="top-right")))
        # OCR 档位/线程数变了才重建引擎（限线程能显著降低 CPU 占用）
        try:
            from .core.ocr import reload_engine_if_needed
            if reload_engine_if_needed():
                self._ocr_worker.reset_signature()  # 引擎换了，允许同一画面重新识别
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR 引擎重载检查失败: %s", exc)
        # 模型规格/设备/词库等可能变了：让翻译线程下一批按新配置重建引擎
        self._translate_worker.reset_engine()

    def _register_hotkey(self) -> None:
        if not self.cfg.get("hotkey_enabled", default=True):
            self.hotkey.unregister()
            return
        hotkey_str = self.cfg.get("hotkey", default=DEFAULT_HOTKEY)
        if not self.hotkey.set_hotkey(hotkey_str):
            log.warning("热键注册失败: %s", hotkey_str)

    # ---------- 管线（逐里程碑扩展） ----------
    def _start_pipeline(self) -> None:
        if self._capture_engine and self._capture_engine.is_running():
            return
        self._ocr_worker = OcrWorker(
            on_lines=self.ocr_lines.emit,
            get_lang=lambda: self.cfg.get("source_lang", default="auto"),
            get_min_conf=lambda: float(self.cfg.get("ocr_min_conf", default=0.45)),
            # 整页模式 OCR 间隔放宽到 4s，大幅降低 CPU
            get_ocr_interval=lambda: 4.0 if self._page_mode else float(
                self.cfg.get("ocr_interval_ms", default=300)) / 1000.0,
        )
        self._ocr_worker.start()

        engine = CaptureEngine(
            self.cfg,
            on_frame=self._on_capture_frame,
            on_status=self._on_capture_status,
            # 整页模式抓屏降采样 33ms -> 400ms
            get_interval_ms=lambda: 400 if self._page_mode else int(
                self.cfg.runtime_params()["interval"]),
        )
        self._capture_engine = engine
        engine.start()
        self._display.show()  # 管线启动时按当前模式显示译文层
        self._fs_timer.start()
        log.info("捕获+OCR 引擎已启动")

    def _stop_pipeline(self) -> None:
        self._recheck_timer.stop()
        self._fs_timer.stop()
        if self._capture_engine:
            self._capture_engine.stop()
            self._capture_engine = None
        if self._ocr_worker:
            self._ocr_worker.stop()
            self._ocr_worker = None
        self._display.hide()  # 停止翻译时隐藏译文层
        log.info("捕获+OCR 引擎已停止")

    def _on_capture_frame(self, bgr, rect) -> None:
        """捕获线程→OCR 队列（非阻塞，绝不拖慢截图循环）。"""
        if self._ocr_worker is None:
            return
        # 防污染保险：控制条是置顶 UI，若与识别区域重叠，把该区域涂黑，
        # 保证“只翻译框选范围内的真实画面内容”。
        try:
            l, t, w, h = rect
            inter = self.bar.frameGeometry().intersected(QRect(l, t, w, h))
            if inter.width() > 0 and inter.height() > 0:
                x0 = max(0, inter.left() - l)
                y0 = max(0, inter.top() - t)
                x1 = min(w, x0 + inter.width())
                y1 = min(h, y0 + inter.height())
                if x1 > x0 and y1 > y0:
                    bgr[y0:y1, x0:x1] = 0
        except Exception:  # noqa: BLE001
            log.exception("控制条遮挡剔除失败")
        if self._page_mode:
            # 整页大图先降采样再 OCR：像素量大幅下降，CPU 显著降低；
            # box_norm 是归一化坐标，缩放不影响显示对位
            speed = str(self.cfg.get("ocr", default={}).get("speed", "balanced")).lower()
            max_side = {"fast": 1000, "balanced": 1400, "high": 2000}.get(speed, 1400)
            hh, ww = bgr.shape[:2]
            longest = max(hh, ww)
            if longest > max_side:
                scale = max_side / longest
                bgr = cv2.resize(bgr, (int(ww * scale), int(hh * scale)),
                                 interpolation=cv2.INTER_AREA)
        self._ocr_worker.submit(bgr)

    def _on_capture_status(self, status: str) -> None:
        if status == "no_window":
            self.toast.show_message("找不到目标游戏窗口，请重新选择", ms=3000, kind="warn")
        elif status == "no_region":
            self.toast.show_message("尚未设置识别区域，点『框选区域』", ms=3000, kind="warn")
        elif status.startswith("error"):
            self.toast.show_message(f"捕获异常：{status}", ms=4000, kind="err")

        if status == "inactive":
            # 目标窗口不在前台：隐藏译文层，提示一次（回到游戏自动恢复）
            if self._display.is_visible():
                self._display.hide()
            if self.running and not self._inactive_notified:
                self._inactive_notified = True
                self.toast.show_message(
                    "目标窗口不在前台，翻译已暂停（切回游戏自动继续）",
                    ms=3000, kind="info")
            return
        if status in ("running", "capturing"):
            # 回到前台：恢复译文层；首次恢复允许再提示一次
            self._inactive_notified = False
            if self.running and not self._display.is_visible():
                self._display.show()

    @staticmethod
    def _looks_like_our_translation(text: str, lang: str) -> bool:
        """识别行是否像“我们刚盖上去的中文译文”（而不是真实原文），用于回声过滤。

        - 英文源：出现汉字/全角标点即视为异常（我们盖的中文或屏幕中文 UI）。
        - 日文源：日文字幕本身就含汉字，只在“无任何假名”时视为我们盖的中文译文
          （纯汉字且无假名 = 简体中文译文特征）。
        """
        has_han = any("\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"
                      for ch in text)
        if not has_han:
            return False
        if lang == "ja":
            has_kana = any("\u3040" <= ch <= "\u30ff" for ch in text)
            return not has_kana
        return True

    @staticmethod
    def _is_noise_line(text: str) -> bool:
        """非自然语言行：纯数字/时间码/代号/指标（如 00:00、15:00、6K、0.024）。

        这些多半来自屏幕上的硬件监控/AI 工具悬浮窗，翻译它们只会产生垃圾。
        只对拉丁文本行生效；含 CJK/假名的行由其它规则处理，避免误伤日文。
        """
        t = (text or "").strip()
        if not t:
            return True
        if any("\u3040" <= ch <= "\u30ff" for ch in t):
            return False  # 含假名：可能是日文原文
        if any("\u4e00" <= ch <= "\u9fff" for ch in t):
            return False  # 含汉字：由回声过滤规则处理
        letters = sum(1 for ch in t if ch.isascii() and ch.isalpha())
        if letters == 0:
            return True  # 纯数字/符号
        if letters / len(t) < 0.35:
            return True  # 字母占比过低（时间码/数值指标）
        if not re.search(r"[A-Za-z]{3,}", t):
            return True  # 没有任何像样的单词（6K、3.6 之类）
        return False

    @staticmethod
    def _ignore_phrases(cfg) -> List[str]:
        """解析『忽略短语』配置：按换行/逗号/分号/顿号分隔，统一小写。"""
        raw = str(cfg.get("ignore_phrases", default="") or "")
        parts = re.split(r"[\n,，;；、]+", raw)
        out = []
        for p in parts:
            p = p.strip().lower()
            if p and p not in out:
                out.append(p)
        return out

    @staticmethod
    def _is_ignored(text: str, phrases: List[str]) -> bool:
        """命中任一忽略短语即丢弃该行（不分大小写）。"""
        if not phrases:
            return False
        low = (text or "").lower()
        return any(p in low for p in phrases)

    def on_ocr_lines(self, lines) -> None:
        """UI 线程收到识别文本。

        1) 丢弃含中文的行：目标语言固定中文，识别到中文多半是我们盖上的译文
          或屏幕中文 UI——投翻译只会“翻译自己的译文”造成回声循环。
        2) 丢弃纯数字/时间码/代号行（屏幕 HUD 悬浮窗文字）。
        3) 与最近一次已处理字幕比较：同句(字幕未变)不重复投递；
           区域不再有外文 → 认为字幕结束 → 清空译文块，等待下一句。
        """
        texts = " | ".join(ln.text for ln in lines)
        log.info("[OCR] %s", texts[:180])
        if not self.running:
            return
        min_len = int(self.cfg.get("min_line_len", default=2))
        lang = str(self.cfg.get("source_lang", default="en"))
        ignore = self._ignore_phrases(self.cfg)
        keep = [
            ln for ln in lines
            if len((ln.text or "").strip()) >= min_len
            and not self._looks_like_our_translation(ln.text, lang)
            and not self._is_noise_line(ln.text)
            and not self._is_ignored(ln.text, ignore)
        ]
        now_ms = time.monotonic() * 1000.0
        if not keep:
            # 区域里没有可翻译的外文了。三重保护才判定“字幕结束”：
            # 1) 连续两次读空（防截图遮罩瞬时干扰）
            # 2) 当前译文已显示满最短保持时间（防刚显示就被清空）
            held = (now_ms - self._last_render_ts) >= self.MIN_HOLD_MS
            self._empty_streak += 1
            if (self._empty_streak >= 2 and self._last_src_key is not None
                    and held):
                self._last_src_key = None
                self._last_render_src_key = None
                self._recheck_timer.stop()
                self._display.clear_payload()
                if self._capture_engine is not None:
                    self._capture_engine.reset_diff()
            return
        self._empty_streak = 0
        # 归一化 key：忽略空格/大小写/标点差异，OCR 噪声不会导致同句重翻
        key = "|".join(self._norm_key(ln.text) for ln in keep)
        if key == self._last_src_key:
            # 同一句字幕仍在：不重翻，但继续安排下一次隐检（感知消失/换句）
            self._schedule_recheck()
            return
        # 与当前显示的译文原文高度相似（只是 OCR 噪声/个别词变化）→ 不替换，
        # 避免译文被反复覆盖造成的闪烁
        if (self._last_render_src_key
                and self._similarity(key, self._last_render_src_key) >= 0.82):
            self._schedule_recheck()
            return
        self._last_src_key = key
        # 整页/文档模式（多行）：抓屏/OCR 节拍放宽，降低 CPU 占用。
        # 注意 keep 是 OCR 线程合并后的“行”，不是零散词块，否则一行字幕
        # 会被误判成整页并导致降频漏句。
        self._page_mode = len(keep) >= 3
        # 实时性关键：按行批量提交（一次请求仍包含全部行，保留上下文），
        # 这样“行级缓存”能命中未变化的行，只翻译新出现的行 —— 比整段合并快得多。
        max_lines = int(self.cfg.get("display", default={}).get("max_lines", 8))
        batch = keep[:max(1, max_lines)]
        self._translate_worker.submit(batch,
                                      self.cfg.get("source_lang", default="en"))

    @staticmethod
    def _norm_key(text: str) -> str:
        """字幕去重 key：去空白/标点、转小写，抹平 OCR 噪声。"""
        import re
        return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE).lower()

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """两段归一化文本的相似度 0~1（用于判断内容是否只是小幅变化）。"""
        if not a or not b:
            return 0.0
        return difflib.SequenceMatcher(None, a, b).ratio()

    # ---------- overlay 字幕生命周期（低频隐检，仅捕获排除失败时回退使用） ----------
    def _schedule_recheck(self) -> None:
        """译文渲染后安排一次低频隐检。

        若译文层已从屏幕捕获中排除（WDA_EXCLUDEFROMCAPTURE 生效），
        OCR 天然读到纯净底层，无需任何隐检/闪烁。
        """
        if not self.running or self._display.mode() != MODE_OVERLAY:
            return
        if self._display.capture_excluded():
            return  # 捕获已排除：译文层对 OCR 不可见，无需隐检
        self._recheck_timer.start(self.RECHECK_INTERVAL_MS)

    def _on_recheck(self) -> None:
        """回退方案：短暂把译文层变透明（框位置保持），让 OCR 看底层。"""
        if not self.running or self._peeking:
            return
        if self._display.mode() != MODE_OVERLAY:
            return
        if self._display.capture_excluded():
            return
        self._peeking = True
        self._display.set_transparent(True)  # 框还在，只是变透明
        if self._capture_engine is not None:
            self._capture_engine.reset_diff()
        if self._ocr_worker is not None:
            self._ocr_worker.reset_signature()  # 强制同文本也回传一次
        # 透明窗口需覆盖至少一个抓屏周期，保证 OCR 拿到干净的底层帧
        hide_ms = 400 if self._page_mode else 120
        QTimer.singleShot(hide_ms, self._finish_peek)

    def _finish_peek(self) -> None:
        self._peeking = False
        if self.running:
            self._display.set_transparent(False)

    def _on_translation_done(self, payload) -> None:
        """UI 线程收到译文。M4 打日志并暂存；M5 交给显示层绘制。"""
        lines, dst, src_lang = payload
        pairs = "  ⏎  ".join(f"{ln.text} ⇒ {zh}" for ln, zh in zip(lines, dst) if zh)
        log.info("[TRANS:%s] %s", src_lang, pairs[:400])
        self._last_translation = (lines, dst, src_lang)
        if not self.running:
            return
        if lines and lines[0].box_norm == [0.0, 0.0, 1.0, 1.0]:
            # 整段翻译结果：一个完整大块覆盖识别区域
            self._display.set_block(dst[0] if dst else "")
        elif len(lines) >= 3:
            self._display.set_block("\n".join(z for z in dst if z))
        else:
            self._display.set_payload(lines, dst)
        # 记录渲染时间与对应原文：用于“最短保持时间”和相似度保护（防闪）
        self._last_render_ts = time.monotonic() * 1000.0
        self._last_render_src_key = self._last_src_key
        self._schedule_recheck()  # 捕获排除未生效时，安排下一次低频透明隐检

    # ---------- 退出 ----------
    def quit(self) -> None:
        self.running = False
        try:
            self.hotkey.unregister()
        except Exception:  # noqa: BLE001
            pass
        self._stop_pipeline()
        self._translate_worker.stop()
        self._display.stop()
        QApplication.quit()

    # ---------- 日志 ----------
    def _setup_logging(self) -> None:
        if getattr(self, "_logging_done", False):
            return
        self._logging_done = True
        try:
            logs_dir = self.cfg.logs_dir()
            # 按大小轮转：单个 5MB，保留 3 份历史（最多约 20MB），
            # 长期挂机不会把日志涨到几百 MB，也不会丢最近的历史记录。
            # delay=True：第一条日志才真正打开文件，启动时不占用文件句柄。
            fh = _SafeRotatingFileHandler(
                logs_dir / "gametrans.log",
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
            fh.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            root = logging.getLogger()
            root.setLevel(logging.INFO)
            # 避免重复添加（多次初始化时日志会翻倍写入）
            if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
                root.addHandler(fh)
        except Exception as exc:  # noqa: BLE001
            # 日志文件不可写（被占用/磁盘满）时只提示，不影响程序运行
            print(f"[GameTrans] 无法写入日志文件: {exc}", file=sys.stderr)
        logging.getLogger("httpx").setLevel(logging.WARNING)
