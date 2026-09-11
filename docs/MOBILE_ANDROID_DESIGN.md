# GameTrans 移动端（Android）设计方案 v2

> 目标：基于现有 PC 版（Python/PySide6）做 **Android 原生**实时屏幕翻译工具。
> **已确认场景：源语言 = 英文（国际服 / PC Steam 版《边狱巴士》，官方无中文）**，译文 = 简体中文。
> 语言架构保留可扩展（英/日/韩 → 中）。

---

## 0. 结论摘要

| 问题 | 结论 |
|---|---|
| 手机端能不能做 | **能**。Android 免 root：`MediaProjection` 抓屏 + `SYSTEM_ALERT_WINDOW` 悬浮窗，业界已有多款同类开源项目 |
| iOS 能不能做 | **不能**做"覆盖在其他 App 上"（系统禁止跨 App 悬浮窗）；只能降级为"截图后在自家 App 内翻译" |
| 语言链路 | **英 → 中，PC 版已支持**，无需新增语言代码。参数化多语言架构（`en/ja/ko → zh`） |
| 现有 Python 代码能直接上手机吗 | **不能**（抓屏/窗口/悬浮窗/热键全是 Win32 专有）。但**翻译策略、缓存、词库、过滤、排版**可 1:1 移植 |
| 边狱巴士本地翻译可行吗 | **可行**。端侧译文模型（腾讯混元 Hy-MT 1.8B）已有 Android 落地先例；**术语表**是质量关键（已提供初版） |
| 最大风险 | ① 竞技手游反作弊误判截屏（边狱巴士无此风险）② 国产 ROM 杀后台/拦悬浮窗 ③ 端侧模型占内存 |
| 立即可做 | **PC 版现在就能翻 Steam 版边狱巴士**（英文链路已支持），配术语表即可用 |

---

## 1. 目标与边界

### 1.1 本期目标（Android）
- 免 root，在**任意 App 画面**上实时翻译文字；译文**覆盖原文位置**或显示在**侧边小窗**
- **离线可用**：OCR 与翻译均可在端侧完成（可选 API 提升质量）
- 首个深度适配场景：**英文 → 中文**，针对《边狱巴士》这类"对话框 + 术语密集"的手游
- 与 PC 版共享策略语义与资产：缓存、词库、忽略短语、行合并、去重、上下文

### 1.2 明确不做（本期）
- iOS 实时覆盖（不可行）
- 竞技手游（反作弊风险）
- 多区域同翻、语音识别（TTS 朗读列后续可选）

### 1.3 系统要求
- Android 8.0+（API 26），仅 `arm64-v8a`
- 权限：悬浮窗、通知、前台服务、MediaProjection 授权（Android 14+ 冷启动需重新授权一次）

---

## 2. 技术选型

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | **Kotlin** | 与同类项目（屏译 overlay-translator）一致 |
| UI | **Jetpack Compose**（设置页）+ `WindowManager`（悬浮窗） | 悬浮窗必须走 WindowManager |
| 抓屏 | **MediaProjection + VirtualDisplay + ImageReader** | 系统标准通路；Shizuku 作为进阶可选（免重复授权） |
| OCR | **ML Kit Text Recognition v2**（首选，离线免费，英/日/中/韩均支持）；备选 PaddleOCR-ONNX | 只对"框选区域"识别 |
| 翻译（离线） | **llama.cpp + GGUF**（腾讯混元 Hy-MT，见 §3.3） | 翻译专用模型，质量远优于系统翻译 |
| 翻译（在线） | **OpenAI 兼容 API**（DeepSeek/Kimi/Ollama…） | 直接复用 PC 版客户端与 prompt |
| 存储 | SQLite（缓存）+ DataStore（配置） | 缓存表结构与 PC 版一致 |
| 网络 | OkHttp + kotlinx.serialization | 轻量 |

---

## 3. 边狱巴士（Limbus Company）专项设计

### 3.1 游戏文本特征
| 特征 | 影响 |
|---|---|
| **源语言：英文**（国际服 / Steam 版，官方无中文） | 链路为 **英 → 中**，PC 版与手机端都无需新增语言支持 |
| **专有名词极密集**：罪人(13)、人格(Identity)、E.G.O、异想体、系统名词 | **术语表是质量的决定性因素**，优先于模型选型 |
| 手机端为竖屏，文本集中在对话框 | 框选区域约占屏宽 × 屏高 10~20%，抓屏/OCR/翻译负担小，性能友好 |
| 台词多为短句，剧情有长段 | 需"整句合并"（`merge_row_lines` 可移植），避免逐词翻译 |
| 单机向、无竞技反作弊 | 抓屏无封号风险 |

### 3.2 翻译链路
```
MediaProjection 抓帧
  → 裁剪"对话框框选区"
  → ML Kit OCR（英文）→ 行框 + 文本
  → 行合并（同排片段拼整句）+ 噪声/忽略短语过滤 + 去重
  → 翻译：
       ① 术语库命中（单行/短串直接替换，零延迟）
       ② 缓存命中（毫秒级）
       ③ 未命中 → 端侧 Hy-MT 批量翻译（附带上文 N 句）
       ④（可选）切 API 引擎获得更高质量
  → 术语后替换 + 中文标点清洗
  → 悬浮窗按 OCR 行框绘制译文 / 侧边小窗
  → 写入缓存
```

### 3.3 端侧翻译模型选型

| 方案 | 体积 | 英→中质量 | 速度 | 内存 | 结论 |
|---|---|---|---|---|---|
| **腾讯混元 Hy-MT2 1.8B (Q4_K_M)** | ~1.08 GB | **好**（翻译专用） | 中（参考屏译：18 段 ≈ 11s） | KV ≈ 512 MB | **首选**，Android 已验证 |
| **腾讯混元 Hy-MT1.5-1.8B (1.25bit)** | **~440 MB** | 好 | 快 | 低（可常驻） | 体积最优 |
| ML Kit Translation | ~30 MB/语言对 | 中等（术语/语气弱） | 很快（<100 ms） | 极低 | 低配机型兜底 |
| API（DeepSeek 等） | 0 | **最好** | 受网络影响 | 0 | 质量模式 |
| 通用小 LLM（Qwen2.5-1.5B 等） | ~1 GB | 中等 | 中 | 512 MB+ | 不如 Hy-MT，不选 |

**推荐组合**：默认 **Hy-MT2 1.8B Q4_K_M（离线）**；低配机型自动降级 **ML Kit**；设置可切 **API**。
三种模式共用同一套「调度 + 缓存 + 术语 + 上下文」，与 PC 版 `auto/local/api` 语义一致。

### 3.4 术语表（已产出初版 ✅）
- 文件：`glossaries/limbus.txt`（PC 版可直接用，手机端同一份）
- 格式与 PC 版词库一致：`原文|译文`，热重载，支持韩/英混排
- 已覆盖：13 名罪人 + 管理人、世界观/组织（The City / Wing / Nest / Backstreets / Fixer…）、系统与玩法（Identity / E.G.O / Enkephalin / Mirror Dungeon / Luxcavation / Refraction Railway…）、战斗术语（Clash / Coin / Stagger / Sanity…）
- 含全大写写法（游戏 UI 常用大写）
- ⚠️ 待优化：PC 版词库匹配当前**区分大小写**，建议改为**不区分大小写**（一行改动，收益明显）

### 3.5 现状：PC 版即可用（无需改语言）
- `const.py` 已支持 `en → zh`（`eng_Latn → zho_Hans`）
- 操作：打开 PC 版 → 『框选区域』框住游戏对话框 → 『开始』
- 窗口锚定（控制条『整屏/窗口』按钮）可让译文跟随游戏窗口移动
- **建议**：先用 PC 版把术语表养准（译名对齐你的习惯），这份术语表手机端直接复用

---

## 4. 架构设计

### 4.1 数据流与线程
```
[CaptureService]  MediaProjection + VirtualDisplay + ImageReader（仅框选区域）
        ▼
[FrameGate]  缩略图帧差异 + 画面稳定判定（同 PC 版 settle）
        ▼
[OcrPipeline] ML Kit OCR → 行合并 → 噪声/忽略短语过滤 → 去重签名
        ▼
[TranslatePipeline] 术语 → 缓存 → 端侧/API 批量翻译 → 术语替换 + 标点清洗
        ▼
[OverlayRenderer] WindowManager 悬浮窗：按 box 绘制译文 / 侧边小窗
```
线程：抓屏（HandlerThread）→ OCR（单线程队列，容量 1，丢旧帧）→ 翻译（单线程，容量 1）→ 主线程渲染。

### 4.2 模块清单（★ = 可从 PC 版移植）
| 模块 | 职责 | 来源 |
|---|---|---|
| `CaptureService` | 前台服务 + 抓帧 | 新写（对应 `core/capture.py`） |
| `FrameGate` ★ | 帧差异 + 稳定判定 | 移植 `capture._changed/_feed` |
| `OcrEngine` | ML Kit 识别 + 归一化坐标 | 新写（对应 `core/ocr.py`） |
| `LineMerger` ★ | 同行片段合并（2 倍行高 / 封顶 45% 宽） | **直接移植** `merge_row_lines` |
| `TextFilter` ★ | 噪声行、回声、忽略短语、最小行长、相似度去重 | **直接移植** `app.py` 四个判定函数 |
| `Glossary` ★ | 词库加载/热更新/后替换（建议改不区分大小写） | **直接移植** `translate.Glossary` |
| `TranslationCache` ★ | SQLite `(lang,src,dst)` + 只缓存含汉字结果 | **直接移植** |
| `TermPipeline` ★ | 术语优先 / 上下文 / 标点清洗 | 移植 `translate_lines` |
| `LocalMtEngine` | llama.cpp + Hy-MT GGUF | 新写（对应 `LocalTranslator`） |
| `ApiEngine` ★ | OpenAI 兼容 + prompt + 容错解析 + 失败回退 | **直接移植** `deepseek.py` |
| `EngineRouter` ★ | auto/local/api + 失败冷却 + 回退 | **直接移植** `EngineRouter` |
| `OverlayRenderer` | 悬浮窗覆盖/小窗、穿透、拖拽 | 新写（对应 `ui/display.py`） |
| `ControlBall` | 悬浮球：开始/停止/框选/切模式 | 替代全局热键 |
| `SettingsScreen` | Compose 设置页 | 新写（对应 `settings_dialog.py`） |

### 4.3 防自截（Android 没有"窗口对截屏不可见"）
抓帧后**按悬浮窗（含悬浮球）几何把该区域抹掉**再送 OCR：
1. 记录悬浮窗矩形
2. 帧上这些矩形填充纯色/上一帧内容（OCR 前完成，成本极低）
3. 可选优化：用周围像素填充，避免黑块影响观感
> PC 版 `_on_capture_frame` 里"剔除控制条遮挡区域"的代码可直接移植。

### 4.4 性能预算
| 环节 | 目标 |
|---|---|
| 抓屏 + 裁剪 | < 30 ms |
| 帧差异 + 稳定判定 | < 5 ms |
| ML Kit OCR（对话框区域） | 150 ~ 400 ms |
| 端侧翻译（Hy-MT，1~3 句） | 400 ms ~ 1.5 s |
| 端到端（缓存命中） | **< 200 ms** |
| 端到端（新句，离线） | **< 2 s** |
| 常驻内存 | < 400 MB（未加载模型）/ < 1.2 GB（加载后） |

---

## 5. 从 PC 版移植清单

| 资产 | 方式 | 工作量 |
|---|---|---|
| `merge_row_lines` 行合并 | Kotlin 重写（算法一致） | 0.5 h |
| 噪声/回声/忽略短语/最小行长/相似度 | Kotlin 重写 | 1 h |
| 词库解析与后替换（含热加载） | Kotlin 重写 | 1 h |
| 缓存表 + 只缓存含汉字结果 | 同 SQL 结构 | 0.5 h |
| 引擎调度（auto/local/api + 冷却 + 回退） | Kotlin 重写 | 1 h |
| OpenAI 兼容客户端 + 容错解析 + prompt | Kotlin 重写 | 2 h |
| 配置项语义（settle/min_line_len/ignore_phrases/样式） | 沿用命名 | — |
| **PC 版词库改为不区分大小写** | Python 改 1 处 | 0.2 h |
| **术语表 glossaries/limbus.txt** | **已完成 ✅** | — |

---

## 6. 配置与数据结构

### 6.1 配置（语义对齐 PC 版）
```jsonc
{
  "source_lang": "en",            // auto | en | ja | ko
  "engine": "auto",               // auto(本地优先) | local(仅端侧) | api(仅API)
  "ocr": { "backend": "mlkit", "min_conf": 0.45 },
  "capture": { "interval_ms": 400, "settle_ms": 300, "frame_diff_threshold": 0.03 },
  "filter": { "min_line_len": 2, "ignore_phrases": "", "similarity_skip": 0.82 },
  "mt": { "model": "hy-mt2-1.8b-q4", "context_pieces": 3, "threads": 6 },
  "api": { "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": "" },
  "display": { "mode": "overlay", "font_size": 22, "text_color": "#FFFFFF",
               "bg_color": "#1A1A1A", "bg_alpha": 200, "hide_source": true },
  "glossary_path": "limbus.txt",
  "auto_cache": true
}
```

### 6.2 缓存表（与 PC 版一致）
```sql
CREATE TABLE IF NOT EXISTS translations (
  lang TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
  PRIMARY KEY (lang, src)
);
```

### 6.3 词库格式（与 PC 版一致）
```
# 每行：原文|译文  或  原文=译文
Identity|人格
Mirror Dungeon|镜像迷宫
```

---

## 7. 里程碑与验收标准

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M0 PoC**（0.5~1 天） | 悬浮球 + MediaProjection + 框选 + 悬浮窗显示**假译文** | 能在边狱巴士上框选对话框；悬浮窗对齐；防自截生效；切后台不崩 |
| **M1 识别** | ML Kit OCR + 行合并 + 过滤 | 对话框英文被合并为整句；UI 噪声被过滤 |
| **M2 离线翻译** | llama.cpp + Hy-MT + 术语优先 + 缓存 | 离线出中文；术语命中 100% 正确；端到端 < 2s |
| **M3 打磨** | 引擎调度、样式、忽略短语、拖拽/穿透、保活引导 | 三引擎可切；失败自动回退；国产 ROM 白名单后稳定 1 小时 |
| **M4 可选** | 上下文增强、译文编辑、TTS 朗读、配置导入导出、PC 互通 | — |

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 竞技手游反作弊误判截屏 | 封号 | 文档明示不适用；边狱巴士无此风险 |
| 国产 ROM 杀后台/拦悬浮窗 | 跑一会失效 | 前台服务 + 通知常驻；内置厂商白名单引导 |
| Android 14+ 冷启动需重新授权截屏 | 体验摩擦 | 引导提示；进阶支持 Shizuku |
| `FLAG_SECURE` 画面黑屏 | 部分 App 无法翻 | 系统行为无法绕过，文档说明 |
| 端侧模型吃内存 | 低端机卡顿/OOM | 三档可选：1.08 GB / 440 MB / ML Kit |
| 模型下载耗时 | 首次体验差 | 后台下载 + 断点续传 + 进度通知 |
| 英文 OCR 漏词/断行 | 译文残缺 | 已解决：OCR 线程内先合并整行（PC 版同款修复） |
| 术语覆盖不全 | 专名不一致 | 持续补充术语表；支持自定义并导出分享 |

---

## 9. 建议的执行顺序

1. **现在**：用 PC 版翻 Steam 版边狱巴士（英文链路已支持）+ 加载 `glossaries/limbus.txt`，把术语译名调到满意 → 这份表手机端直接复用；
2. 顺手做 2 个小优化：词库**不区分大小写**、术语表接入默认词库；
3. 然后按 **M0 → M1 → M2 → M3** 推进 Android 端。

---

## 10. 待确认

1. **手机机型 / 内存**（如骁龙 8 Gen 3 / 12GB）→ 决定端侧模型选 1.08 GB 还是 440 MB，或退到 ML Kit
2. **是否接受首次下载 0.5~1.2 GB 模型**（离线翻译前提）
3. **是否需要与 PC 版互通**（共用词库/忽略短语/缓存导出导入）
