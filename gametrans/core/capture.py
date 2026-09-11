"""捕获引擎：独立线程按设定频率抓取识别区域并做帧差异去重。

对外行为：
- start() 启动线程；stop() 停止并 join。
- 每抓到一帧且与上一帧差异超过阈值时，发出 frame_signal(bgr_img, rect)。
- 坐标跟随：锚定窗口时每帧重算窗口客户区位置，实现拖动/缩放跟随。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

from . import regions
from ..config import Config

log = logging.getLogger(__name__)


class CaptureEngine:
    """高频截图 + 去重。不依赖 Qt，便于独立线程运行。"""

    def __init__(self, config: Config,
                 on_frame: Optional[Callable[[np.ndarray, Tuple[int, int, int, int]], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None,
                 get_interval_ms: Optional[Callable[[], int]] = None) -> None:
        self.cfg = config
        self._on_frame = on_frame
        self._on_status = on_status
        # 抓屏间隔可动态调节（整页/文档模式自动降采样以省 CPU）
        self._get_interval_ms = get_interval_ms or (
            lambda: int(self.cfg.runtime_params()["interval"]))
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._prev_small: Optional[np.ndarray] = None
        # 稳定判定：变化帧先暂存，等画面静止 settle_ms 后再送 OCR（0=关闭）
        self._pending: Optional[tuple] = None
        self._pending_ts = 0.0
        self._stable_since: Optional[float] = None
        self._was_active = True  # 目标窗口是否在前台（上次判定结果）
        self._hwnd_cache: Optional[int] = None
        self._last_found_ts = 0.0
        self._status = "stopped"

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            # 确保坐标基准一致：Win32 与 mss 都按物理像素
            from ..utils.dpi import enable_per_monitor_dpi_awareness
            enable_per_monitor_dpi_awareness()
            self._stop_evt.clear()
            self._was_active = True
            self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
            self._thread.start()
            self._set_status("running")
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=2.0)
        self._thread = None
        self._set_status("stopped")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------- 主循环 ----------
    def _run(self) -> None:
        import mss
        mss_factory = getattr(mss, "MSS", None) or mss.mss
        self._set_status("running")

        def _interval() -> float:
            return max(0.02, int(self._get_interval_ms()) / 1000.0)

        try:
            with mss_factory() as sct:
                frame_i = 0
                resolve_every = 60
                while not self._stop_evt.is_set():
                    t0 = time.monotonic()
                    interval = _interval()  # 每帧动态读取，模式切换即时生效
                    resolve_every = max(1, int(2.0 / interval))
                    base = self._current_base(frame_i, resolve_every)
                    if base is None:
                        # 目标窗口不可用：等一会儿再试，避免空转
                        self._set_status("no_window")
                        time.sleep(0.5)
                        frame_i += 1
                        continue

                    # 仅翻译选定窗口：目标窗口不在前台时暂停（切到浏览器/桌面不翻）
                    if (bool(self.cfg.get("only_foreground", default=True))
                            and self.cfg.get("anchor", default={}).get("type") == "window"
                            and not regions.target_is_foreground(self.cfg, self._hwnd_cache)):
                        if self._was_active:
                            self._was_active = False
                            log.info("目标窗口离开前台：暂停翻译")
                        self._set_status("inactive")
                        time.sleep(0.3)
                        frame_i += 1
                        continue
                    if not self._was_active:
                        self._was_active = True
                        self.reset_diff()  # 回到前台后重新识别当前画面
                        log.info("目标窗口回到前台：恢复翻译")

                    abs_rect = regions.region_to_abs(base, self.cfg.get("region"))
                    if abs_rect is None:
                        self._set_status("no_region")
                        time.sleep(0.5)
                        frame_i += 1
                        continue

                    img = self._grab(sct, abs_rect)
                    if img is not None:
                        self._feed(img, abs_rect)

                    # 精确节奏
                    elapsed = time.monotonic() - t0
                    wait = interval - elapsed
                    if wait > 0 and not self._stop_evt.wait(wait):
                        pass
                    frame_i += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("捕获线程异常退出: %s", exc)
            self._set_status(f"error:{exc}")
        finally:
            self._set_status("stopped")

    def _current_base(self, frame_i: int, resolve_every: int) -> Optional[Tuple[int, int, int, int]]:
        anchor = self.cfg.get("anchor", default={})
        if anchor.get("type") != "window":
            return regions.monitor_rect(self.cfg)
        # 窗口锚定：句柄缓存 + 周期重解析
        now = time.monotonic()
        if self._hwnd_cache is None or frame_i % resolve_every == 0 or now - self._last_found_ts > 3.0:
            hwnd = regions.resolve_hwnd(self.cfg)
            self._hwnd_cache = hwnd
            self._last_found_ts = now
        if self._hwnd_cache is None:
            return None
        return regions.client_rect(self._hwnd_cache)

    def _grab(self, sct, rect) -> Optional[np.ndarray]:
        """抓取区域并转 BGR ndarray。部分屏幕被遮挡/异常时返回 None。"""
        l, t, w, h = rect
        try:
            shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
            if shot.width < 4 or shot.height < 4:
                return None
            arr = np.asarray(shot, dtype=np.uint8)          # BGRA
            return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        except Exception as exc:  # noqa: BLE001
            log.debug("抓屏失败 %s: %s", rect, exc)
            return None

    # ---------- 帧差异 ----------
    def _changed(self, bgr: np.ndarray) -> bool:
        """与“上次触发时的画面”比较，超过阈值视为变化并刷新基准。

        - 采用基准帧而非“相邻帧”：字幕翻页是瞬时单帧变化，基准法即使错过
          变化瞬间，也会在之后静止画面与旧基准差异显著时继续触发，不漏检。
        - 基准只在“触发后”更新：静止字幕不会反复触发；同一字幕停留期不重复 OCR。
        - 基准缺失（启动/区域重置后）首帧即视为变化：启动时画面已存在的字幕
          也能被识别一次，而不是要等画面先变一次。
        """
        threshold = float(self.cfg.get("frame_diff_threshold", default=0.03))
        h, w = bgr.shape[:2]
        small = cv2.resize(bgr, (max(1, min(160, w)), max(1, min(160, h))),
                           interpolation=cv2.INTER_AREA)
        small_gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)
        if self._prev_small is None:
            self._prev_small = small_gray.copy()
            return True  # 无基准：先识别一次
        if self._prev_small.shape == small_gray.shape:
            diff = float(np.mean(np.abs(small_gray - self._prev_small))) / 255.0
            if diff >= threshold:
                self._prev_small = small_gray.copy()  # 基准更新到“本次触发”的画面
                return True
        return False

    def reset_diff(self) -> None:
        """清除基准：下一帧强制识别一次（区域调整/语言切换后调用）。"""
        self._prev_small = None
        self._pending = None
        self._stable_since = None

    # ---------- 稳定判定 ----------
    def _feed(self, bgr: np.ndarray, abs_rect: Tuple[int, int, int, int]) -> None:
        """收到一帧：变化后等画面稳定再送 OCR。

        字幕常伴随淡入/滚动/打字机效果，变化瞬间送 OCR 会读到半句。
        这里先暂存变化帧，等连续 settle_ms 不再变化后再提交；
        若画面一直变化（如持续动画），超过最长等待也强制提交，保证不漏。
        """
        settle_ms = int(self.cfg.get("settle_ms", default=300) or 0)
        if not self._changed(bgr):
            # 与基准一致：视为稳定，累积稳定时长
            if self._pending is None:
                return
            now = time.monotonic()
            if self._stable_since is None:
                self._stable_since = now
            max_wait = max(0.8, settle_ms * 3 / 1000.0)
            if (now - self._stable_since) * 1000.0 >= settle_ms or (
                    now - self._pending_ts) >= max_wait:
                pending, rect = self._pending
                self._pending = None
                self._stable_since = None
                self._emit(pending, rect)
            return

        self._set_status("capturing")
        if settle_ms <= 0 or self._on_frame is None:
            self._pending = None
            self._stable_since = None
            self._emit(bgr, abs_rect)
            return
        now = time.monotonic()
        max_wait = max(0.8, settle_ms * 3 / 1000.0)
        if self._pending is not None and (now - self._pending_ts) >= max_wait:
            # 画面持续变化（动画/滚动）：超过最长等待强制出帧，避免永远等不到稳定
            pending, rect = self._pending
            self._emit(pending, rect)
            self._pending = (bgr, abs_rect)
            self._pending_ts = now  # 新一轮变化重新计时
            self._stable_since = now
            return
        # 暂存最新变化帧（同一轮变化只保留最后一帧，避免送中间态）。
        # 注意：计时起点是本轮变化的第一次，持续变化时不重置，否则永远等不到超时。
        if self._pending is None:
            self._pending_ts = now
            self._stable_since = now
        self._pending = (bgr, abs_rect)

    def _emit(self, bgr: np.ndarray, abs_rect: Tuple[int, int, int, int]) -> None:
        try:
            if self._on_frame:
                self._on_frame(bgr, abs_rect)
        except Exception as exc:  # noqa: BLE001
            log.exception("帧回调异常: %s", exc)

    # ---------- 状态 ----------
    def _set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            try:
                if self._on_status:
                    self._on_status(status)
            except Exception:  # noqa: BLE001
                pass

    @property
    def status(self) -> str:
        return self._status
