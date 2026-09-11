"""离线翻译引擎（本地 NLLB via CTranslate2）+ 翻译缓存 + 词库替换。

设计：
- NLLB 模型为 CTranslate2 格式，见 tools/download_models.py；目录含
  model.bin / config.json / shared_vocabulary.json / sentencepiece.bpe.model / tokenizer 文件。
- 用官方推荐方式：HF AutoTokenizer 加载本地分词器（src_lang），
  ctranslate2.Translator + translate_batch(target_prefix=[tgt_lang]) 推理。
- 模型加载与推理都较慢，必须放在独立线程（TranslateWorker），不得阻塞 UI。
- 词库（人工词库 wordbook.txt）在译文上做后替换：适合强制游戏专有词/人名。

语言代码约定：本模块只认 NLLB 代码（eng_Latn / jpn_Jpan / zho_Hans），
UI 层传入 app 语言代码（en/ja）后先经 const.NLLB_SRC_CODE 转换。
"""
from __future__ import annotations

import json
import logging
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..const import (
    ENGINE_API,
    ENGINE_AUTO,
    ENGINE_LOCAL,
    ENGINE_LOCAL_FIRST,
    LANG_EN,
    NLLB_SRC_CODE,
    NLLB_TGT_CODE,
)

log = logging.getLogger(__name__)


# ---------- 长文本拆分 ----------
_SENT_SPLIT = re.compile(r"(?<=[。！？.!?;；,，])\s*")


def split_long_text(text: str, max_chars: int = 240) -> List[str]:
    """把超长文本按句子边界切成 ≤max_chars 的子块（NLLB 单句上限 512 token）。

    超长单句（无标点）按字符硬切。返回空输入为空列表。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: List[str] = []
    cur = ""
    for seg in (s.strip() for s in _SENT_SPLIT.split(text)):
        if not seg:
            continue
        while len(seg) > max_chars:  # 无标点超长段硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(seg[:max_chars])
            seg = seg[max_chars:]
        if not seg:
            continue
        if cur and len(cur) + len(seg) + 1 > max_chars:
            chunks.append(cur)
            cur = seg
        else:
            cur = f"{cur} {seg}" if cur else seg
    if cur:
        chunks.append(cur)
    return chunks


# ---------- 译文清洗 ----------
_PUNCT_FULL = {
    ",": "，", ".": "。", "!": "！", "?": "？",
    ";": "；", ":": "：", "(": "（", ")": "）", "~": "～",
}


def zh_punct(text: str) -> str:
    """把模型输出的半角标点转中文标点、清理汉字间多余空格。

    数字小数点（42.5）保留半角，不误转。
    """
    if not text:
        return text
    protected = re.sub(r"(?<=\d)\.(?=\d)", "\uE000", text)
    out = "".join(_PUNCT_FULL.get(ch, ch) for ch in protected)
    out = out.replace("\uE000", ".")
    # 去掉汉字/全角标点两侧的空格
    out = re.sub(r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+"
                 r"(?=[\u4e00-\u9fff\uff00-\uffef])", "", out)
    # 去掉全角标点前的空格
    out = re.sub(r"\s+([，。！？；：、])", r"\1", out)
    return out.strip()


# ---------- 词库（术语表） ----------
class Glossary:
    """『人工词库』加载与译后替换。

    词库文件每行一条：`原文|译文` 或 `原文=译文`，# 开头为注释。
    应用时机：译文生成后，把仍在译文中出现的原文词替换为指定译文
    （适合人名、游戏专有名词等模型译不好的词）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pairs: List[tuple[str, str]] = []
        self._mtime: float = -1.0
        self._lock = threading.Lock()
        self._load_if_changed()

    def _load_if_changed(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        pairs: List[tuple[str, str]] = []
        try:
            for raw in self._path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.split(r"[|=]", line, maxsplit=1)
                if len(m) == 2 and m[0].strip() and m[1].strip():
                    pairs.append((m[0].strip(), m[1].strip()))
        except Exception as exc:  # noqa: BLE001
            log.warning("词库解析失败 %s: %s", self._path, exc)
        with self._lock:
            # 按原文长度降序：先替换长条目，避免 "Sinner" 先把 "Sinners"
            # 吃掉一半变成「罪人s」
            self._pairs = sorted(pairs, key=lambda kv: len(kv[0]), reverse=True)
            self._mtime = mtime
        if pairs:
            log.info("词库已加载 %d 条: %s", len(pairs), self._path)

    def apply(self, text: str) -> str:
        """译后替换：把译文里仍残留的原文词换成指定译名。

        只对"模型保留了原文"的情况有效；若模型把专名音译错了
        （如 Dante → 丹蒂），这里替换不到，需要配合 pre_apply 使用。
        """
        return self._replace(text)

    def pre_apply(self, text: str) -> tuple:
        """翻译前置替换：把原文里的术语直接换成目标译名。

        为什么直接写译名而不是占位符：实测 NLLB 无法处理 `[[T0]]` 这类标记
        （会输出 <unk> 甚至空译文），只能用它能识别的目标语言词。
        直接前置译名后，多数专名（尤其人名）会被正确保留，
        明显优于让模型自行音译（Dante → 丹蒂）。
        """
        return self._replace(text), {}

    @staticmethod
    def restore(text: str, mapping: Dict[str, str]) -> str:
        """把译文中残留的占位符还原为译名（容忍模型插入的空格/括号变体）。"""
        if not text or not mapping:
            return text
        out = text
        for ph, dst in mapping.items():
            out = out.replace(ph, dst)
        # 容错：模型可能输出 "[ [T3] ]" "[[ T3 ]]" 等变形
        for ph, dst in mapping.items():
            core = ph.strip("[]")
            out = re.sub(
                rf"\[*\[*\s*{re.escape(core)}\s*\]*\]*", dst, out, count=1
            ) if core in out else out
        return out

    def _replace(self, text: str) -> str:
        self._load_if_changed()
        if not self._pairs or not text:
            return text
        with self._lock:
            pairs = self._pairs
        for src, dst in pairs:
            if src and dst and src in text:
                text = text.replace(src, dst)
        return text


# 汉字判定：用于识别"这到底是不是真译文"。
# 目标语言固定简体中文，因此真正的译文必然含汉字；若不含，基本可以断定是
# 引擎失败后透传的原文（曾导致这种"原文"被写进缓存，之后永远显示英文）。
_HAN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def looks_translated(text: str) -> bool:
    """译文是否像真正的中文译结果（含汉字）。"""
    return bool(_HAN_RE.search(text or ""))


# ---------- 翻译缓存（SQLite） ----------
class TranslationCache:
    """按 (source_lang, 原文) 缓存译文，避免相同文本重复跑模型。"""

    def __init__(self, db_path: Path) -> None:
        self._db = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS translations ("
                    "lang TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,"
                    "PRIMARY KEY (lang, src))"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("翻译缓存初始化失败: %s", exc)

    def get(self, lang: str, src: str) -> Optional[str]:
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute(
                    "SELECT dst FROM translations WHERE lang=? AND src=?",
                    (lang, src),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            log.debug("缓存读取失败: %s", exc)
            return None

    def put(self, lang: str, src: str, dst: str) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO translations(lang, src, dst) VALUES(?,?,?)",
                    (lang, src, dst),
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("缓存写入失败: %s", exc)


# ---------- 本地 NLLB 引擎 ----------
class LocalTranslator:
    """CTranslate2 NLLB 引擎（懒加载）。实例只允许在单个线程中使用。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._translator: Optional[object] = None
        self._tokenizers: Dict[str, object] = {}
        self._model_dir: Optional[Path] = None
        self._load_error: Optional[str] = None
        self._fail_ts = 0.0  # 失败冷却，避免模型缺失时每批重复尝试/刷错误
        self._device_used: Optional[str] = None  # 实际生效设备：cuda / cpu

    @property
    def device_used(self) -> Optional[str]:
        """实际生效的推理设备（加载后才有值）。"""
        return self._device_used

    @staticmethod
    def cuda_available() -> bool:
        """当前是否有可用的 CUDA 显卡。"""
        try:
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _resolve_device(want: str) -> str:
        """解析推理设备。

        - `cpu`：只有**显式选择**时才用 CPU（CPU 跑本地模型会打满所有核心，
          因此绝不隐式回退）。
        - `auto` / `cuda`：都要求用显卡；检测不到 CUDA 或加载失败时直接报错，
          由上层停止翻译，而不是偷偷改用 CPU。
        """
        if want == "cpu":
            return "cpu"
        return "cuda"

    # ---------- 模型定位 ----------
    def model_dir(self) -> Optional[Path]:
        variant = self.cfg.get("nllb", default={}).get("variant", "nllb-200-distilled-600M")
        custom = self.cfg.get("model_dir", default="")
        if custom:
            d = Path(custom).expanduser()
        else:
            d = self.cfg.models_dir() / variant
        # 允许用户把 CT2 文件直接放 model_dir 本身，或放其变体子目录
        if (d / "model.bin").exists():
            return d
        parent = self.cfg.models_dir()
        if (parent / "model.bin").exists():
            return parent
        return d if d.exists() else None

    def is_ready(self) -> bool:
        return self._translator is not None

    def load(self) -> bool:
        """加载 CT2 模型与分词器。失败时记录原因并返回 False。"""
        if self._translator is not None:
            return True
        if time.monotonic() - self._fail_ts < 10.0:
            return False  # 失败冷却期内不再重复尝试/刷错误
        d = self.model_dir()
        if d is None or not (d / "model.bin").exists():
            self._load_error = f"未找到模型文件（{d}）。请先运行 python -m gametrans.tools.download_models"
            self._fail_ts = time.monotonic()
            log.error("%s", self._load_error)
            return False
        try:
            _ensure_vocab_txt(d)
            import ctranslate2

            nllb = self.cfg.get("nllb", default={})
            want = str(nllb.get("device", "auto") or "auto").lower()
            device = self._resolve_device(want)
            compute_type = str(nllb.get("compute_type", "int8"))
            # 限制推理线程数：默认吃满所有核会卡整机，2 线程在速度/占用间平衡
            threads = max(1, min(8, int(nllb.get("threads", 2) or 2)))

            if device == "cuda":
                # 先确认真的有可用显卡；没有就直接失败（不回退 CPU）
                if not self.cuda_available():
                    self._load_error = (
                        "未检测到可用显卡（CUDA）。为避免占满 CPU，程序不会自动"
                        "改用 CPU 推理；如确实想用 CPU，请在设置→翻译引擎里手动"
                        "选择『仅 CPU』"
                    )
                    self._fail_ts = time.monotonic()
                    log.error("%s", self._load_error)
                    return False

            # GPU 首选 int8_float16（显存省、速度快），CPU 侧不支持该类型
            cands = ([compute_type, "int8_float16", "float16",
                      "int8_bfloat16", "int8", "float32"]
                     if device == "cuda"
                     else [compute_type, "int8", "int8_float32", "float32"])
            translator = None
            last_err: Optional[Exception] = None
            used_ct = ""
            seen: set = set()
            for ct in cands:
                if ct in seen:
                    continue
                seen.add(ct)
                try:
                    translator = ctranslate2.Translator(
                        str(d), device=device, compute_type=ct,
                        intra_threads=threads,
                    )
                    used_ct = ct
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    log.debug("device=%s compute_type=%s 加载失败: %s", device, ct, exc)
            if translator is None:
                # 不做 GPU→CPU 隐式回退（CPU 会打满核心），直接失败交由上层停止翻译
                tag = "显卡(CUDA)" if device == "cuda" else "CPU"
                self._load_error = (
                    f"{tag} 加载失败: {last_err}。若显卡不可用，可在设置→翻译引擎里"
                    f"手动选择『仅 CPU』（会占满 CPU 核心）"
                )
                self._fail_ts = time.monotonic()
                log.error("%s", self._load_error)
                return False
            self._translator = translator
            self._model_dir = d
            self._device_used = device
            log.info("NLLB 引擎已加载 device=%s compute_type=%s threads=%d",
                     device, used_ct, threads)
            if device == "cuda":
                self._warmup()  # GPU 首次推理要选算法，先热身避免首句卡顿
            return True
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"引擎初始化失败: {exc}"
            self._fail_ts = time.monotonic()
            log.exception("引擎初始化失败")
            return False

    def _warmup(self) -> None:
        """GPU 预热：提前跑一次极短推理。

        CUDA 上首次推理要选择 cuDNN 算法并建立上下文，实测比后续慢十倍以上；
        预热后用户遇到的第一句字幕就不会卡顿（失败不影响功能）。
        """
        try:
            tok = self._tokenizer(NLLB_SRC_CODE.get(LANG_EN, "eng_Latn"))
            tokens = tok.convert_ids_to_tokens(tok.encode("Hello."))[:16]
            t0 = time.monotonic()
            self._translator.translate_batch(
                [tokens],
                target_prefix=[[NLLB_TGT_CODE]],
                max_decoding_length=16,
                beam_size=1,
            )
            log.info("GPU 预热完成，用时 %.1fs", time.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001
            log.debug("GPU 预热失败（忽略）: %s", exc)

    def _tokenizer(self, src_lang: str):
        tok = self._tokenizers.get(src_lang)
        if tok is None:
            import transformers

            # NLLB 是 BPE 分词器，clean_up_tokenization_spaces 对其无效且会触发警告，
            # 显式置 False 关闭该后处理。
            tok = transformers.AutoTokenizer.from_pretrained(
                str(self._model_dir),
                src_lang=src_lang,
                clean_up_tokenization_spaces=False,
                local_files_only=True,
            )
            self._tokenizers[src_lang] = tok
        return tok

    def translate_many(self, texts: List[str], src_lang: str,
                       context: Optional[List[str]] = None) -> List[str]:
        """把一组句子从 src_lang 翻译成中文。长度不足/空的句子原样返回。

        超长句子自动按句子边界拆成子块批量翻译后拼接（NLLB 单句上限 512 token）。
        context 仅用于与 API 引擎接口一致；NLLB 逐句翻译，不使用上下文。
        """
        if not texts:
            return []
        if not self.load():
            return texts  # 引擎不可用：原样返回，不阻断管线
        nllb_src = NLLB_SRC_CODE.get(src_lang, NLLB_SRC_CODE.get(LANG_EN, "eng_Latn"))
        tok = self._tokenizer(nllb_src)

        cleaned = [t.strip() for t in texts]
        batch_tokens = []
        owner: List[int] = []  # 子块 -> 原句索引
        for i, text in enumerate(cleaned):
            if not text:
                continue
            for chunk in split_long_text(text, 240):
                tokens = tok.convert_ids_to_tokens(tok.encode(chunk))
                batch_tokens.append(tokens[:480])  # NLLB 训练上限 512；截断保护
                owner.append(i)
        if not batch_tokens:
            return cleaned

        try:
            results = self._translator.translate_batch(
                batch_tokens,
                target_prefix=[[NLLB_TGT_CODE]] * len(batch_tokens),
                max_decoding_length=512,
                beam_size=1,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("批量翻译失败: %s", exc)
            return cleaned

        parts: List[List[str]] = [[] for _ in texts]
        for i, res in zip(owner, results):
            try:
                hyp = res.hypotheses[0][1:]  # 去掉 target 前缀
                parts[i].append(tok.decode(tok.convert_tokens_to_ids(hyp)).strip())
            except Exception:  # noqa: BLE001
                parts[i].append(cleaned[i])

        out: List[str] = []
        for i in range(len(texts)):
            if not cleaned[i]:
                out.append("")
                continue
            if not parts[i]:
                out.append(cleaned[i])
                continue
            out.append(" ".join(p for p in parts[i] if p).strip())
        return out

    @property
    def last_error(self) -> Optional[str]:
        return self._load_error


def _ensure_vocab_txt(model_dir: Path) -> None:
    """把 shared_vocabulary.json 转成 CT2 需要的 shared_vocabulary.txt（按 id 排序）。"""
    txt = model_dir / "shared_vocabulary.txt"
    if txt.exists():
        return
    jpath = model_dir / "shared_vocabulary.json"
    if not jpath.exists():
        log.warning("模型目录缺 shared_vocabulary.json，CT2 可能需要词表")
        return
    try:
        data = json.loads(jpath.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data and all(
            isinstance(v, int) for v in data.values()
        ):
            ordered = [k for k, _ in sorted(data.items(), key=lambda kv: kv[1])]
            txt.write_text("\n".join(ordered), encoding="utf-8")
            log.info("已生成 shared_vocabulary.txt（%d tokens）", len(ordered))
    except Exception as exc:  # noqa: BLE001
        log.warning("生成词表文件失败: %s", exc)


# ---------- 引擎调度器 ----------
class EngineRouter:
    """按 cfg.engine 分发翻译。专注 API（默认）：不再静默回退本地。

    api ：仅 API（OpenAI 兼容）——失败/未配 Key 直接透传原文并告警，不调本地引擎。
    auto：API 失败时才回退本地（作为后备）。
    local：仅本地 NLLB（保留，供离线场景手动选用）。
    对外与 LocalTranslator 同接口 translate_many(texts, src_lang)。
    """

    # API 失败后的冷却秒数：断网/服务异常时不至于每批字幕都反复打超时
    API_FAIL_COOLDOWN_S = 20.0

    def __init__(self, cfg, on_warning: Optional[Callable[[str], None]] = None,
                 on_fatal: Optional[Callable[[str], None]] = None) -> None:
        self.cfg = cfg
        self._local = LocalTranslator(cfg)
        self._api = None
        self._api_fail_ts = 0.0
        self._on_warning = on_warning
        self._on_fatal = on_fatal  # 致命错误（如显卡不可用）→ 上层停止翻译
        self._fallback_notified = False  # 回退提示去重（避免每批刷提示）
        self._fatal_notified = False
        self.last_used: Optional[str] = None  # api / local / passthrough / fatal

    @property
    def local(self) -> LocalTranslator:
        return self._local

    @staticmethod
    def _engine_order(mode: str) -> List[str]:
        """按配置决定引擎尝试顺序。

        - local_first：本地优先，本地不可用才转 API（默认）
        - auto       ：API 优先，失败转本地
        - api        ：API 优先，失败转本地（与 auto 的差别：回退时明确提示）
        - local      ：仅本地；本地不可用即停止（绝不碰 CPU）
        """
        if mode == ENGINE_LOCAL_FIRST:
            return ["local", "api"]
        if mode == ENGINE_LOCAL:
            return ["local"]
        return ["api", "local"]

    def _get_api(self):
        if self._api is None:
            from .deepseek import DeepSeekClient  # 延迟导入
            self._api = DeepSeekClient(self.cfg)
        return self._api

    def translate_many(self, texts: List[str], src_lang: str,
                       context: Optional[List[str]] = None) -> List[str]:
        """按 cfg.engine 的顺序逐个尝试引擎。

        硬约束：**绝不把英文原文当结果**，也**绝不偷偷用 CPU**；
        所有候选引擎都不可用时直接通知上层停止翻译。
        """
        mode = str(self.cfg.get("engine", default=ENGINE_LOCAL_FIRST))
        order = self._engine_order(mode)
        errors: List[str] = []

        for name in order:
            if name == "local":
                if not self._local.load():
                    errors.append(
                        f"本地引擎不可用（{self._local.last_error or '未知原因'}）")
                    continue
                self.last_used = "local"
                if order[0] != "local":
                    # 首选是 API，降级用了本地
                    if mode == ENGINE_API:
                        self._notify_once(
                            f"API 不可用（{'；'.join(errors)}），已临时改用本地离线翻译")
                else:
                    self._fallback_notified = False  # 用上首选引擎，恢复提示能力
                return self._local.translate_many(texts, src_lang,
                                                  context=context)

            # ---- API ----
            api = self._get_api()
            if not api.ready:
                errors.append("API 未配置 Key")
                continue
            if (time.monotonic() - self._api_fail_ts) <= self.API_FAIL_COOLDOWN_S:
                errors.append("API 连续失败冷却中")
                continue
            try:
                dst = api.translate_many(texts, src_lang, context=context)
            except Exception as exc:  # noqa: BLE001
                self._api_fail_ts = time.monotonic()
                errors.append(f"API 失败（{str(exc)[:100]}）")
                log.warning("API 翻译失败(冷却 %ds): %s",
                            int(self.API_FAIL_COOLDOWN_S), exc)
                continue
            self.last_used = "api"
            if order[0] != "api":
                # 本地优先模式下转到 API：告知一次
                self._notify_once(
                    f"本地翻译不可用（{'；'.join(errors)}），已改用 API 翻译")
            else:
                self._fallback_notified = False
            return dst

        # 所有候选引擎都不可用 → 停止翻译（不透传、不用 CPU）
        self._notify_fatal("翻译已停止：" + "；".join(errors))
        self.last_used = "fatal"
        return list(texts)

    def _notify_once(self, msg: str) -> None:
        """同类提示只弹一次，避免每批字幕都刷屏（成功后会自动复位）。"""
        if self._fallback_notified:
            return
        self._fallback_notified = True
        self._notify_warning(msg)

    def _notify_warning(self, msg: str) -> None:
        try:
            if self._on_warning:
                self._on_warning(msg)
        except Exception:  # noqa: BLE001
            pass

    def _notify_fatal(self, msg: str) -> None:
        """致命错误只通知一次（避免每批字幕重复弹提示）。"""
        if self._fatal_notified:
            return
        self._fatal_notified = True
        log.error("%s", msg)
        try:
            if self._on_fatal:
                self._on_fatal(msg)
        except Exception:  # noqa: BLE001
            pass


# ---------- 高层入口 ----------
def translate_lines(cfg, lines, src_lang: str,
                    translator: Optional[LocalTranslator] = None,
                    cache: Optional[TranslationCache] = None,
                    glossary: Optional[Glossary] = None,
                    context: Optional[List[str]] = None) -> List[str]:
    """翻译一批 OCR 行文本，返回与输入等长的译文列表。

    - 命中缓存直接返回；未命中跑模型并写缓存。
    - 词库在译文上做后替换。
    - context：最近几句原文，交给 API 引擎保持人名/术语一致（本地引擎忽略）。
    - 高频调用方（TranslateWorker）请复用同一 translator/cache/glossary 实例，
      避免每批重复创建对象与 SQLite 连接。
    """
    if not lines:
        return []
    use_cache = bool(cfg.get("auto_cache", default=True))
    if cache is None and use_cache:
        cache = TranslationCache(cfg.cache_db())
    eng = translator or LocalTranslator(cfg)
    if glossary is None:
        glossary = Glossary(cfg.glossary_file())

    src_texts = [ln.text for ln in lines]
    out = [""] * len(src_texts)
    todo: List[int] = []
    for i, text in enumerate(src_texts):
        text = (text or "").strip()
        if not text:
            continue
        if use_cache:
            hit = cache.get(src_lang, text)
            # 只认含汉字的结果：旧版本可能把失败透传的原文误写入缓存，
            # 这里直接忽略这类脏数据并重新翻译（自愈，无需手动清库）。
            if hit is not None and looks_translated(hit):
                out[i] = hit  # 缓存即最终译文（已含词库替换与标点清洗）
                continue
        todo.append(i)

    if todo:
        need_map: List[int] = [i for i in todo if len(src_texts[i]) >= 1]
        feeds: List[str] = []
        maps: List[Dict[str, str]] = []
        for i in need_map:
            # 术语前置：原文里的专名先换成占位符，避免模型自行音译出错
            feed, mapping = glossary.pre_apply(src_texts[i])
            feeds.append(feed)
            maps.append(mapping)
        if feeds:
            t0 = time.monotonic()
            translated = eng.translate_many(feeds, src_lang, context=context)
            dt = time.monotonic() - t0
            if dt > 0.3:
                log.info("翻译 %d 行耗时 %.1fs", len(feeds), dt)
            for i, dst, mapping in zip(need_map, translated, maps):
                raw = (dst or "").strip() or src_texts[i]
                raw = glossary.restore(raw, mapping)   # 占位符 → 正确译名
                final = zh_punct(glossary.apply(raw))  # 兜底：残留原文再替换
                # 目标语言固定中文：不含汉字的结果一律视为无效——
                # 典型来源：① 引擎失败后透传的原文（用英文冒充译文）
                #          ② 本地模型对噪点文本输出的 <unk>
                # 这类结果既不显示（避免覆盖原文/显示乱码）也不写缓存。
                if not looks_translated(final):
                    final = ""
                out[i] = final
                if use_cache and final:
                    cache.put(src_lang, src_texts[i], final)
    return out


# ---------- 翻译线程 ----------
class TranslateWorker:
    """独立线程翻译：UI 把 OCR 行丢进来，完成后回调译文。

    - 队列只保留最新一批：翻译慢时丢弃旧批次（字幕场景足够）。
    - 相同原文去重：同一句字幕稳定出现时不重复翻译。
    """

    def __init__(self, cfg, on_done: Callable[[List, List[str], str], None],
                 on_status: Optional[Callable[[str], None]] = None,
                 on_fatal: Optional[Callable[[str], None]] = None) -> None:
        self.cfg = cfg
        self._on_done = on_done
        self._on_status = on_status
        self._on_fatal = on_fatal
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_key: Optional[str] = None
        self._router: Optional[EngineRouter] = None
        # 共享组件只建一次：SQLite/词库/引擎跨批次复用，降低每批开销
        self._cache: Optional[TranslationCache] = None
        self._glossary: Optional[Glossary] = None
        # 最近几句已翻原文：交给 API 引擎做上下文，保证人名/术语前后一致
        self._recent: List[str] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="translate", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=2.0)
        self._thread = None

    def submit(self, lines, src_lang: str) -> None:
        """提交一批 OCR 行。行对象需含 .text（如 OCRResult）。"""
        if not self._thread or not self._thread.is_alive():
            return
        if not lines:
            return
        key = src_lang + "|" + "|".join(ln.text.strip() for ln in lines)
        if key == self._last_key:
            return  # 与上一批完全相同的文本，不重复翻译
        self._last_key = key
        try:
            self._queue.put_nowait((lines, src_lang, key))
        except queue.Full:
            pass  # 上一批还在翻译，丢弃本批

    def reset(self) -> None:
        """语言切换/区域变化后调用：清除去重键与上下文（跨语言上下文会误导模型）。"""
        self._last_key = None
        self._recent = []

    def reset_engine(self) -> None:
        """设置里换了模型规格/推理设备后调用：丢弃引擎实例重新加载。

        只把引用置空（原子操作）；旧引擎仍被正在进行的翻译持有，
        不会被中途销毁，下一批字幕才会按新配置重建。
        """
        self._router = None
        self._last_key = None

    def _status(self, msg: str) -> None:
        """引擎告警（翻译线程内调用，上层通常经 Qt 信号转发）。"""
        try:
            if self._on_status:
                self._on_status(msg)
        except Exception:  # noqa: BLE001
            pass

    def _fatal(self, msg: str) -> None:
        """引擎致命错误：上层据此停止翻译（不做任何降级）。"""
        try:
            if self._on_fatal:
                self._on_fatal(msg)
        except Exception:  # noqa: BLE001
            pass

    def _loop(self) -> None:
        # 引擎放本线程懒加载（首次翻译较慢，不阻塞启动）
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            lines, src_lang, _key = item
            try:
                if self._router is None:
                    self._router = EngineRouter(self.cfg, on_warning=self._status,
                                                on_fatal=self._fatal)
                    self._cache = TranslationCache(self.cfg.cache_db())
                    self._glossary = Glossary(self.cfg.glossary_file())
                    # 预热本地引擎（GPU 加载 + 预热耗时较长，提前做掉）。
                    # 失败不在这里报错：等 translate_many 判定是否致命，
                    # 以便 API 模式仍能正常工作。
                    self._router.local.load()
                # 先把同一物理行被 OCR 断开的片段拼回整行，再整体翻译/显示
                from .ocr import merge_row_lines
                merged = merge_row_lines(list(lines))
                n_ctx = int(self.cfg.get("context_pieces", default=3) or 0)
                ctx = self._recent[-n_ctx:] if n_ctx > 0 else None
                dst = translate_lines(self.cfg, merged, src_lang,
                                      translator=self._router,
                                      cache=self._cache,
                                      glossary=self._glossary,
                                      context=ctx)
                if n_ctx > 0:
                    # 原文入上下文（去重后追加，保持最近 N 句）
                    for ln in merged:
                        t = (getattr(ln, "text", "") or "").strip()
                        if t and (not self._recent or self._recent[-1] != t):
                            self._recent.append(t)
                    del self._recent[:-n_ctx]
                try:
                    self._on_done(merged, dst, src_lang)
                except Exception:  # noqa: BLE001
                    log.exception("翻译回调异常")
            except Exception:  # noqa: BLE001
                log.exception("翻译线程异常")


# ---------- 便捷诊断 ----------
def resolve_nllb_model_path(cfg) -> Optional[Path]:
    return LocalTranslator(cfg).model_dir()
