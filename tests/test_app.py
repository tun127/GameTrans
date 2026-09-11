"""主控制器测试：启动校验、OCR 结果过滤、设置即时生效（不启动真实管线）。"""
from __future__ import annotations

from gametrans.app import GameTransApp
from gametrans.config import Config
from tests.helpers import fake_lines, isolate_home, patched, qt_app


def _app(**overrides):
    qt_app()
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("hotkey_enabled", False)
    cfg.set("engine", "api")            # 避免加载本地模型
    cfg.set("deepseek", "api_key", "sk-test")
    for k, v in overrides.items():
        cfg.set(k, v)
    app = GameTransApp(cfg)
    app.toast.show_message = lambda *a, **k: None
    return app, cfg


# ---------- 启动 ----------
def test_init_clears_region_and_anchor():
    """启动必须回到全新选择状态：区域与目标窗口都不保留上次的。"""
    qt_app()
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("hotkey_enabled", False)
    cfg.set("engine", "api")
    cfg.set("deepseek", "api_key", "sk-test")
    cfg.set("region", {"nx": 0.2, "ny": 0.2, "nw": 0.3, "nh": 0.3})
    cfg.set("anchor", "type", "window")
    cfg.set("anchor", "title_contains", "上次选的游戏窗口")
    app = GameTransApp(cfg)
    try:
        assert cfg.get("region") is None, "启动应清空上次的区域（每次都要重新框选）"
        anchor = cfg.get("anchor", default={})
        assert anchor.get("type") == "screen", \
            f"启动应把目标窗口重置为整屏，实际: {anchor}"
        assert anchor.get("title_contains") == "", \
            f"启动不应保留上次的窗口名，实际: {anchor!r}"
        assert app.bar.btn_pick.text() == "整屏", \
            f"控制条按钮应显示『整屏』，实际: {app.bar.btn_pick.text()}"
        assert app.running is False
    finally:
        app.quit()


def test_block_reason_api_without_key():
    app, cfg = _app()
    try:
        cfg.set("engine", "api")
        cfg.set("deepseek", "api_key", "")
        assert app._translation_block_reason(), "仅 API 且无 Key 应阻止启动"
    finally:
        app.quit()


def test_block_reason_local_missing_model():
    app, cfg = _app()
    try:
        cfg.set("engine", "local")
        cfg.set("model_dir", "D:\\绝对不存在的模型目录_ZZZ")
        assert app._translation_block_reason(), "本地引擎缺模型应阻止启动"
    finally:
        app.quit()


def test_block_reason_ok_when_api_has_key():
    app, _cfg = _app()
    try:
        assert app._translation_block_reason() == ""
    finally:
        app.quit()


# ---------- 静态过滤规则 ----------
def test_ignore_phrases_parsing():
    qt_app()
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("ignore_phrases", "Dify，超频; 淘宝热卖\nFPS")
    assert GameTransApp._ignore_phrases(cfg) == ["dify", "超频", "淘宝热卖", "fps"]


def test_is_ignored_substring_match():
    phrases = ["dify", "fps"]
    assert GameTransApp._is_ignored("Dify something", phrases) is True
    assert GameTransApp._is_ignored("show FPS 60", phrases) is True
    assert GameTransApp._is_ignored("Hello world", phrases) is False
    assert GameTransApp._is_ignored("Hello", []) is False


def test_noise_line_detection():
    assert GameTransApp._is_noise_line("12:30") is True
    assert GameTransApp._is_noise_line("0.024") is True
    assert GameTransApp._is_noise_line("6K") is True
    assert GameTransApp._is_noise_line("Hello there") is False
    assert GameTransApp._is_noise_line("こんにちは") is False   # 含假名可能是日文
    assert GameTransApp._is_noise_line("已经中文") is False      # 交给回声过滤


def test_echo_filter():
    assert GameTransApp._looks_like_our_translation("你好世界", "en") is True
    assert GameTransApp._looks_like_our_translation("Hello", "en") is False
    assert GameTransApp._looks_like_our_translation("竜が目を覚ます", "ja") is False
    assert GameTransApp._looks_like_our_translation("龙在黎明时醒来", "ja") is True


# ---------- OCR 回调链路 ----------
def test_on_ocr_lines_filters_and_submits_only_valid():
    app, cfg = _app()
    try:
        app.running = True
        cfg.set("min_line_len", 2)
        cfg.set("ignore_phrases", "Dify,超频")
        submitted = []
        with patched(app._translate_worker, "submit",
                     lambda lines, lang: submitted.append(([ln.text for ln in lines], lang))):
            app.on_ocr_lines(fake_lines([
                "OK line",        # 保留
                "x",              # 太短
                "Dify 提示",      # 命中忽略短语
                "超频 4.2",       # 命中忽略短语
                "12:30",          # 噪声
                "已经中文了",     # 回声过滤
            ]))
        assert submitted, "应有有效文本被提交"
        assert submitted[0][0] == ["OK line"], submitted[0][0]
    finally:
        app.quit()


def test_on_ocr_lines_dedup_same_text():
    app, _cfg = _app()
    try:
        app.running = True
        submitted = []
        with patched(app._translate_worker, "submit",
                     lambda lines, lang: submitted.append(([ln.text for ln in lines], lang))):
            app.on_ocr_lines(fake_lines(["Same line"]))
            app.on_ocr_lines(fake_lines(["Same line"]))   # 同句不重复
        assert len(submitted) <= 1, "相同字幕不应重复投递翻译"
    finally:
        app.quit()


def test_on_ocr_lines_clears_payload_on_double_empty():
    app, _cfg = _app()
    try:
        app.running = True
        app._last_src_key = "previous"
        app._last_render_ts = 0.0   # 早已超过最短保持时间
        cleared = []
        with patched(app._display, "clear_payload", lambda: cleared.append(1)):
            app.on_ocr_lines([])
            app.on_ocr_lines([])
        assert cleared, "连续两次读空且超过最短保持时应清空译文"
    finally:
        app.quit()


def test_on_ocr_lines_ignored_when_not_running():
    app, _cfg = _app()
    try:
        app.running = False
        submitted = []
        with patched(app._translate_worker, "submit",
                     lambda lines, lang: submitted.append(1)):
            app.on_ocr_lines(fake_lines(["Should not submit"]))
        assert not submitted, "未运行时不应投递翻译"
    finally:
        app.quit()


# ---------- 设置即时生效 ----------
def test_on_settings_applied_updates_scale_and_toast():
    app, cfg = _app()
    try:
        cfg.set("ui_scale", 1.5)
        cfg.set("toast_pos", "bottom-left")
        app._on_settings_applied()  # 不应抛异常
        assert app.bar.height() == 78
        assert app.toast._corner == "bottom-left"
    finally:
        app.quit()


def test_on_settings_applied_reloads_ocr_engine_when_threads_change():
    app, cfg = _app()
    try:
        from gametrans.core import ocr as ocr_mod
        cfg.set("ocr", "threads", 4)
        old_engine, old_key = ocr_mod._engine, ocr_mod._engine_key
        ocr_mod._engine, ocr_mod._engine_key = object(), ("fast", 1)
        try:
            app._on_settings_applied()
            assert ocr_mod._engine is None, "线程数变化后应丢弃旧引擎待重建"
        finally:
            ocr_mod._engine, ocr_mod._engine_key = old_engine, old_key
    finally:
        app.quit()
