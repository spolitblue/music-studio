"""验证：无色玻璃 + 拖动时高频刷新（不闪、不平移失效）。"""

import sys
import time
from pathlib import Path

# --- 项目根路径引导（本文件位于子目录，保证从任何 cwd 都能 import app.*）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from app import theme
from app.audio import demo
from app.ui.main_window import MainWindow
from app.widgets.ambient import ambient

print("=" * 68)
print("1) 玻璃是否无色")
print("=" * 68)
f = theme.GLOSS
print(f"  档位        : {f.name}")
print(f"  饱和度      : {f.saturate}  ({'去饱和=中性' if f.saturate < 1 else '加饱和=带色'})")
print(f"  凝霜色      : {f.frost_color if hasattr(f, 'frost_color') else '见 LiquidParams'}")
from app.widgets.liquid import LiquidParams

lp = LiquidParams.from_finish(f)
print(f"  凝霜色(实际): {lp.frost_color}  (中性灰 = R≈G≈B)")
r, g, b = lp.frost_color
check_neutral = abs(r - g) <= 6 and abs(g - b) <= 6

w = MainWindow(demo.demo_project())
w.resize(1000, 660)
w.move(200, 150)
w.show()

state = {"step": 0, "samples": []}


def measure(tag):
    pm = w.grab()
    img = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    W, H = img.width(), img.height()
    a = np.frombuffer(img.constBits(), dtype=np.uint8, count=W * H * 4).reshape(H, W, 4)
    dpr = pm.devicePixelRatio() or 1.0
    x0, y0 = int(420 * dpr), int(200 * dpr)
    x1, y1 = int(900 * dpr), int(240 * dpr)
    s = a[y0:y1, x0:x1, :3].astype(np.float32)
    mean = s.reshape(-1, 3).mean(axis=0)
    # 色偏：三个通道的极差，越小越"无色"
    cast = float(mean.max() - mean.min())
    state["samples"].append((tag, float(mean.mean()), cast))
    return mean, cast


def stage1():
    mean, cast = measure("静止")
    print()
    print("=" * 68)
    print("2) 色偏（面板区域三通道极差，越小越中性）")
    print("=" * 68)
    print(f"  静止时  R={mean[0]:6.1f} G={mean[1]:6.1f} B={mean[2]:6.1f}  "
          f"通道极差 {cast:5.2f}")
    print(f"  {'[OK  ]' if cast < 14 else '[FAIL]'} 玻璃呈中性（阈值 14）")
    print(f"  {'[OK  ]' if check_neutral else '[FAIL]'} 凝霜色是中性灰")
    QTimer.singleShot(400, stage2)


def stage2():
    print()
    print("=" * 68)
    print("3) 拖动时是否高频刷新且不重拍")
    print("=" * 68)
    if ambient._capture_rect:
        cr = ambient._capture_rect
        print(f"  截图范围 {cr.width()}x{cr.height()}，窗口 {w.width()}x{w.height()}")
        print(f"  {'[OK  ]' if cr.width() > w.width() and cr.height() > w.height() else '[FAIL]'}"
              f" 截图带余量（拖动时无需重拍）")
    state["t0"] = time.perf_counter()
    state["captures"] = 0
    _orig = ambient.set_desktop_capture

    def counted(pm, rect=None):
        state["captures"] += 1
        return _orig(pm, rect)

    ambient.set_desktop_capture = counted
    do_move()


def do_move():
    if state["step"] >= 14:
        QTimer.singleShot(900, stage3)
        return
    state["step"] += 1
    w.move(200 + state["step"] * 16, 150 + state["step"] * 5)
    QTimer.singleShot(85, do_move)


def stage3():
    dt = time.perf_counter() - state["t0"]
    n = state["step"]
    print(f"  拖动 {n} 步，耗时 {dt:.2f}s → 平均每步 {dt / n * 1000:.0f} ms")
    print(f"  {'[OK  ]' if dt / n < 0.30 else '[FAIL]'} 刷新足够频繁（阈值 300ms/步，旧逻辑是 420ms 才动一次）")
    print(f"  {'[OK  ]' if state['captures'] <= 2 else '[FAIL]'} 拖动期间几乎没有重新截图"
          f"（{state['captures']} 次；每次截图都会让窗口闪一下）")
    mean, cast = measure("拖动后")
    print(f"  拖动后  通道极差 {cast:5.2f}  {'[OK]' if cast < 14 else '[FAIL]'} 仍为中性")
    w.controller.shutdown()
    app.quit()


QTimer.singleShot(2000, stage1)
QTimer.singleShot(20000, app.quit)
app.exec()
