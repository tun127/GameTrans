"""API 翻译引擎测试：就绪判断、提示词注入、返回解析、错误处理（全部走假网络）。"""
from __future__ import annotations

import json

from gametrans.config import Config
from gametrans.core.deepseek import OpenAICompatClient, guess_service_name
from tests.helpers import isolate_home, patched


class _Resp:
    def __init__(self, payload=None, status=200, text="") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    """记录请求体，返回预置响应。"""

    def __init__(self, resp: _Resp) -> None:
        self.resp = resp
        self.body = None
        self.headers = None
        self.url = None

    def post(self, url, **kwargs):  # noqa: ANN003
        self.url = url
        self.body = kwargs.get("json")
        self.headers = kwargs.get("headers")
        return self.resp


def _client(reply="[\"你好\"]"):
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("deepseek", "api_key", "sk-test")
    cfg.set("deepseek", "base_url", "https://api.deepseek.com/v1")
    payload = {"choices": [{"message": {"content": reply}}]}
    session = _Session(_Resp(payload))
    client = OpenAICompatClient(cfg)
    return client, cfg, session


def test_guess_service_name():
    assert "DeepSeek" in guess_service_name("https://api.deepseek.com/v1")
    assert guess_service_name("https://api.openai.com/v1")
    assert guess_service_name("") == "API"


def test_ready_requires_api_key():
    client, cfg, _s = _client()
    assert client.ready is True
    cfg.set("deepseek", "api_key", "")
    assert OpenAICompatClient(cfg).ready is False


def test_prompt_injects_game_info_and_context():
    client, cfg, session = _client()
    cfg.set("game_info", "中世纪奇幻 RPG，主角叫 Arthur")
    with patched(client, "_get_session", lambda: session):
        out = client.translate_many(["The dragon awakens"], "en",
                                    context=["He drew his sword。"])
    assert out == ["你好"]
    msgs = session.body["messages"]
    assert "中世纪奇幻 RPG" in msgs[0]["content"], "内容背景应进入 system 提示"
    user = msgs[1]["content"]
    assert "The dragon awakens" in user
    assert "He drew his sword" in user, "上文应注入用户提示"
    assert "不要翻译" in user, "上文必须声明不参与输出，避免污染结果数组"
    assert session.headers.get("Authorization", "").startswith("Bearer ")


def test_prompt_without_context_has_no_context_block():
    client, cfg, session = _client()
    with patched(client, "_get_session", lambda: session):
        client.translate_many(["Hello"], "en")
    assert "上文" not in session.body["messages"][1]["content"]


def test_translate_many_blank_texts_safe():
    client, cfg, session = _client()
    with patched(client, "_get_session", lambda: session):
        out = client.translate_many(["", "   "], "en")
    assert len(out) == 2
    assert all(isinstance(x, str) for x in out)


def test_parse_json_array():
    assert OpenAICompatClient._parse('["你好","世界"]', 2, ["a", "b"]) == ["你好", "世界"]


def test_parse_code_fenced_json():
    text = "```json\n[\"你好\"]\n```"
    assert OpenAICompatClient._parse(text, 1, ["a"]) == ["你好"]


def test_parse_dict_values():
    text = '{"0": "你好", "1": "世界"}'
    assert OpenAICompatClient._parse(text, 2, ["a", "b"]) == ["你好", "世界"]


def test_parse_plain_lines_fallback():
    text = "你好\n世界"
    assert OpenAICompatClient._parse(text, 2, ["a", "b"]) == ["你好", "世界"]


def test_parse_truncates_or_falls_back_on_length_mismatch():
    out = OpenAICompatClient._parse('["a","b","c"]', 2, ["x", "y"])
    assert len(out) == 2


def test_parse_garbage_aligns_length():
    """纯垃圾文本无法解析为 JSON 时，按行兜底并对齐到目标长度（不抛错）。"""
    out = OpenAICompatClient._parse("$$$", 2, ["x", "y"])
    assert len(out) == 2  # 多退少补，避免整批失败显示英文原文


def test_http_error_raises():
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("deepseek", "api_key", "sk-test")
    client = OpenAICompatClient(cfg)
    session = _Session(_Resp(None, status=500))
    raised = False
    with patched(client, "_get_session", lambda: session):
        try:
            client.translate_many(["Hello"], "en")
        except Exception:  # noqa: BLE001
            raised = True
    assert raised, "HTTP 500 应抛出异常，供上层回退到本地引擎"


def test_malformed_response_raises():
    home = isolate_home()
    cfg = Config(home / "config.json")
    cfg.set("deepseek", "api_key", "sk-test")
    client = OpenAICompatClient(cfg)
    session = _Session(_Resp({"unexpected": True}))
    raised = False
    with patched(client, "_get_session", lambda: session):
        try:
            client.translate_many(["Hello"], "en")
        except Exception:  # noqa: BLE001
            raised = True
    assert raised, "响应结构异常应抛出异常，而不是静默返回空"
