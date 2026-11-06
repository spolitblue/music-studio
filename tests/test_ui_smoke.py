"""离屏冒烟：把所有界面模块都构造一遍，抓导入/构造期错误。"""

import faulthandler
import os
import sys
import traceback

# 原生崩溃（访问违例）拿不到 Python 回溯，必须靠 faulthandler 打信号时的调用栈
faulthandler.enable()

# --- 项目根路径引导（本文件位于子目录，保证从任何 cwd 都能 import app.*）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app import icons
from app.audio import demo

failures: list[str] = []


def stage(name, fn):
    try:
        fn()
        print(f"  [OK  ] {name}")
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"  [FAIL] {name} — {type(exc).__name__}: {exc}")
        traceback.print_exc()


app = QApplication(sys.argv)
app.setApplicationName("制乐 Music Studio")

print("=" * 68)
print("界面模块构造（offscreen）")
print("=" * 68)

from app.ui.chrome import FramelessWindow, TitleBar
from app.ui.controller import AppController
from app.ui.pages.overview import MasterCard, MiniRoll, OverviewPage, ProjectHeader, TrackStrip
from app.ui.pages.placeholder import PlaceholderPage
from app.ui.sidebar import NavItem, Sidebar
from app.ui.transport import LevelMeter, TransportBar, WaveformView
from app.widgets.controls import GlassKnob, GlassSlider
from app.widgets.glass import (
    GlassButton, GlassCard, GlassChip, GlassDivider, GlassImageButton,
    GlassIconButton, GlassPanel, GlassSegmented,
)

project = demo.demo_project()

stage("设计令牌/排版", lambda: (icons.app_icon(64), icons.pixmap("piano", 32)))
stage("GlassPanel", lambda: GlassPanel())
stage("GlassCard", lambda: GlassCard())
stage("GlassButton", lambda: GlassButton("按钮"))
stage("GlassIconButton", lambda: GlassIconButton("✕"))
stage("GlassImageButton", lambda: GlassImageButton("play", size=32))
stage("GlassSegmented", lambda: GlassSegmented(["一", "二", "三"]))
stage("GlassChip", lambda: GlassChip("芯片", checkable=True))
stage("GlassDivider", lambda: GlassDivider())
stage("GlassSlider", lambda: GlassSlider(minimum=0, maximum=1, value=0.5))
stage("GlassKnob", lambda: GlassKnob(minimum=-1, maximum=1, value=0))
stage("TitleBar", lambda: TitleBar())
stage("Sidebar", lambda: Sidebar())
stage("WaveformView", lambda: WaveformView())
stage("LevelMeter", lambda: LevelMeter())
stage("TransportBar", lambda: TransportBar())
stage("MiniRoll", lambda: MiniRoll())
stage("ProjectHeader", lambda: ProjectHeader())
stage("MasterCard", lambda: MasterCard(project))
stage("TrackStrip", lambda: TrackStrip(project.tracks[0], project, 0))
stage("OverviewPage", lambda: OverviewPage(project))
stage("PlaceholderPage", lambda: PlaceholderPage("测试", "robot", "说明", ["a", "b"], None))
def _make_controller():
    c = AppController(project)
    holder["controller"] = c      # 保住引用，否则渲染线程会被提前销毁
    return c


holder = {}
stage("AppController", _make_controller)


def build_window():
    from app.ui.main_window import MainWindow

    w = MainWindow(project)
    w.resize(1280, 820)
    w.show()
    holder["w"] = w
    return w


stage("MainWindow", build_window)
stage("MainWindow", lambda: holder.setdefault("w", build_window()))


def render_pass():
    """跑几轮事件循环，让绘制真正发生（构造成功不代表 paintEvent 不炸）。"""
    w = holder.get("w")
    if w is None:
        return
    try:
        w.repaint()
        app.processEvents()
        print("  [OK  ] 首帧绘制")
    except Exception as exc:
        failures.append(f"首帧绘制: {exc}")
        print(f"  [FAIL] 首帧绘制 — {exc}")
        traceback.print_exc()

    # 逐页切换，每一页都触发一次绘制
    for key in list(w.pages.keys()):
        try:
            w._navigate(key)
            w.repaint()
            app.processEvents()
            print(f"  [OK  ] 页面绘制 {key}")
        except Exception as exc:
            failures.append(f"页面 {key}: {exc}")
            print(f"  [FAIL] 页面 {key} — {exc}")
            traceback.print_exc()

    try:
        if holder.get("controller") is not None:
            holder["controller"].shutdown()
        if w is not None:
            w.controller.shutdown()
    except Exception as exc:
        failures.append(f"关闭控制器: {exc}")
        print(f"  [FAIL] 关闭控制器 — {exc}")
    app.quit()


QTimer.singleShot(120, render_pass)
QTimer.singleShot(8000, app.quit)
app.exec()

print()
print("=" * 68)
if failures:
    print(f"失败 {len(failures)} 项：")
    for f in failures:
        print("  -", f)
else:
    print("全部通过")
print("=" * 68)
sys.exit(1 if failures else 0)
