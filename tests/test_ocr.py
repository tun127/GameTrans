"""OCR 模块测试：坐标归一化、行合并、引擎参数、识别线程队列。"""
from __future__ import annotations

import time

import numpy as np

from gametrans.config import Config
from gametrans.core import ocr as ocr_mod
from gametrans.core.ocr import OCRResult, boxes_to_norm, merge_row_lines
from tests.helpers import isolate_home, patched


def _ln(text: str, box, conf: float = 0.9) -> OCRResult:
    return OCRResult(text, conf, list(box) if box else None)


# ---------- 坐标归一化 ----------
def test_boxes_to_norm_basic():
    box = boxes_to_norm([[0, 0], [10, 0], [10, 5], [0, 5]], 10, 10)
    assert box == [0.0, 0.0, 1.0, 0.5]


def test_boxes_to_norm_invalid_input():
    assert boxes_to_norm(None, 10, 10) is None
    assert boxes_to_norm([], 10, 10) is None


def test_boxes_to_norm_zero_size_image_safe():
    box = boxes_to_norm([[0, 0], [1, 0], [1, 1], [0, 1]], 0, 0)
    assert box is not None  # 用 max(1, w/h) 兜底，不除零


# ---------- 同一行片段合并 ----------
def test_merge_same_row_joins_with_space():
    lines = [_ln("The dragon", [0.05, 0.60, 0.30, 0.70]),
             _ln("awakens at dawn", [0.32, 0.60, 0.75, 0.70])]
    merged = merge_row_lines(lines)
    assert len(merged) == 1
    assert merged[0].text == "The dragon awakens at dawn"
    assert merged[0].box_norm[0] == 0.05 and merged[0].box_norm[2] == 0.75


def test_merge_two_rows_stay_separate():
    lines = [_ln("line one", [0.05, 0.20, 0.30, 0.28]),
             _ln("line two", [0.05, 0.60, 0.30, 0.68])]
    assert len(merge_row_lines(lines)) == 2


def test_merge_far_apart_blocks_stay_separate():
    """同一水平线上、中间隔了半屏的两块（左 HUD + 右字幕）不能被拼成一句。"""
    lines = [_ln("HUD", [0.00, 0.50, 0.10, 0.60]),
             _ln("subtitle text", [0.60, 0.50, 0.90, 0.60])]
    assert len(merge_row_lines(lines)) == 2


def test_merge_hyphenated_word_no_space():
    lines = [_ln("inter-", [0.05, 0.50, 0.20, 0.60]),
             _ln("national", [0.21, 0.50, 0.35, 0.60])]
    merged = merge_row_lines(lines)
    assert len(merged) == 1
    assert merged[0].text == "inter-national"


def test_merge_keeps_single_line_untouched():
    only = [_ln("alone", [0.1, 0.1, 0.5, 0.2])]
    assert merge_row_lines(only) is only


def test_merge_lines_without_boxes_untouched():
    lines = [OCRResult("a", 0.9, None), OCRResult("b", 0.9, None)]
    out = merge_row_lines(lines)
    assert len(out) == 2


def test_merge_confidence_uses_min():
    lines = [_ln("aa", [0.05, 0.5, 0.20, 0.6], 0.9),
             _ln("bb", [0.21, 0.5, 0.35, 0.6], 0.6)]
    merged = merge_row_lines(lines)
    assert merged[0].confidence == 0.6


# ---------- 引擎参数 ----------
def test_det_side_increases_with_quality():
    m = ocr_mod.DET_SIDE_BY_SPEED
    assert m["fast"] < m["balanced"] < m["high"]


def test_current_ocr_key_follows_config():
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("ocr", "speed", "fast")
    cfg.set("ocr", "threads", 1)
    assert ocr_mod._current_ocr_key() == ("fast", 1)
    cfg.set("ocr", "threads", 2)
    assert ocr_mod._current_ocr_key() == ("fast", 2)
    cfg.set("ocr", "speed", "high")
    assert ocr_mod._current_ocr_key() == ("high", 2)


def test_reload_engine_only_when_params_change():
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("ocr", "speed", "fast")
    cfg.set("ocr", "threads", 1)

    old_engine, old_key = ocr_mod._engine, ocr_mod._engine_key
    try:
        ocr_mod._engine, ocr_mod._engine_key = None, None
        assert ocr_mod.reload_engine_if_needed() is False  # 从未加载 → 无需重载

        ocr_mod._engine, ocr_mod._engine_key = object(), ("fast", 1)
        assert ocr_mod.reload_engine_if_needed() is False  # 参数未变

        cfg.set("ocr", "threads", 4)
        assert ocr_mod.reload_engine_if_needed() is True
        assert ocr_mod._engine is None and ocr_mod._engine_key is None
        assert ocr_mod.reload_engine_if_needed() is False  # 不重复触发
    finally:
        ocr_mod._engine, ocr_mod._engine_key = old_engine, old_key


# ---------- 识别线程 ----------
def test_ocr_worker_calls_back_once_per_text():
    seen: list = []
    fake = lambda bgr, min_conf=0.45, lang_hint="auto": [  # noqa: E731
        OCRResult("hello world", 0.9, [0.0, 0.0, 1.0, 0.2])]
    with patched(ocr_mod, "ocr_image", fake):
        worker = ocr_mod.OcrWorker(on_lines=seen.append, get_lang=lambda: "en",
                                   get_ocr_interval=lambda: 0.0)
        worker.start()
        try:
            worker.submit(np.zeros((16, 16, 3), np.uint8))
            for _ in range(60):
                if seen:
                    break
                time.sleep(0.02)
            assert len(seen) == 1, "首次识别未回调"

            worker.submit(np.zeros((16, 16, 3), np.uint8))
            time.sleep(0.3)
            assert len(seen) == 1, "同一文本重复回调（应去重）"

            worker.reset_signature()
            worker.submit(np.zeros((16, 16, 3), np.uint8))
            for _ in range(60):
                if len(seen) >= 2:
                    break
                time.sleep(0.02)
            assert len(seen) >= 2, "reset_signature 后应能再次回调"
        finally:
            worker.stop()


def test_ocr_worker_keeps_newest_frame():
    """识别忙时提交多帧，最终送进去的应是**最新**那一帧（减少延迟）。"""
    got: list = []

    def fake(bgr, min_conf=0.45, lang_hint="auto"):
        got.append(int(bgr[0, 0, 0]))
        return []

    def blocking(bgr, min_conf=0.45, lang_hint="auto"):
        got.append(int(bgr[0, 0, 0]))
        time.sleep(0.25)  # 模拟一次较慢的识别，占住工作线程
        return []

    with patched(ocr_mod, "ocr_image", blocking):
        worker = ocr_mod.OcrWorker(on_lines=lambda _l: None, get_lang=lambda: "en",
                                   get_ocr_interval=lambda: 0.0)
        worker.start()
        try:
            first = np.zeros((8, 8, 3), np.uint8)
            first[:] = 1
            worker.submit(first)
            time.sleep(0.05)          # 让工作线程开始处理第 1 帧
            older = np.zeros((8, 8, 3), np.uint8)
            older[:] = 2
            newest = np.zeros((8, 8, 3), np.uint8)
            newest[:] = 3
            worker.submit(older)
            worker.submit(newest)     # 应覆盖 older
            time.sleep(0.6)
        finally:
            worker.stop()
    assert got, "工作线程未识别任何帧"
    assert got[-1] == 3, f"应识别最新帧(3)，实际 {got}"


def test_ocr_worker_stop_is_safe_when_never_started():
    worker = ocr_mod.OcrWorker(on_lines=lambda _l: None, get_lang=lambda: "en")
    worker.stop()


# ---------- 真实识别（快速冒烟） ----------
def test_real_ocr_recognizes_rendered_text():
    import cv2
    img = np.full((220, 900, 3), 16, np.uint8)
    cv2.putText(img, "The dragon awakens", (30, 90),
                cv2.FONT_HERSHEY_SIMPLEX, 1.6, (240, 240, 240), 3, cv2.LINE_AA)
    cv2.putText(img, "Press E to open", (30, 180),
                cv2.FONT_HERSHEY_SIMPLEX, 1.6, (240, 240, 240), 3, cv2.LINE_AA)
    lines = ocr_mod.ocr_image(img, min_conf=0.3, lang_hint="en")
    text = " ".join(ln.text for ln in lines)
    assert lines, "真实 OCR 未识别到任何文本"
    assert "dragon" in text.lower(), text
    assert all(0.0 <= c <= 1.0 for ln in lines for c in (ln.box_norm or [0.0]))


def test_real_ocr_engine_uses_limited_threads():
    """引擎必须按配置限制线程，否则会吃满 CPU。"""
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("ocr", "threads", 1)
    cfg.set("ocr", "speed", "fast")
    ocr_mod._engine = None
    ocr_mod._engine_key = None
    engine = ocr_mod.get_engine()
    assert engine is not None
    assert ocr_mod._engine_key == ("fast", 1), ocr_mod._engine_key


def slow_test_ocr_cpu_cores_is_bounded():
    """慢测试：实测 OCR 单帧占用核心数（需 tens of seconds 的机器仍应 < 8 核）。"""
    import cv2
    img = np.full((626, 1286, 3), 16, np.uint8)
    cv2.putText(img, "The dragon awakens at dawn.", (60, 330),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (240, 240, 240), 3, cv2.LINE_AA)
    ocr_mod.ocr_image(img, min_conf=0.3, lang_hint="en")
    c0, w0 = time.process_time(), time.perf_counter()
    for _ in range(3):
        ocr_mod.ocr_image(img, min_conf=0.3, lang_hint="en")
    cores = (time.process_time() - c0) / max(1e-6, time.perf_counter() - w0)
    assert cores <= 6.0, f"OCR 占用 {cores:.2f} 核，疑似未限制线程"
