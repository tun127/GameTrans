"""OCR 识别：基于 RapidOCR（ONNX 运行时），提供文本行+坐标输出。

设计要点：
- RapidOCR 初始化慢（模型加载数百 ms），因此使用单例懒加载。
- 帧在独立线程排队识别（OCR 不阻塞捕获线程）。
- 返回行级结果，含归一化坐标与置信度，供显示层/翻译层使用。
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


# 识别速度档位 -> 检测输入边长（越小越快，精度略降）
DET_SIDE_BY_SPEED = {"fast": 640, "balanced": 960, "high": 1280}


def _current_ocr_key() -> tuple:
    """当前 OCR 引擎参数 (speed, threads)，用于判断是否需要重建引擎。"""
    try:
        from ..config import Config  # 只取默认值，避免依赖实例
        ocr = Config().get("ocr", default={}) or {}
        return (str(ocr.get("speed", "fast")).lower(),
                int(ocr.get("threads", 0) or 0))
    except Exception:  # noqa: BLE001
        return ("fast", 0)


def _load_engine():
    """延迟加载 RapidOCR，避免拖慢程序启动。

    intra_op_num_threads 是关键：ONNX Runtime 默认会把线程数设为逻辑核心总数，
    单次识别就能占满整机 CPU（实测 20 核 ≈ 18 核），必须显式限制。
    """
    global _engine_key
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:  # noqa: BLE001
        log.error("RapidOCR 导入失败: %s", exc)
        return None

    speed, threads = _current_ocr_key()
    side = DET_SIDE_BY_SPEED.get(speed, 960)
    kw = {"det_limit_side_len": side}
    if threads > 0:
        kw["intra_op_num_threads"] = threads
    try:
        engine = RapidOCR(**kw)
    except TypeError:
        # 该版本不支持线程参数：至少保住检测尺寸限制
        try:
            engine = RapidOCR(det_limit_side_len=side)
        except TypeError:
            engine = RapidOCR()
        threads = 0
    except Exception as exc:  # noqa: BLE001
        log.error("RapidOCR 初始化失败: %s", exc)
        return None
    _engine_key = (speed, threads)
    log.info("RapidOCR 已加载(speed=%s det_side=%d threads=%s)",
             speed, side, threads or "默认(全部核心)")
    return engine


_engine = None
_engine_lock = threading.Lock()
_engine_key: Optional[tuple] = None  # 已加载引擎对应的 (speed, threads)


def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _load_engine()
    return _engine


def reload_engine_if_needed() -> bool:
    """OCR 档位/线程数变了就丢弃旧引擎，下次识别按新参数重建。

    不重建时直接返回 False（避免每次保存设置都白付一次加载开销）。
    """
    global _engine, _engine_key
    key = _current_ocr_key()
    with _engine_lock:
        if _engine is not None and key == _engine_key:
            return False
        if _engine is None and _engine_key is None:
            return False  # 还没加载过：无需处理
        log.info("OCR 参数变化 %s -> %s，重载引擎", _engine_key, key)
        _engine = None
        _engine_key = None
    return True


class OCRResult:
    """一行识别结果。坐标为相对原图的 0..1（显示层会再做坐标换算）。"""

    __slots__ = ("text", "confidence", "box_norm")

    def __init__(self, text: str, confidence: float,
                 box_norm: Optional[List[float]] = None) -> None:
        self.text = text
        self.confidence = confidence
        self.box_norm = box_norm  # [x0,y0,x1,y1] 归一化（左上右下）

    def __repr__(self) -> str:
        return f"<OCR {self.confidence:.2f} {self.text!r}>"


def boxes_to_norm(boxes, w: int, h: int) -> Optional[List[float]]:
    """把四个角点归一化为 [x0,y0,x1,y1]；无框返回 None。"""
    if boxes is None:
        return None
    try:
        xs = [p[0] for p in boxes]
        ys = [p[1] for p in boxes]
        x0 = min(xs) / max(1, w)
        y0 = min(ys) / max(1, h)
        x1 = max(xs) / max(1, w)
        y1 = max(ys) / max(1, h)
        return [x0, y0, x1, y1]
    except Exception:  # noqa: BLE001
        return None


class OcrWorker:
    """独立线程 OCR：捕获帧进来后立即排队识别，不阻塞主流程。

    - 队列只保留最新一帧：若识别中又来了新帧，直接覆盖待办帧（字幕场景够用）。
    - 连续相同文本只回调一次（配合字幕稳定出现的游戏，避免重复翻译）。
    """

    def __init__(self,
                 on_lines,
                 get_lang,
                 get_min_conf=lambda: 0.45,
                 get_ocr_interval=lambda: 0.35,
                 name: str = "ocr") -> None:
        self._on_lines = on_lines
        self._get_lang = get_lang
        self._get_min_conf = get_min_conf
        self._get_ocr_interval = get_ocr_interval
        self._queue = queue.Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_sig: Optional[str] = None
        self._name = name

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # 唤醒可能的阻塞
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=2.0)
        self._thread = None

    def submit(self, bgr: np.ndarray) -> None:
        """提交一帧用于识别；识别繁忙时丢弃旧帧、只留最新。

        队列满说明上一帧还没被取走（画面已经更新了）。这时必须丢掉**旧**帧，
        否则识别的一直是过期画面，字幕会白白多延迟一个识别周期。
        """
        if not self._thread or not self._thread.is_alive():
            return
        frame = bgr.copy()
        try:
            self._queue.put_nowait(frame)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()  # 丢掉过期的旧帧
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            pass  # 极端并发下仍满：直接放弃这一帧，不阻塞调用方

    def _loop(self) -> None:
        while not self._stop.is_set():
            # 每次循环动态读取：设置里调整 OCR 间隔后无需重启管线即生效
            interval = max(0.0, self._get_ocr_interval())
            try:
                frame = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if frame is None:
                break
            # 若识别过程中又积累了更新帧，取最新一次
            drained = 0
            while not self._queue.empty():
                try:
                    newer = self._queue.get_nowait()
                    if newer is not None:
                        frame = newer
                        drained += 1
                except queue.Empty:
                    break
            # 先合并成整行再回调：OCR 常把一行字幕切成多个词块，若不合并，
            # 上层会按"块"做长度过滤（单字母 E 被当噪点丢掉）和整页模式判断
            # （一行 5 个词块被误判成整页 → 抓屏/OCR 降频），必须在此处归一。
            lines = merge_row_lines(self._safe_ocr(frame))
            sig = "|".join(ln.text for ln in lines)
            if lines and sig != self._last_sig:
                self._last_sig = sig
                try:
                    self._on_lines(lines)
                except Exception:  # noqa: BLE001
                    log.exception("OCR 回调异常")
            if interval > 0:
                self._stop.wait(interval)

    def _safe_ocr(self, bgr: np.ndarray) -> List[OCRResult]:
        try:
            return ocr_image(bgr,
                             min_conf=self._get_min_conf(),
                             lang_hint=self._get_lang())
        except Exception as exc:  # noqa: BLE001
            log.error("OCR 识别失败: %s", exc)
            return []

    def reset_signature(self) -> None:
        self._last_sig = None


def ocr_image(bgr: np.ndarray,
              min_conf: float = 0.45,
              lang_hint: str = "auto") -> List[OCRResult]:
    """识别一帧，返回过滤低置信后的文本行。lang_hint 暂为扩展位。

    RapidOCR 默认英文+中文模型；日文含汉字/假名也能较好识别。
    """
    engine = get_engine()
    if engine is None:
        return []
    try:
        result, _elapse = engine(bgr)
    except Exception as exc:  # noqa: BLE001
        log.error("OCR 识别异常: %s", exc)
        return []

    if not result:
        return []
    lines: List[OCRResult] = []
    h, w = bgr.shape[:2]
    for item in result:
        try:
            # RapidOCR 返回 [box, text, score]
            box, text, score = item
            text = str(text).strip()
            score = float(score)
            if not text or score < min_conf:
                continue
            lines.append(OCRResult(text, score, boxes_to_norm(box, w, h)))
        except Exception:  # noqa: BLE001
            continue
    lines.sort(key=lambda ln: (ln.box_norm[1] if ln.box_norm else 0.0))
    return lines


def merge_row_lines(lines: List[OCRResult]) -> List[OCRResult]:
    """把同一物理文本行被 OCR 断成多个检测框的情况重新拼接。

    典型场景：一行字幕因排版/间隙被切成 “The dragon / awakens at dawn”，
    逐片段翻译会得到不完整译文。这里按“垂直中心相近 + 水平相邻”归并回整行：
    - 垂直多行字幕各行保持独立；
    - 无框的行保持原样；
    - 拼回后 box 为该行所有片段的外接矩形（显示层据此覆盖整行）。
    """
    if len(lines) < 2:
        return lines
    heights = [
        b[3] - b[1] for b in (ln.box_norm for ln in lines) if b
    ]
    if not heights:
        return lines
    med_h = max(0.015, sorted(heights)[len(heights) // 2])
    row_tol = med_h * 0.45  # 垂直中心允差：同一行的多框中心差通常在行高内
    # 同行水平拼接允差：2 倍行高，并封顶为区域宽度的 45%。
    # - 字幕常因漏识别中间词留下较大空隙（实测 "The" + "at dawn." 间隙≈0.41，
    #   约 2 倍行高），阈值过严就拼不回整句、译文残缺；
    # - 封顶 45% 可避免把横跨屏幕的左右两块（如左侧 HUD + 右侧字幕）误拼成一句。
    gap_tol = min(max(med_h * 2.0, 0.02), 0.45)

    # 1) 行聚类：逐行(已按 y 排序)归入 y 中心相近的行
    rows: List[list] = []  # 每行: [ [OCRResult, ...], 当前行中心y ]
    for ln in lines:
        box = ln.box_norm
        if not box:
            rows.append([[ln], None])
            continue
        yc = (box[1] + box[3]) / 2.0
        placed = False
        for idx, (grp, center) in enumerate(rows):
            if center is None:
                continue
            if abs(yc - center) <= row_tol:
                grp.append(ln)
                # 更新该行中心为片段中心均值
                boxes = [g.box_norm for g in grp if g.box_norm]
                rows[idx][1] = sum((b[1] + b[3]) / 2.0 for b in boxes) / len(boxes)
                placed = True
                break
        if not placed:
            rows.append([[ln], yc])

    # 2) 行内按 x 排序并拼接相邻片段（间隙大于半行高视为不同文本块，不拼）
    out: List[OCRResult] = []
    for grp, _center in rows:
        if len(grp) < 2 or any(ln.box_norm is None for ln in grp):
            out.extend(grp)
            continue
        grp.sort(key=lambda ln: (ln.box_norm[0], ln.box_norm[2]))  # type: ignore[index]
        merged: List[OCRResult] = []
        cur: OCRResult | None = None
        for ln in grp:
            b = ln.box_norm
            assert b is not None
            if cur is None:
                cur = ln
                continue
            cb = cur.box_norm
            assert cb is not None
            gap = b[0] - cb[2]
            if gap < gap_tol:
                # 拼接文本：片段间补一个空格（前片段以连字符结尾时不补）
                left = cur.text.rstrip()
                right = ln.text.strip()
                joined = left + right
                if left and right and not left.endswith("-"):
                    joined = left + " " + right
                box = [min(cb[0], b[0]), min(cb[1], b[1]),
                       max(cb[2], b[2]), max(cb[3], b[3])]
                cur = OCRResult(joined, min(cur.confidence, ln.confidence), box)
            else:
                merged.append(cur)
                cur = ln
        if cur is not None:
            merged.append(cur)
        out.extend(merged)
    return out
