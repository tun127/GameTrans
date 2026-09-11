"""GameTrans 测试运行器（标准库实现，无需 pytest）。

用法：
    python tests/run_all.py                 # 全部快速测试（约 1 分钟）
    python tests/run_all.py --slow          # 额外跑需要模型/显卡的慢测试
    python tests/run_all.py config ocr ui   # 只跑文件名含关键字的模块
    python tests/run_all.py -v              # 打印每条测试的耗时

约定：
- 测试文件为 tests/test_*.py，测试函数名以 test_ 开头；
- 依赖模型/显卡的慢测试命名 slow_test_*，默认跳过（加 --slow 才跑）；
- 全程 QT_QPA_PLATFORM=offscreen，不会在屏幕上弹出窗口。
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys
import time
import traceback
from pathlib import Path
from types import ModuleType
from typing import Callable, List, Tuple

# 必须在导入任何 Qt 相关模块之前设置：测试不得弹窗
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TESTS_DIR = Path(__file__).resolve().parent


def _discover(filters: List[str]) -> List[ModuleType]:
    mods: List[ModuleType] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if filters and not any(f in path.stem for f in filters):
            continue
        try:
            mods.append(importlib.import_module(f"tests.{path.stem}"))
        except Exception as exc:  # noqa: BLE001
            print(f"[加载失败] {path.name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    return mods


def _collect(mods: List[ModuleType], slow: bool) -> List[Tuple[str, Callable[[], None]]]:
    cases: List[Tuple[str, Callable[[], None]]] = []
    for mod in mods:
        name = mod.__name__.split(".")[-1]
        for fn_name, fn in inspect.getmembers(mod, inspect.isfunction):
            if fn_name.startswith("test_"):
                cases.append((f"{name}.{fn_name}", fn))
            elif slow and fn_name.startswith("slow_test_"):
                cases.append((f"{name}.{fn_name}", fn))
    return cases


def main(argv: List[str]) -> int:
    slow = "--slow" in argv
    verbose = "-v" in argv or "--verbose" in argv
    filters = [a for a in argv if not a.startswith("-")]

    print("=" * 72)
    print(f"GameTrans 测试  |  Python {sys.version.split()[0]}  |  "
          f"慢测试: {'开' if slow else '关'}")
    print("=" * 72)

    mods = _discover(filters)
    if not mods:
        print("没有匹配的测试模块。")
        return 1

    cases = _collect(mods, slow)
    passed, failed, errors = 0, [], []
    t_all = time.perf_counter()

    for full_name, fn in cases:
        t0 = time.perf_counter()
        try:
            fn()
        except AssertionError as exc:
            failed.append((full_name, f"断言失败: {exc}", traceback.format_exc()))
            status = "FAIL"
        except Exception as exc:  # noqa: BLE001
            errors.append((full_name, f"{type(exc).__name__}: {exc}",
                           traceback.format_exc()))
            status = "ERROR"
        else:
            passed += 1
            status = "PASS"
        dt = (time.perf_counter() - t0) * 1000
        if verbose or status != "PASS":
            print(f"[{status}] {full_name} ({dt:.0f}ms)")

    dt_all = time.perf_counter() - t_all
    total = len(cases)
    print("-" * 72)
    print(f"合计 {total} 项：通过 {passed}，失败 {len(failed)}，异常 {len(errors)}，"
          f"耗时 {dt_all:.1f}s")

    for title, group in (("失败", failed), ("异常", errors)):
        if not group:
            continue
        print(f"\n===== {title}详情 =====")
        for name, msg, tb in group:
            print(f"\n--- {name}: {msg}")
            print(tb.rstrip())

    print("=" * 72)
    print("全部通过 ✔" if not failed and not errors
          else f"存在 {len(failed) + len(errors)} 项问题 ✘")
    return 0 if not failed and not errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
