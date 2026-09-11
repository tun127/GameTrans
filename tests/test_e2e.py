"""端到端集成测试（慢）：合成字幕帧 → OCR → 本地 NLLB 翻译 → UI 收到中文。

- 全程 QT_QPA_PLATFORM=offscreen：不创建任何可见窗口，不弹提示条，
  不会打扰正在使用的桌面；
- 不启动真实抓屏（capture 线程因无区域而空转），帧由测试直接喂入；
- 需要本地模型与显卡，约 30~60 秒；未装模型时自动跳过。
运行：python tests/run_all.py --slow
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from tests.helpers import isolate_home, qt_app

MODEL_BIN = Path(r"D:\GameTrans\models\nllb-200-distilled-600M\model.bin")


def slow_test_e2e_ocr_to_translation_offscreen():
    if not MODEL_BIN.exists():
        print(f"[SKIP] 未找到本地模型 {MODEL_BIN}，跳过端到端测试")
        return
    app = qt_app()  # offscreen：所有窗口都画在虚拟屏上，屏幕上看不到
    from gametrans.app import GameTransApp
    from gametrans.config import Config

    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("hotkey_enabled", False)
    cfg.set("engine", "local")           # 走真实本地引擎（GPU）
    cfg.set("model_dir", str(MODEL_BIN.parent))
    cfg.set("source_lang", "en")
    cfg.set("anchor", "type", "screen")
    # 不设 region：GameTransApp 启动会清空且 capture 线程因无区域空转（零抓屏开销）；
    # 帧由测试直接喂入 _on_capture_frame，绕过抓屏但仍走完整 OCR→翻译→UI 链路
    cfg.set("ocr_min_conf", 0.3)

    ctrl = GameTransApp(cfg)
    ctrl.toast.show_message = lambda *a, **k: None
    try:
        ctrl._start_pipeline()
        ctrl.running = True
        assert ctrl._ocr_worker is not None and ctrl._ocr_worker._thread.is_alive()

        # 合成一张含英文字幕的帧（避免依赖桌面内容）
        frame = np.full((220, 900, 3), 16, np.uint8)
        cv2.putText(frame, "The dragon awakens at dawn.", (30, 120),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, (240, 240, 240), 3, cv2.LINE_AA)
        ctrl._on_capture_frame(frame, (0, 0, 900, 220))

        # 等 UI 收到中文译文（首次包含模型加载，给足 90 秒）
        deadline = time.monotonic() + 90.0
        zh_texts: list = []
        while time.monotonic() < deadline:
            app.processEvents()
            t = ctrl._last_translation
            if t is not None:
                zh_texts = [d for d in t[1]
                            if d and any("\u4e00" <= ch <= "\u9fff" for ch in d)]
                if zh_texts:
                    break
            time.sleep(0.05)

        assert zh_texts, f"90 秒内未收到中文译文（last={ctrl._last_translation!r}）"
        joined = "".join(zh_texts)
        print(f"[info] 端到端译文: {joined}")
        assert ("龙" in joined) or ("黎明" in joined), \
            f"译文语义不符：{joined}"
    finally:
        ctrl._stop_pipeline()
        ctrl.quit()
        app.processEvents()
