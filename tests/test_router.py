"""引擎路由测试：尝试顺序、故障回退、失败冷却、致命停止。

全部使用假引擎，不加载真实模型、不访问网络。
"""
from __future__ import annotations

from gametrans.config import Config
from gametrans.core.translate import EngineRouter
from tests.helpers import isolate_home


class _FakeLocal:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.last_error = "" if ok else "模型缺失"
        self.calls = []
        self.load_calls = 0

    def load(self) -> bool:
        self.load_calls += 1
        return self.ok

    def translate_many(self, texts, src_lang, context=None):
        self.calls.append((list(texts), list(context or [])))
        return [f"本地:{t}" for t in texts]


class _FakeApi:
    def __init__(self, ready: bool = True, fail: bool = False) -> None:
        self._ready = ready
        self.fail = fail
        self.calls = 0

    @property
    def ready(self) -> bool:
        return self._ready

    def translate_many(self, texts, src_lang, context=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("模拟网络错误")
        return [f"API:{t}" for t in texts]


def _router(mode: str = "local_first", local_ok: bool = True,
            api_ready: bool = True, api_fail: bool = False):
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("engine", mode)
    warns: list = []
    fatals: list = []
    r = EngineRouter(cfg, on_warning=warns.append, on_fatal=fatals.append)
    r._local = _FakeLocal(local_ok)
    api = _FakeApi(api_ready, api_fail)
    r._get_api = lambda: api          # 实例属性覆盖方法：避免真实网络请求
    return r, cfg, warns, fatals, api


def test_engine_order_by_mode():
    assert EngineRouter._engine_order("local_first") == ["local", "api"]
    assert EngineRouter._engine_order("api") == ["api", "local"]
    assert EngineRouter._engine_order("auto") == ["api", "local"]
    assert EngineRouter._engine_order("local") == ["local"]


def test_local_first_uses_local():
    r, _cfg, warns, fatals, api = _router("local_first")
    out = r.translate_many(["Hello"], "en")
    assert out == ["本地:Hello"]
    assert r.last_used == "local"
    assert api.calls == 0
    assert not warns and not fatals


def test_local_first_falls_back_to_api_when_local_unavailable():
    r, _cfg, warns, fatals, api = _router("local_first", local_ok=False)
    out = r.translate_many(["Hello"], "en")
    assert out == ["API:Hello"]
    assert r.last_used == "api"
    assert warns and "本地翻译不可用" in warns[0]
    assert not fatals


def test_api_mode_falls_back_to_local_when_api_fails():
    r, _cfg, warns, fatals, api = _router("api", api_fail=True)
    out = r.translate_many(["Hello"], "en")
    assert out == ["本地:Hello"]
    assert r.last_used == "local"
    assert warns and "API 不可用" in warns[0]


def test_api_failure_enters_cooldown():
    """API 失败后 20 秒内不再重复尝试（避免每批字幕都打超时）。"""
    r, _cfg, _warns, _fatals, api = _router("api", api_fail=True)
    r.translate_many(["One"], "en")
    assert api.calls == 1
    r.translate_many(["Two"], "en")
    r.translate_many(["Three"], "en")
    assert api.calls == 1, "冷却期内不应再次调用 API"
    assert r.last_used == "local"


def test_api_mode_without_key_falls_back_local():
    r, _cfg, warns, _fatals, api = _router("api", api_ready=False)
    out = r.translate_many(["Hello"], "en")
    assert out == ["本地:Hello"]
    assert api.calls == 0
    assert warns and "API" in warns[0]


def test_local_only_unavailable_is_fatal_and_passes_text_through():
    """仅本地模式且本地不可用：停止翻译（提示一次），返回原文由上层丢弃。"""
    r, _cfg, _warns, fatals, _api = _router("local", local_ok=False)
    out = r.translate_many(["Hello"], "en")
    assert out == ["Hello"], "返回原文，上层会因'无汉字'而丢弃"
    assert r.last_used == "fatal"
    assert len(fatals) == 1 and "翻译已停止" in fatals[0]


def test_fatal_notified_only_once():
    r, _cfg, _warns, fatals, _api = _router("local", local_ok=False)
    for _ in range(3):
        r.translate_many(["Hello"], "en")
    assert len(fatals) == 1, "致命提示不应每批重复"


def test_context_is_passed_through():
    r, _cfg, _warns, _fatals, _api = _router("local_first")
    r.translate_many(["Hello"], "en", context=["上文一句"])
    assert r._local.calls[0][1] == ["上文一句"]


def test_local_load_called_each_time_but_real_load_is_idempotent():
    """路由每次都会调 load()，但真实实现是幂等的（已加载直接返回）。"""
    r, _cfg, _warns, _fatals, _api = _router("local_first")
    r.translate_many(["One"], "en")
    r.translate_many(["Two"], "en")
    assert r._local.load_calls == 2  # 调用次数
    from gametrans.core.translate import LocalTranslator
    lt = LocalTranslator(Config(isolate_home() / "config.json"))
    lt._translator = object()  # 模拟"已加载"
    assert lt.load() is True   # 幂等：不会重新加载
