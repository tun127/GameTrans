"""配置读写：全部配置持久化到 %APPDATA%/GameTrans/config.json。"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .const import (
    APP_NAME,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_URL,
    DEFAULT_HOTKEY,
    ENGINE_LOCAL_FIRST,
    LANG_EN,
    MODE_OVERLAY,
    SPEED_PRESETS,
)


# 默认数据根目录：模型约 1.2GB，默认不放系统盘（C 盘）。
# 想换位置就设环境变量 GAMETRANS_HOME（例如 D:\\MyData\\GameTrans）。
DEFAULT_DATA_ROOT = "D:\\GameTrans"

_DATA_DIR_CACHE: Optional[Path] = None


def app_data_dir() -> Path:
    """程序数据目录（配置/模型/缓存/词库/日志都在这）。

    优先级：环境变量 GAMETRANS_HOME > D:\\GameTrans > %APPDATA%\\GameTrans。
    D 盘不可用（没有 D 盘或不可写）时自动回退 AppData，保证程序仍能启动。
    目录只解析一次并缓存，避免运行中因磁盘变化导致路径漂移。
    """
    global _DATA_DIR_CACHE
    if _DATA_DIR_CACHE is not None:
        return _DATA_DIR_CACHE

    env = os.environ.get("GAMETRANS_HOME", "").strip()
    if env:
        d: Path = Path(env).expanduser()
    else:
        d = Path(DEFAULT_DATA_ROOT)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 没有 D 盘/不可写：回退系统盘标准位置
            base = os.environ.get("APPDATA")
            d = Path(base) / APP_NAME if base else Path.home() / ".config" / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    _DATA_DIR_CACHE = d
    return d


def reset_data_dir_cache() -> None:
    """仅测试/迁移用：清空路径缓存，下次重新解析。"""
    global _DATA_DIR_CACHE
    _DATA_DIR_CACHE = None


_DEFAULTS: Dict[str, Any] = {
    # 目标窗口锚定
    "anchor": {
        # anchor=="window" 时按 title_contains 匹配游戏窗口；anchor=="screen" 时用整个主屏
        "type": "screen",
        "title_contains": "",
        "class_name": "",
        "monitor_index": 0,
    },
    # 识别区域。anchor=screen: 物理像素绝对值；anchor=window: 相对窗口客户区 0..1
    "region": None,  # {"x":…, "y":…, "w":…, "h":…}
    "source_lang": LANG_EN,
    # 默认：本地优先（GPU 快、免费、离线），本地不可用才转 API
    "engine": ENGINE_LOCAL_FIRST,
    "deepseek": {
        "api_key": "",
        "base_url": DEEPSEEK_DEFAULT_URL,
        "model": DEEPSEEK_DEFAULT_MODEL,
        "timeout_s": 15,
    },
    "model_dir": "",
    "ocr": {
        "backend": "rapidocr",
        # 识别速度/精度档位：fast 最快(检测边 640) / balanced 均衡(960) / high 精细(1280)
        "speed": "fast",
        # ONNX Runtime 线程数。不限制时它会用满全部逻辑核心（实测 20 核机器
        # 单次 OCR 占 ≈18 核、玩游戏的 CPU 被吃光）。实测（识别档=fast）：
        #   1 线程 → 0.77s / 0.95 核（默认，最省 CPU，延迟已够用）
        #   2 线程 → 0.64s / 3.35 核
        #   4 线程 → 0.20s / 10.2 核
        #   8 线程 → 0.39s / 15.9 核
        "threads": 1,
        # 高级：手动指定检测/识别 ONNX 与字典（留空用内置默认模型）
        "custom_det_model": "",
        "custom_rec_model": "",
        "custom_rec_keys": "",
    },
    "nllb": {
        "variant": "nllb-200-distilled-600M",
        # auto / cuda：使用显卡（程序**不会**自动改用 CPU —— CPU 跑本地模型会打满
        #             核心；显卡不可用时直接停止翻译并提示用户）
        # cpu：只有显式选择才用 CPU
        "device": "auto",
        # 首选类型：GPU 上会自动改试 int8_float16 / float16；CPU 上 int8 最快
        "compute_type": "int8",
        "threads": 2,  # 推理线程数：1 最省 CPU，越大越快但越占核
    },
    "display": {
        "mode": MODE_OVERLAY,
        # overlay 模式样式
        "font_family": "",  # 空=微软雅黑；可填黑体/宋体/思源黑体/楷体等
        "font_size": 19,
        "text_color": "#FFFFFF",
        "bg_color": "#1A1A1A",
        "bg_alpha": 180,
        "corner_radius": 12,
        "padding": 8,
        "hide_source": True,  # 译文覆盖在原文字上（隐藏原文）
        "max_lines": 6,
        # side 模式样式
        "side_width": 460,
        "side_font_size": 16,
        "side_opacity": 0.94,
        "side_pos": "auto",  # auto 自动靠右 / 自定义由 Runtime 记住屏幕位置
    },
    "hotkey": DEFAULT_HOTKEY,
    "hotkey_enabled": True,
    "glossary_path": "",  # 空则使用 AppData/wordbook.txt
    "auto_cache": True,
    # 界面缩放：控制条高度/字号/间距与侧边小窗字号都按此倍率放大。
    # 1.0=紧凑 / 1.25=推荐（看清又不挡视线）/ 1.5=大字号。设置里可即时切换。
    "ui_scale": 1.25,
    # 提示条位置：固定到屏幕角落，避免挡在游戏画面中央。
    # top-right 右上（默认）/ top-left 左上 / bottom-right 右下 /
    # bottom-left 左下 / follow 跟随控制条（控制条被拖到中间时会跟着挡画面）
    "toast_pos": "top-right",
    "capture_interval_ms": SPEED_PRESETS["fast"]["capture_interval_ms"],
    "debounce_ms": SPEED_PRESETS["fast"]["debounce_ms"],
    "stable_ms": SPEED_PRESETS["fast"]["stable_ms"],
    "speed_preset": "fast",  # fast / balanced / saver
    "ocr_min_conf": 0.45,
    # 两次 OCR 的最小间隔（毫秒）。单次识别约 0.5~1s（已限线程），
    # 间隔过小会让 OCR 线程连轴转、CPU 长期高占用；600ms 兼顾响应与占用。
    "ocr_interval_ms": 600,
    # 缩略图平均绝对差阈值，超过才触发 OCR。
    # 实测字幕翻页在缩略图指标下 diff≈0.038~0.055，0.03 保证翻页必触发、
    # 同时过滤画面微抖（不要设回 0.5+，会把字幕变化全挡掉）。
    "frame_diff_threshold": 0.03,
    "check_fullscreen": True,  # 检测真·独占全屏并提示
    # 只翻译选定的游戏窗口：目标窗口不在前台（切到浏览器/桌面）时暂停翻译。
    # 仅对"窗口锚定"生效；整屏锚定下该开关无意义。
    "only_foreground": True,
    "min_line_len": 2,  # 少于该长度的行不翻译（过滤单字符噪点）
    # 稳定判定：画面变化后等它稳定这么久再送 OCR，避免读到滚动/动画的中间态。
    # 0=关闭（变化即送）；最长等待 = max(800ms, 3×settle)，保证快速翻页也不漏。
    "settle_ms": 300,
    # 忽略短语：命中即整行丢弃（逗号/分号/换行分隔，不分大小写）。
    # 用于屏蔽屏幕上的固定 UI 文字、水印、监控悬浮窗等噪声。
    "ignore_phrases": "",
    # 上下文增强（对 API 引擎生效）：带上最近几句已翻原文，并告诉模型内容背景，
    # 让人名/术语前后一致。本地 NLLB 逐句翻译，不受影响。
    "game_info": "",
    "context_pieces": 3,
}


class Config:
    """线程安全的 JSON 配置读写。"""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path or (app_data_dir() / "config.json")
        self._data: Dict[str, Any] = self._load()

    # ---------- 基础 ----------
    def _load(self) -> Dict[str, Any]:
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    return self._deep_merge(copy.deepcopy(_DEFAULTS), loaded)
            except Exception:
                pass
        return copy.deepcopy(_DEFAULTS)

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    def save(self) -> None:
        with self._lock:
            try:
                self._path.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass

    def get(self, *path: str, default: Any = None) -> Any:
        # 注意：path 只接受字符串键，默认值请用关键字 default= 传入。
        with self._lock:
            node: Any = self._data
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            return copy.deepcopy(node)

    def set(self, *path_value) -> None:
        """set('a','b', value) 设置嵌套字段。"""
        with self._lock:
            *path, value = path_value
            node = self._data
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = value
        self.save()

    def update_dict(self, mapping: Dict) -> None:
        with self._lock:
            merged = self._deep_merge(self._data, mapping)
            self._data = merged
        self.save()

    def reset(self) -> None:
        with self._lock:
            self._data = copy.deepcopy(_DEFAULTS)
        self.save()

    def as_dict(self) -> Dict:
        with self._lock:
            return copy.deepcopy(self._data)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data_dir(self) -> Path:
        return self._path.parent

    # ---------- 便捷路径 ----------
    def models_dir(self) -> Path:
        custom = self.get("model_dir")
        if custom:
            return Path(custom).expanduser()
        d = self.data_dir / "models"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def glossary_file(self) -> Path:
        custom = self.get("glossary_path")
        if custom:
            p = Path(custom).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        p = self.data_dir / "wordbook.txt"
        if not p.exists():
            p.write_text("# 人工词库：每条一行「原文|译文」或「原文=译文」\n", encoding="utf-8")
        return p

    def cache_db(self) -> Path:
        return self.data_dir / "cache.sqlite"

    def logs_dir(self) -> Path:
        d = self.data_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 运行参数 ----------
    def runtime_params(self) -> Dict[str, int]:
        """实际生效的捕获/去抖参数（跟随速度预设与自定义覆盖）。"""
        interval = int(self.get("capture_interval_ms", default=33))
        debounce = int(self.get("debounce_ms", default=60))
        stable = int(self.get("stable_ms", default=120))
        return {"interval": interval, "debounce": debounce, "stable": stable}

    def apply_preset(self, name: str) -> None:
        preset = SPEED_PRESETS.get(name)
        if not preset:
            return
        with self._lock:
            self._data["speed_preset"] = name
            self._data.update(preset)
        self.save()
