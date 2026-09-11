"""OpenAI 兼容 API 翻译客户端（DeepSeek / OpenAI / Kimi / 通义 / Ollama…）。

统一走 OpenAI 兼容 chat/completions：只要在设置里填好 base_url、model、api_key，
即可接入任何兼容服务（本地 Ollama 通常为 http://127.0.0.1:11434/v1）。

与 LocalTranslator 使用同一接口 translate_many(texts, src_lang) -> List[str]，
便于上层（引擎调度器）无缝切换与回退。所有失败都以异常抛出，由调度层决定
回退本地还是透传原文。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from ..const import DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_DEFAULT_URL, LANG_AUTO

log = logging.getLogger(__name__)

LANG_NAMES = {
    "en": "英文",
    "ja": "日文",
    LANG_AUTO: "自动判断的原文",
}
SYSTEM_PROMPT = (
    "你是一名专业游戏字幕本地化翻译。把用户给出的文本翻译成简体中文，"
    "要求：1) 译文口语自然、贴合游戏字幕语气；2) 专有名词/人名保持可读译法；"
    "3) 只输出译文本身，不解释、不加引号。"
)

_SERVICE_HINTS = [
    ("deepseek", "DeepSeek"),
    ("openai", "OpenAI"),
    ("moonshot", "Kimi"),
    ("kimi", "Kimi"),
    ("dashscope", "通义"),
    ("aliyun", "通义"),
    ("bigmodel", "智谱"),
    ("zhipu", "智谱"),
    ("siliconflow", "硅基流动"),
    ("ollama", "Ollama"),
    ("localhost", "本地服务"),
    ("127.0.0.1", "本地服务"),
]


def guess_service_name(base_url: str) -> str:
    """根据 base_url 猜一个便于提示的服务名，猜不到就返回主机名。"""
    host = (base_url or "").split("/")[2] if "//" in (base_url or "") else base_url
    host = (host or "").lower()
    for frag, name in _SERVICE_HINTS:
        if frag in host:
            return name
    return host or "API"


class OpenAICompatClient:
    """OpenAI 兼容 API 客户端（base_url/model/api_key 均可配置）。仅在配置 key 时可用。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._session = None

    # 全部配置动态读取：运行中在设置里改 Key/模型/地址，下一批请求即生效
    @property
    def service(self) -> str:
        return guess_service_name(self.base_url)

    @property
    def api_key(self) -> str:
        return str(self.cfg.get("deepseek", default={}).get("api_key", "") or "").strip()

    @property
    def base_url(self) -> str:
        ds = self.cfg.get("deepseek", default={})
        return str(ds.get("base_url", DEEPSEEK_DEFAULT_URL) or DEEPSEEK_DEFAULT_URL).rstrip("/")

    @property
    def model(self) -> str:
        ds = self.cfg.get("deepseek", default={})
        return str(ds.get("model", DEEPSEEK_DEFAULT_MODEL) or DEEPSEEK_DEFAULT_MODEL)

    @property
    def timeout(self) -> int:
        return int(self.cfg.get("deepseek", default={}).get("timeout_s", 15))

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _get_session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def translate_many(self, texts: List[str], src_lang: str,
                       context: Optional[List[str]] = None) -> List[str]:
        """翻译一组字幕文本。失败抛 RuntimeError。

        context：最近几句已翻原文（仅作参考，不参与翻译输出），
        用于让模型保持人名/术语前后一致。
        """
        if not texts:
            return []
        if not self.ready:
            raise RuntimeError("未配置 API Key")
        cleaned = [t.strip() for t in texts]
        # 空行直接返回空，不发给模型
        indices = [i for i, t in enumerate(cleaned) if t]
        payload_texts = [cleaned[i] for i in indices]
        if not payload_texts:
            return cleaned

        lang_hint = LANG_NAMES.get(src_lang, LANG_NAMES["en"])
        parts = [
            f"源语言：{lang_hint}。请把下面 JSON 数组中的每条字符串逐条翻译为简体中文，"
            f"并只输出一个长度相同的中文 JSON 字符串数组（不要输出任何解释或代码块标记）："
        ]
        # 上文只作参考，明确要求不翻译、不输出，避免污染结果数组长度
        if context:
            parts.append(
                "\n【最近几句上文（仅供保持人名/术语与语气一致，不要翻译、不要输出）】：\n"
                + "\n".join(f"- {c}" for c in context[-5:])
            )
        parts.append("\n" + json.dumps(payload_texts, ensure_ascii=False))
        user = "".join(parts)

        # 内容背景（游戏名/题材）让模型选对译法：术语、人名、语气更贴合
        system = SYSTEM_PROMPT
        game_info = str(self.cfg.get("game_info", default="") or "").strip()
        if game_info:
            system += f"\n本次内容背景：{game_info}。请据此选择贴合的译法与语气。"

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            # 按输入长度估算输出上限（中文约 1 字≈1 token），避免长批次被截断，
            # 截断会导致 JSON 不完整、整批解析失败。上限 8192。
            "max_tokens": max(2048, min(8192, sum(len(t) for t in payload_texts) * 2)),
            "stream": False,
        }
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            r = self._get_session().post(url, json=body, headers=headers,
                                         timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{self.service} 请求失败: {exc}") from exc
        if r.status_code != 200:
            raise RuntimeError(
                f"{self.service} HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{self.service} 返回格式异常: {exc}") from exc

        translated = self._parse(content, len(payload_texts), payload_texts)
        out: List[str] = [""] * len(cleaned)
        for j, i in enumerate(indices):
            out[i] = translated[j]
        return out

    # ---------- 解析 ----------
    @staticmethod
    def _parse(content: str, n: int, fallback: List[str]) -> List[str]:
        """解析模型返回，尽量对齐成 n 条译文。

        模型偶尔会偏离格式（少一条、多一条、包在代码块里、返回对象、带编号
        列表）。这里做宽容对齐：条数多了截断、少了用原文补齐，避免"偶发
        漂移导致整批失败并显示英文原文"。
        """
        text = (content or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)

        def fit(items: List) -> Optional[List[str]]:
            out = [str(x).strip() for x in items]
            if not out:
                return None
            if len(out) > n:
                out = out[:n]
            elif len(out) < n:
                out += [fallback[i] for i in range(len(out), n)]
            return out

        # 1) 标准 JSON（数组 或 {"1": "…"} 形式对象）
        try:
            data = json.loads(text)
            if isinstance(data, list):
                done = fit(data)
                if done:
                    return done
            elif isinstance(data, dict):
                vals = [v for _k, v in sorted(data.items(),
                                               key=lambda kv: str(kv[0]))]
                done = fit(vals)
                if done:
                    return done
        except Exception:  # noqa: BLE001
            pass
        # 2) 文本里夹带解释时，提取第一段 JSON 数组
        m = re.search(r"\[.*\]", text, flags=re.S)
        if m:
            try:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    done = fit(arr)
                    if done:
                        return done
            except Exception:  # noqa: BLE001
                pass
        # 3) 单条：直接采用全文
        if n == 1:
            return [text or fallback[0]]
        # 4) 兜底：按行切（模型逐行输出、带编号列表等）
        lines = [ln.strip(" \t-*•0123456789.、)）") for ln in text.splitlines()
                 if ln.strip()]
        done = fit(lines)
        if done:
            return done
        # 5) 完全无法解析：抛错交给上层（会回退本地引擎，而不是显示英文原文）
        raise RuntimeError("无法解析模型返回（内容为空或格式不符）")


# 兼容别名：旧代码/历史配置仍可引用 DeepSeekClient
DeepSeekClient = OpenAICompatClient
