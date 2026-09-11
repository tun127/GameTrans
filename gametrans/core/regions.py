"""识别区域模型：把"框选的区域"换算为每次截图的物理像素矩形。

设计约定
--------
config["region"] 统一保存为**相对 0..1 归一化矩形**：
    {"nx": .., "ny": .., "nw": .., "nh": ..}
再配合锚定方式决定"基准矩形"（base）：
- anchor.type == "screen": base = 某块显示器（monitor_index）的物理边界
- anchor.type == "window": base = 目标窗口的**客户区**物理边界（跟随窗口移动/缩放）

每次截图前调用 denormalize(base, region) 得到真实像素 (l,t,w,h)。
这样游戏窗口被拖动/缩放时译文覆盖层与识别区域都能跟随。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from ..utils import win32

log = logging.getLogger(__name__)

Rect = Tuple[int, int, int, int]  # left, top, width, height


# ---------- 解析 ----------
def resolve_hwnd(cfg) -> Optional[int]:
    """按配置找到目标窗口句柄；锚定 screen 时返回 None。"""
    anchor = cfg.get("anchor", default={})
    if anchor.get("type") != "window":
        return None
    title = anchor.get("title_contains", "")
    if not title:
        return None
    return win32.find_window_by_title_contains(title)


def monitor_rect(cfg, index: Optional[int] = None) -> Optional[Rect]:
    monitors = win32.get_monitor_rects()
    if not monitors:
        return None
    idx = index if index is not None else int(cfg.get("anchor", default={}).get("monitor_index", 0) or 0)
    if idx >= len(monitors):
        idx = 0
    l, t, r, b = monitors[idx]
    return l, t, r - l, b - t


def client_rect(hwnd: int) -> Optional[Rect]:
    """目标窗口客户区物理矩形；窗口已关闭/不可见返回 None。"""
    rect = win32.get_client_rect_on_screen(hwnd)
    if not rect:
        return None
    l, t, r, b = rect
    if r <= l or b <= t:
        return None
    return l, t, r - l, b - t


def target_is_foreground(cfg, hwnd: Optional[int] = None) -> bool:
    """窗口锚定时，目标窗口是否处于前台（整屏模式恒为 True）。

    用于"只翻译选定的游戏窗口"：不在前台时暂停抓屏/翻译，
    避免把浏览器、桌面等别的内容当游戏翻译。
    """
    anchor = cfg.get("anchor", default={})
    if anchor.get("type") != "window":
        return True
    h = hwnd if hwnd is not None else resolve_hwnd(cfg)
    if h is None:
        return False
    return win32.is_foreground_related(h)


def current_base_rect(cfg, hwnd: Optional[int] = None) -> Optional[Rect]:
    """当前生效的基准矩形。hwnd 可预解析缓存（避免每帧枚举窗口）。"""
    anchor = cfg.get("anchor", default={})
    if anchor.get("type") == "window":
        h = hwnd or resolve_hwnd(cfg)
        if h is None:
            return None
        return client_rect(h)
    return monitor_rect(cfg)


# ---------- 区域换算 ----------
def region_of_abs(base: Rect, abs_rect: Rect) -> Dict[str, float]:
    """把物理像素矩形映射到 base 的 0..1 归一化区域。"""
    bl, bt, bw, bh = base
    l, t, w, h = abs_rect
    nx = (l - bl) / bw if bw else 0.0
    ny = (t - bt) / bh if bh else 0.0
    nw = w / bw if bw else 0.0
    nh = h / bh if bh else 0.0
    nx = max(0.0, min(1.0, nx))
    ny = max(0.0, min(1.0, ny))
    nw = max(0.0, min(1.0 - nx, nw))
    nh = max(0.0, min(1.0 - ny, nh))
    return {"nx": nx, "ny": ny, "nw": nw, "nh": nh}


def region_to_abs(base: Rect, region: Optional[Dict]) -> Optional[Rect]:
    """把归一化区域换算为当前 base 下的物理像素矩形。"""
    if not region:
        return None
    bl, bt, bw, bh = base
    nx = float(region.get("nx", 0.0))
    ny = float(region.get("ny", 0.0))
    nw = float(region.get("nw", 1.0))
    nh = float(region.get("nh", 1.0))
    nx = max(0.0, min(1.0, nx))
    ny = max(0.0, min(1.0, ny))
    nw = max(0.0, min(1.0, nx + nw) - nx)
    nh = max(0.0, min(1.0, ny + nh) - ny)
    l = int(round(bl + nx * bw))
    t = int(round(bt + ny * bh))
    w = int(round(nw * bw))
    h = int(round(nh * bh))
    if w < 8 or h < 8:
        return None
    return l, t, w, h


def save_abs_selection(cfg, abs_rect: Rect) -> bool:
    """把框选得到的物理绝对矩形写入配置（转为归一化+锚定信息）。

    若锚定窗口，基准为窗口客户区；否则基准为选区中心所在显示器。
    返回是否保存成功。
    """
    anchor = cfg.get("anchor", default={})
    if anchor.get("type") == "window":
        hwnd = resolve_hwnd(cfg)
        if hwnd is None:
            # 目标窗口已关闭/标题已变（浏览器标签页切换很常见）：
            # 自动降级为整屏锚定，保证框选永远可用，而不是拒绝保存
            log.warning("目标窗口已不可用，自动切换为整屏锚定并保存选区")
            cfg.set("anchor", "type", "screen")
            cfg.set("anchor", "title_contains", "")
            anchor = cfg.get("anchor", default={})
            hwnd = None
        base = client_rect(hwnd) if hwnd else None
        if base is None:
            base = monitor_rect(cfg)
        if base is None:
            log.warning("无法定位任何可用的基准矩形")
            return False
    else:
        # 找到选区中心所在显示器作为基准
        cx = abs_rect[0] + abs_rect[2] // 2
        cy = abs_rect[1] + abs_rect[3] // 2
        monitors = win32.get_monitor_rects()
        if not monitors:
            log.warning("无法获取显示器信息，选区保存失败")
            return False
        idx = 0
        base = None
        for i, (l, t, r, b) in enumerate(monitors):
            if l <= cx < r and t <= cy < b:
                idx, base = i, (l, t, r - l, b - t)
                break
        if base is None:
            base = monitors[0]
            idx = 0
        anchor = dict(anchor)
        anchor["type"] = "screen"
        anchor["monitor_index"] = idx
        cfg.set("anchor", "type", "screen")
        cfg.set("anchor", "monitor_index", idx)

    region = region_of_abs(base, abs_rect)
    cfg.set("region", region)
    log.info("区域已保存: base=%s abs=%s norm=%s", base, abs_rect, region)
    return True


def snapshot_region_desc(cfg) -> str:
    """给控制条显示的简短描述，如『窗口已框选 640×180』。"""
    region = cfg.get("region")
    if not region:
        return "框选区域"
    base = current_base_rect(cfg)
    abs_rect = region_to_abs(base, region) if base else None
    anchor = cfg.get("anchor", default={})
    prefix = "窗口" if anchor.get("type") == "window" else ""
    if abs_rect:
        return f"{prefix}已框选 {abs_rect[2]}×{abs_rect[3]}"
    return f"{prefix}已框选 {int(region.get('nw', 0) * 100)}%×{int(region.get('nh', 0) * 100)}%"
