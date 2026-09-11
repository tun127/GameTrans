"""全局常量。"""

APP_NAME = "GameTrans"
APP_TITLE = "游戏实时翻译助手 GameTrans"
APP_VERSION = "0.1.0"

# 源语言选项
LANG_AUTO = "auto"
LANG_EN = "en"
LANG_JA = "ja"
SRC_LANG_NAMES = {
    LANG_AUTO: "自动",
    LANG_EN: "英文",
    LANG_JA: "日文",
}
SRC_LANG_CYCLE = [LANG_AUTO, LANG_EN, LANG_JA]

# 显示模式
MODE_OVERLAY = "overlay"   # 覆盖原文位置
MODE_SIDE = "side"         # 独立置顶小窗
MODE_NAMES = {MODE_OVERLAY: "覆盖原文", MODE_SIDE: "独立小窗"}
MODE_CYCLE = [MODE_OVERLAY, MODE_SIDE]

# 翻译引擎
ENGINE_LOCAL_FIRST = "local_first"  # 本地优先，本地不可用才转 API（默认）
ENGINE_AUTO = "auto"    # API 优先后回退本地
ENGINE_LOCAL = "local"  # 仅本地
ENGINE_API = "api"      # 仅 API（OpenAI 兼容）
ENGINE_NAMES = {
    ENGINE_LOCAL_FIRST: "本地优先（失败转 API）",
    ENGINE_AUTO: "API 优先（失败转本地）",
    ENGINE_LOCAL: "仅本地离线",
    ENGINE_API: "仅 API（OpenAI 兼容）",
}

# 翻译源语言 -> NLLB 语言代码
NLLB_SRC_CODE = {LANG_EN: "eng_Latn", LANG_JA: "jpn_Jpan"}
NLLB_TGT_CODE = "zho_Hans"

# 目标语言（固定简体中文）
TARGET_LANG = "zh-CN"

DEFAULT_HOTKEY = "Ctrl+Shift+T"

# 速度预设（毫秒）
SPEED_PRESETS = {
    "fast": {"capture_interval_ms": 33, "debounce_ms": 60, "stable_ms": 120},
    "balanced": {"capture_interval_ms": 66, "debounce_ms": 120, "stable_ms": 250},
    "saver": {"capture_interval_ms": 150, "debounce_ms": 250, "stable_ms": 500},
}

DEEPSEEK_DEFAULT_URL = "https://api.deepseek.com/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
