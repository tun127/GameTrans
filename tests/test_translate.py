"""翻译模块测试：切分、标点、词库、缓存、批翻译、翻译线程。"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

from gametrans.config import Config
from gametrans.core import translate as tr
from gametrans.core.translate import (
    Glossary,
    TranslationCache,
    looks_translated,
    split_long_text,
    translate_lines,
    zh_punct,
)
from tests.helpers import fake_lines, isolate_home, patched


# ---------- 文本处理 ----------
def test_split_long_text_keeps_short_text():
    assert split_long_text("Hello world.") == ["Hello world."]


def test_split_long_text_splits_and_bounds():
    text = ("A" * 200 + ". ") * 4
    parts = split_long_text(text, max_chars=240)
    assert len(parts) >= 2
    assert all(len(p) <= 240 * 1.5 for p in parts), [len(p) for p in parts]


def test_split_long_text_handles_no_punctuation():
    parts = split_long_text("X" * 1000, max_chars=240)
    assert len(parts) >= 4
    assert all(len(p) <= 260 for p in parts)


def test_zh_punct_converts_and_keeps_decimal_point():
    assert zh_punct("你好, 世界!") == "你好，世界！"
    assert zh_punct("真的吗?") == "真的吗？"
    assert zh_punct("价格是 3.14 元") == "价格是 3.14 元"
    assert zh_punct("已经好了; 走吧") == "已经好了；走吧"


def test_looks_translated():
    assert looks_translated("龙在黎明时醒来。") is True
    assert looks_translated("The dragon awakens.") is False
    assert looks_translated("") is False
    assert looks_translated("<unk>") is False


# ---------- 词库 ----------
def _make_glossary() -> Glossary:
    p = Path(tempfile.mkdtemp(prefix="gt_gl_")) / "wordbook.txt"
    p.write_text("# 注释行\nDragon|龙\nKnight=骑士\n\n", encoding="utf-8")
    return Glossary(p)


def test_glossary_load_and_apply():
    g = _make_glossary()
    assert g.apply("Dragon 出现了") == "龙 出现了"
    assert g.apply("Knight 站在那里") == "骑士 站在那里"
    assert g.apply("无关文本") == "无关文本"


def test_glossary_pre_apply_replaces_term_directly():
    """当前实现直接把术语前置替换为译名（不用占位符，NLLB 处理不了占位符）。"""
    g = _make_glossary()
    feed, mapping = g.pre_apply("Dragon 出现了")
    assert "Dragon" not in feed and "龙" in feed, "术语应被前置替换为译名"
    assert mapping == {}, "不使用占位符方案"
    # restore 对空映射原样返回
    assert Glossary.restore("某个文本", mapping) == "某个文本"


def test_glossary_reloads_when_file_changes():
    p = Path(tempfile.mkdtemp(prefix="gt_gl2_")) / "wordbook.txt"
    p.write_text("Dragon|龙\n", encoding="utf-8")
    g = Glossary(p)
    assert g.apply("Dragon") == "龙"
    p.write_text("Dragon|巨龙\n", encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime + 5, st.st_mtime + 5))  # 强制 mtime 变化
    assert g.apply("Dragon") == "巨龙"


def test_glossary_missing_file_is_safe():
    g = Glossary(Path(tempfile.mkdtemp(prefix="gt_gl3_")) / "nope.txt")
    assert g.apply("Dragon") == "Dragon"  # 不存在时退化为原文


# ---------- 缓存 ----------
def test_cache_put_get_and_language_isolation():
    db = Path(tempfile.mkdtemp(prefix="gt_cache_")) / "cache.sqlite"
    c = TranslationCache(db)
    c.put("en", "hello", "你好")
    assert c.get("en", "hello") == "你好"
    assert c.get("ja", "hello") is None
    c.put("en", "hello", "您好")
    assert c.get("en", "hello") == "您好"


# ---------- 批翻译 ----------
class _FakeEngine:
    """假翻译引擎：记录调用参数，按字典返回译文。"""

    def __init__(self, mapping=None) -> None:
        self.calls = []
        self.mapping = mapping or {}

    def translate_many(self, texts, src_lang, context=None):
        self.calls.append((list(texts), src_lang, list(context) if context else None))
        return [self.mapping.get(t, f"译:{t}") for t in texts]


class _EchoEngine:
    """模拟失败透传：把原文当译文返回（不含汉字，应被丢弃）。"""

    def translate_many(self, texts, src_lang, context=None):
        return list(texts)


def _env():
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("auto_cache", True)
    return cfg, TranslationCache(home / "cache.sqlite"), Glossary(cfg.glossary_file())


def test_translate_lines_basic_and_cache():
    cfg, cache, gl = _env()
    eng = _FakeEngine({"Hello": "你好"})
    lines = fake_lines(["Hello", "World"])
    out = translate_lines(cfg, lines, "en", translator=eng, cache=cache, glossary=gl)
    assert out[0] == "你好"
    assert out[1].startswith("译"), out  # zh_punct 会把 : 转成 ：
    assert len(out) == 2

    eng.calls.clear()
    again = translate_lines(cfg, lines, "en", translator=eng, cache=cache, glossary=gl)
    assert again[0] == "你好"
    # 命中缓存的那条不应再送去翻译
    assert all("Hello" not in texts for texts, _l, _c in eng.calls)


def test_translate_lines_empty_keeps_positions():
    cfg, cache, gl = _env()
    eng = _FakeEngine()
    out = translate_lines(cfg, fake_lines(["Hi", "   ", ""]), "en",
                          translator=eng, cache=cache, glossary=gl)
    assert len(out) == 3
    assert out[1] == "" and out[2] == ""


def test_translate_lines_drops_non_chinese_result():
    cfg, cache, gl = _env()
    out = translate_lines(cfg, fake_lines(["Hello there"]), "en",
                          translator=_EchoEngine(), cache=cache, glossary=gl)
    assert out == [""], "非中文结果必须被丢弃（既不显示也不缓存）"
    assert cache.get("en", "Hello there") is None


def test_translate_lines_passes_context_to_engine():
    cfg, cache, gl = _env()
    eng = _FakeEngine()
    translate_lines(cfg, fake_lines(["Hi"]), "en", translator=eng,
                    cache=cache, glossary=gl, context=["上一句原文"])
    assert eng.calls[0][2] == ["上一句原文"]


def test_translate_lines_ignores_dirty_cache_entry():
    """缓存里若混入纯英文脏数据，必须忽略并重新翻译。"""
    cfg, cache, gl = _env()
    cache.put("en", "Hello", "Hello")  # 脏数据
    eng = _FakeEngine({"Hello": "你好"})
    out = translate_lines(cfg, fake_lines(["Hello"]), "en",
                          translator=eng, cache=cache, glossary=gl)
    assert out == ["你好"]
    assert eng.calls, "脏缓存应触发重新翻译"


def test_translate_lines_empty_input():
    cfg, cache, gl = _env()
    assert translate_lines(cfg, [], "en", translator=_FakeEngine(),
                          cache=cache, glossary=gl) == []


# ---------- 翻译线程 ----------
class _WorkerEnv:
    """构造一个使用假引擎的 TranslateWorker。"""

    def __init__(self) -> None:
        self.home = isolate_home()
        self.cfg = Config(self.home / "config.json")
        self.cfg.set("auto_cache", False)
        self.cfg.set("context_pieces", 3)
        self.results = []
        self.calls = []
        self.done = threading.Event()
        self.worker = None

    def start(self, module=tr):
        outer = self

        class FakeRouter:
            # 真实 EngineRouter 有 .local（LocalTranslator），
            # _loop 会调 router.local.load() 预热本地引擎，假对象必须提供
            def __init__(self, *_a, **_k) -> None:
                self.local = type("L", (), {"load": lambda self: True})()

            def translate_many(self, texts, src_lang, context=None):
                outer.calls.append((list(texts), list(context or [])))
                return [f"译{i}" for i in range(len(texts))]

        self._patch = patched(module, "EngineRouter", FakeRouter)
        self._patch.__enter__()
        self.worker = module.TranslateWorker(
            self.cfg,
            on_done=lambda _l, dst, _s: (self.results.append(dst), self.done.set()),
        )
        self.worker.start()
        return self.worker

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
        self._patch.__exit__(None, None, None)


def test_translate_worker_dedup_and_context():
    env = _WorkerEnv()
    env.start()
    try:
        env.worker.submit(fake_lines(["Hello there"]), "en")
        assert env.done.wait(6.0), "首批翻译超时"
        assert env.results[0][0].startswith("译")

        env.done.clear()
        env.worker.submit(fake_lines(["Second line"]), "en")
        assert env.done.wait(6.0), "第二批翻译超时"

        assert env.calls[0][1] == [], "首批不应带上下文"
        assert "Hello there" in env.calls[1][1], "第二批应携带上文"

        # 同一句重复提交 → 去重，不再翻译
        n = len(env.calls)
        env.worker.submit(fake_lines(["Second line"]), "en")
        time.sleep(0.4)
        assert len(env.calls) == n, "相同原文被重复翻译"
    finally:
        env.stop()


def test_translate_worker_reset_clears_context():
    env = _WorkerEnv()
    env.start()
    try:
        env.worker.submit(fake_lines(["Hello there"]), "en")
        assert env.done.wait(6.0)
        assert env.worker._recent, "上文应被累积"
        env.worker.reset()
        assert env.worker._recent == []
        assert env.worker._last_key is None
    finally:
        env.stop()


def test_translate_worker_stop_is_safe():
    env = _WorkerEnv()
    env.start()
    env.stop()
    env.stop()  # 重复停止不应抛异常
