"""配置模块测试：默认值、向后兼容、持久化、路径、速度预设。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from gametrans.config import Config, app_data_dir
from tests.helpers import isolate_home, tmp_cfg, write_old_config


def test_defaults_are_sane():
    """关键默认值（改动会影响功耗/体验，锁定住）。"""
    cfg = tmp_cfg()
    assert cfg.get("engine") == "local_first"
    assert cfg.get("source_lang") == "en"
    assert cfg.get("ui_scale") == 1.25
    assert cfg.get("toast_pos") == "top-right"  # 提示条固定在屏幕角落
    assert cfg.get("min_line_len") == 2
    assert cfg.get("settle_ms") == 300
    assert cfg.get("context_pieces") == 3
    assert cfg.get("check_fullscreen") is True
    assert cfg.get("only_foreground") is True
    assert cfg.get("auto_cache") is True


def test_ocr_defaults_are_cpu_friendly():
    """OCR 默认必须限制线程数：不限线程会吃满全部核心（曾实测 20 核 ≈18 核）。"""
    cfg = tmp_cfg()
    ocr = cfg.get("ocr", default={})
    assert ocr.get("speed") == "fast"
    threads = int(ocr.get("threads"))
    assert 1 <= threads <= 4, f"OCR 默认线程数不合理: {threads}"
    assert int(cfg.get("ocr_interval_ms")) >= 300
    assert 0.0 < float(cfg.get("frame_diff_threshold")) <= 0.1


def test_nllb_defaults():
    cfg = tmp_cfg()
    nllb = cfg.get("nllb", default={})
    assert nllb.get("device") in ("auto", "cuda", "cpu")
    assert 1 <= int(nllb.get("threads", 0)) <= 8


def test_set_get_roundtrip_persists():
    cfg = tmp_cfg()
    cfg.set("display", "font_size", 26)
    cfg.set("deepseek", "model", "my-model")
    assert cfg.get("display", default={})["font_size"] == 26
    assert cfg.get("deepseek", default={})["model"] == "my-model"

    again = Config(cfg.path)
    assert again.get("display", default={})["font_size"] == 26
    assert again.get("deepseek", default={})["model"] == "my-model"


def test_get_missing_returns_default():
    cfg = tmp_cfg()
    assert cfg.get("not", "exist", default="dft") == "dft"
    assert cfg.get("display", "nope", default=1) == 1


def test_old_config_keeps_user_values_and_fills_new_keys():
    """旧版本配置：用户显式改过的值必须保留，新键自动补默认。"""
    cfg = write_old_config({
        "engine": "api",
        "ocr": {"speed": "balanced"},       # 旧配置没有 threads
        "display": {"font_size": 22},
        "ignore_phrases": "Dify,超频",
    })
    assert cfg.get("engine") == "api"
    assert cfg.get("ocr", default={})["speed"] == "balanced"  # 不越权改用户选择
    assert cfg.get("display", default={})["font_size"] == 22
    assert cfg.get("ignore_phrases") == "Dify,超频"
    # 新增键补默认
    assert cfg.get("ocr", default={})["threads"] == 1
    assert cfg.get("ocr_interval_ms") == 600
    assert cfg.get("toast_pos") == "top-right"


def test_corrupt_config_falls_back_to_defaults():
    d = Path(tempfile.mkdtemp(prefix="gt_bad_"))
    (d / "config.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
    cfg = Config(d / "config.json")
    assert cfg.get("engine") == "local_first"  # 不抛异常，回退默认


def test_non_dict_config_falls_back():
    d = Path(tempfile.mkdtemp(prefix="gt_bad2_"))
    (d / "config.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    cfg = Config(d / "config.json")
    assert cfg.get("engine") == "local_first"


def test_paths_live_under_data_dir():
    cfg = tmp_cfg()
    d = cfg.data_dir
    assert cfg.models_dir() == d / "models"
    assert cfg.glossary_file().parent == d
    assert cfg.cache_db().parent == d
    assert cfg.logs_dir() == d / "logs"
    assert cfg.logs_dir().exists()
    assert cfg.glossary_file().exists()  # 首次自动创建（含说明注释）
    assert "词库" in cfg.glossary_file().read_text(encoding="utf-8")


def test_custom_model_dir_wins():
    cfg = tmp_cfg()
    custom = Path(tempfile.mkdtemp(prefix="gt_model_"))
    cfg.set("model_dir", str(custom))
    assert cfg.models_dir() == custom


def test_apply_preset():
    cfg = tmp_cfg()
    cfg.apply_preset("saver")
    assert cfg.get("speed_preset") == "saver"
    rp = cfg.runtime_params()
    assert rp["interval"] == 150 and rp["debounce"] == 250 and rp["stable"] == 500
    cfg.apply_preset("不存在的预设")
    assert cfg.get("speed_preset") == "saver"  # 非法预设不改动


def test_runtime_params_shape():
    cfg = tmp_cfg()
    rp = cfg.runtime_params()
    assert set(rp) == {"interval", "debounce", "stable"}
    assert all(isinstance(v, int) and v > 0 for v in rp.values())


def test_reset_restores_defaults():
    cfg = tmp_cfg()
    cfg.set("ui_scale", 1.5)
    cfg.set("ocr", "threads", 8)
    cfg.reset()
    assert cfg.get("ui_scale") == 1.25
    assert cfg.get("ocr", default={})["threads"] == 1


def test_update_dict_merges():
    cfg = tmp_cfg()
    cfg.update_dict({"display": {"font_size": 30}})
    assert cfg.get("display", default={})["font_size"] == 30
    assert cfg.get("display", default={})["mode"] == "overlay"  # 其它键保留


def test_app_data_dir_uses_env_override():
    home = isolate_home()
    assert app_data_dir() == home
    assert (home / "config.json").parent == home


def test_gametrans_home_fallback_when_env_cleared():
    """没有 GAMETRANS_HOME 时应回退到默认根目录或 AppData，且目录可用。"""
    import os

    from gametrans.config import DEFAULT_DATA_ROOT, reset_data_dir_cache
    old = os.environ.pop("GAMETRANS_HOME", None)
    try:
        reset_data_dir_cache()
        d = app_data_dir()
        assert d.exists()
        assert str(d).startswith(str(Path(DEFAULT_DATA_ROOT))) or "GameTrans" in str(d)
    finally:
        if old is not None:
            os.environ["GAMETRANS_HOME"] = old
        reset_data_dir_cache()
