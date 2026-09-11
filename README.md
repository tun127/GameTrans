# GameTrans · 游戏实时翻译助手

在任意 Windows 游戏/应用画面上**实时翻译字幕文字**，译文覆盖原文位置或显示在侧边小窗。本地优先（显卡推理，免费离线），失败可回退 OpenAI 兼容 API（DeepSeek 等）。

> 专为"官方无中文、但有英文/日文字幕"的游戏设计（如《边狱巴士》国际服）。手机端（Android）方案见 [`docs/MOBILE_ANDROID_DESIGN.md`](docs/MOBILE_ANDROID_DESIGN.md)。

## 特性

- **实时屏幕翻译**：框选字幕区域 → 自动抓屏识别 → 翻译 → 译文盖在原文上
- **双引擎**：本地 NLLB（CTranslate2，GPU int8_float16，离线免费）+ OpenAI 兼容 API（DeepSeek 等），本地优先、失败自动回退
- **质量增强**：术语词库（前置替换 + 译后替换）、译文缓存（SQLite）、上下文 + 内容背景注入（API 引擎）、中文标点清洗
- **稳定性**：画面稳定判定（避免读到滚动/打字机的半句）、相似度去重、忽略短语黑名单、噪声行过滤（时间码/监控悬浮窗）
- **性能可控**：OCR 线程数可限（默认 1 线程，实测单次 ≈1 核）、识别速度档位、识别间隔可调
- **不打扰**：提示条固定在屏幕角落（不挡画面中心）、界面缩放 100/125/150%
- **跟随游戏**：窗口锚定（译文随游戏窗口移动/缩放）、切走自动暂停、回前台自动恢复
- **热键**：默认 `Ctrl+Shift+T` 切换翻译开关

## 快速开始

### 环境要求
- Windows 10/11
- Python 3.12 或 3.13
- NVIDIA 显卡（本地推理用；仅 API 模式可纯 CPU）

### 安装

```bash
git clone <repo-url> GameTrans
cd GameTrans
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 下载本地翻译模型（约 2.3 GB，仅需一次）

```bash
python -m gametrans.tools.download_models
```

模型默认放在 `D:\GameTrans\models`（可用环境变量 `GAMETRANS_HOME` 改位置）。

### 运行

```bash
python main.py
```

启动后控制条出现在屏幕右上角：点「目标窗口」选游戏 → 点「框选区域」框住字幕 → 点「开始」。

## 使用

1. **选目标窗口**：控制条 → 目标窗口 → 列表里选游戏（或选「整屏」）
2. **框选字幕区**：控制条 → 框选区域 → 在画面上拖出字幕出现的范围
3. **开始翻译**：控制条 → 开始（或按 `Ctrl+Shift+T`）
4. **切显示模式**：控制条 → 覆盖原文 / 独立小窗
5. **设置**：控制条 → 齿轮图标

## 关键配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `ocr.threads` | `1` | OCR 线程数。不限会吃满 CPU（实测 20 核 ≈18 核）；1 线程 ≈1 核 |
| `ocr.speed` | `fast` | 检测边长 640/960/1280，越大越准越慢 |
| `ocr_interval_ms` | `600` | 两次识别最小间隔 |
| `settle_ms` | `300` | 画面变化后等稳定再识别（避免半句） |
| `min_line_len` | `2` | 短于该长度的行不翻译（过滤噪点） |
| `ignore_phrases` | 空 | 命中即丢弃该行（水印/UI 文字） |
| `game_info` | 空 | 告诉 API 引擎内容背景，译法更贴合 |
| `context_pieces` | `3` | 带最近几句原文给 API（人名/术语一致） |
| `ui_scale` | `1.25` | 控制条/小窗字号缩放 |
| `toast_pos` | `top-right` | 提示条位置（固定角落，不挡画面） |

## 测试

```bash
python tests/run_all.py            # 全部快速测试（约 8 秒）
python tests/run_all.py --slow     # 含真实 OCR 的 CPU 核数验证
python tests/run_all.py config ocr # 只跑指定模块
python tests/run_all.py -v         # 打印每条耗时
```

测试全程 `QT_QPA_PLATFORM=offscreen`，**不会弹出任何窗口**。当前 130 项全过。

## 项目结构

```
gametrans/
├── main.py                 # 入口
├── config.py               # 配置读写（JSON，数据目录可配置）
├── const.py                # 常量
├── app.py                  # 主控制器（管线编排、OCR 过滤、设置生效）
├── core/                   # 核心逻辑（不依赖 Qt，便于手机端移植）
│   ├── capture.py          #   抓屏 + 帧差异 + 稳定判定
│   ├── ocr.py              #   RapidOCR + 行合并 + 引擎参数
│   ├── translate.py        #   词库/缓存/本地引擎/路由/翻译线程
│   └── deepseek.py         #   OpenAI 兼容 API 客户端
├── ui/                     # 桌面 UI（PySide6，Windows 专有）
│   ├── control_bar.py     #   控制条
│   ├── display.py          #   译文覆盖层/侧边小窗
│   ├── settings_dialog.py  #   设置面板
│   ├── region_selector.py  #   框选
│   └── window_picker.py    #   目标窗口选择
├── utils/                  # Windows 工具（ctypes，无额外依赖）
│   ├── win32.py            #   窗口枚举/置顶/穿透/排除捕获
│   ├── hotkey.py           #   全局热键
│   └── dpi.py              #   多显示器 DPI 感知
├── tools/download_models.py  # 模型下载
└── glossaries/limbus.txt     # 《边狱巴士》术语表（示例词库）
tests/                      # 测试套件（标准库 runner，无需 pytest）
docs/MOBILE_ANDROID_DESIGN.md  # Android 端设计方案
```

## 手机端（Android）

手机端方案已设计完成（见 [`docs/MOBILE_ANDROID_DESIGN.md`](docs/MOBILE_ANDROID_DESIGN.md)）：Kotlin + MediaProjection + ML Kit OCR + 端侧混元 Hy-MT 翻译模型。`core/` 不依赖 Qt，**行合并、过滤规则、词库、缓存、引擎路由、API 客户端**可 1:1 移植；仅抓屏/悬浮窗/热键需用 Android API 重写。

## 性能实测

20 核机器、英文字幕场景：

| | 单次 OCR 耗时 | CPU 占用 |
|---|---|---|
| 不限线程（旧） | 1056 ms | ≈18 核 |
| 1 线程（默认） | 766 ms | ≈1 核 |

翻译在显卡上跑（NLLB int8_float16），不占 CPU。

## 协议

本项目采用 [MIT 协议](LICENSE)。

> ⚠️ **注意依赖模型的授权**：默认本地翻译模型 **NLLB-200 为 CC-BY-NC-4.0（禁止商用）**，
> 仅供个人/研究使用。如需商用，请在设置里改用 API 引擎，或更换为可商用的翻译模型。
> 其余第三方库（PySide6 / RapidOCR / ONNX Runtime / CTranslate2 / OpenCV 等）遵循各自协议。
