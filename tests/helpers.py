"""测试公共工具：隔离数据目录、临时配置、Qt 应用（offscreen 不弹窗）。

设计原则：
- 只碰临时目录，绝不读写用户的真实配置/缓存/词库；
- Qt 全程 offscreen，测试不会在屏幕上弹出任何窗口；
- 不依赖 pytest 等第三方库，`python tests/run_all.py` 即可运行。
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, List

from gametrans.config import Config, reset_data_dir_cache

_APP = None


def isolate_home() -> Path:
    """把程序数据根目录指向临时目录（避免污染真实配置）。"""
    d = Path(tempfile.mkdtemp(prefix="gt_test_"))
    os.environ["GAMETRANS_HOME"] = str(d)
    reset_data_dir_cache()
    return d


def tmp_cfg(path: Path | None = None) -> Config:
    """一份全新的临时配置（等价于首次安装）。"""
    if path is None:
        path = Path(tempfile.mkdtemp(prefix="gt_cfg_")) / "config.json"
    return Config(path)


def write_old_config(data: dict) -> Config:
    """写入一份"旧版本"配置（含任意你指定的键），用于验证向后兼容。"""
    import json
    d = Path(tempfile.mkdtemp(prefix="gt_old_"))
    (d / "config.json").write_text(json.dumps(data, ensure_ascii=False),
                                   encoding="utf-8")
    return Config(d / "config.json")


def qt_app():
    """获取（必要时创建）QApplication；平台为 offscreen，不显示窗口。"""
    global _APP
    if _APP is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        _APP = QApplication.instance() or QApplication([])
    return _APP


def fake_lines(texts: Iterable[str], conf: float = 0.95) -> List[Any]:
    """构造 OCRResult 列表（带一个占满区域的假坐标框）。"""
    from gametrans.core.ocr import OCRResult
    return [OCRResult(t, conf, [0.0, 0.0, 1.0, 1.0]) for t in texts]


@contextlib.contextmanager
def patched(obj: Any, name: str, value: Any):
    """临时替换对象属性（比 monkeypatch 更轻，避免额外依赖）。"""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield value
    finally:
        setattr(obj, name, old)


def make_glossary_text(pairs: Iterable[tuple]) -> str:
    """生成词库文件内容（支持 | 与 = 两种分隔）。"""
    return "".join(f"{a}|{b}\n" for a, b in pairs)
