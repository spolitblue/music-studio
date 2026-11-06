"""开真窗口 → 等渲染 → 截图。

两条截图路径各有用途，都要：
* ``win.grab()``      —— Qt 自己渲染的画面，**不受遮挡影响**，用于核对排版与配色；
* ``screen.grabWindow`` —— 屏幕合成结果，**含 DWM 亚克力**，用于核对玻璃材质。

坐标坑：``QScreen.grabWindow`` 的 x/y/w/h 是**逻辑像素**，返回的 QPixmap 才带
devicePixelRatio。多乘一次 dpr 会裁到完全不相干的区域（踩过一次）。
"""

import ctypes
import os
import sys
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

# --- 项目根路径引导（本文件位于子目录，保证从任何 cwd 都能 import app.*）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

from app import icons
from app.audio import demo

OUT = Path("_shots")
OUT.mkdir(exist_ok=True)

app = QApplication(sys.argv)
app.setApplicationName("制乐 Music Studio")
app.setWindowIcon(icons.app_icon(256))

from app.ui.main_window import MainWindow

project = demo.demo_project()
win = MainWindow(project)
win.resize(1280, 800)
win.show()

# 挪到固定位置并强制置顶，保证屏幕截图里是我们的窗口
WIN_X, WIN_Y = 30, 24
win.move(WIN_X, WIN_Y)
hwnd = int(win.winId())
HWND_TOPMOST, SWP_SHOWWINDOW = -1, 0x0040
ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, WIN_X, WIN_Y, 1280, 800, SWP_SHOWWINDOW)
win.raise_()
win.activateWindow()

state = {"render_done": False}


def grab_qt(name: str) -> None:
    """Qt 自绘画面 —— 反映窗口自己的渲染，不受遮挡。"""
    pm = win.grab()
    path = OUT / f"{name}_qt.png"
    pm.save(str(path))
    print(f"  [Qt  ] {path.name}  {pm.width()}x{pm.height()}  dpr={pm.devicePixelRatio()}")


def grab_screen(name: str) -> None:
    """屏幕合成画面 —— 含 DWM 亚克力。"""
    screen = QGuiApplication.primaryScreen()
    geo = win.frameGeometry()
    # 注意：这里传逻辑像素，不要再乘 devicePixelRatio
    pm = screen.grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
    path = OUT / f"{name}_screen.png"
    pm.save(str(path))
    print(f"  [屏幕] {path.name}  {pm.width()}x{pm.height()}  "
          f"dpr={pm.devicePixelRatio()}  裁剪自 ({geo.x()},{geo.y()}) {geo.width()}x{geo.height()}")


def on_render_finished(_stats):
    state["render_done"] = True


win.controller.renderFinished.connect(on_render_finished)


def stage1():
    print(f"玻璃材质: {win.glass_active} — {win.glass_note}")
    print(f"窗口: hwnd={hwnd}  几何={win.frameGeometry().getRect()}  "
          f"DPR={win.devicePixelRatioF()}")
    waited = 0
    while not state["render_done"] and waited < 15000:
        app.processEvents()
        ctypes.windll.kernel32.Sleep(100)
        waited += 100
    st = win.controller.stats
    print(f"渲染完成: {state['render_done']} (等待 {waited} ms) · "
          f"峰值 {st.peak_db:+.2f} dBFS · 有效值 {st.rms_db:+.2f} dBFS · 耗时 {st.seconds:.2f}s")
    QTimer.singleShot(800, stage2)


def stage2():
    grab_qt("01_overview")
    grab_screen("01_overview")
    win._navigate("composer")
    QTimer.singleShot(600, stage2b)


def stage2b():
    grab_qt("02_composer")
    win._navigate("overview")
    QTimer.singleShot(500, stage3b)


def stage3b():
    """放大截取一条轨道行，用来核对小尺寸控件的可读性。"""
    from PySide6.QtCore import QRect, Qt

    pm = win.grab()
    dpr = pm.devicePixelRatio() or 1.0
    strip = win.overview._strips[0]
    tl = strip.mapTo(win, strip.rect().topLeft())
    # grab() 返回的是物理像素，需要按 dpr 换算
    region = QRect(int(tl.x() * dpr), int(tl.y() * dpr),
                   int(strip.width() * dpr), int(strip.height() * dpr))
    crop = pm.copy(region)
    # 放大 2 倍看清细节
    zoom = crop.scaled(crop.width() * 2, crop.height() * 2,
                       Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.FastTransformation)
    path = OUT / "04_track_row_zoom.png"
    zoom.save(str(path))
    print(f"  [放大] {path.name}  {zoom.width()}x{zoom.height()}")
    QTimer.singleShot(300, stage4)


def stage4():
    win.controller.play()
    QTimer.singleShot(1600, stage5)


def stage5():
    grab_qt("03_playing")
    grab_screen("03_playing")
    st = win.controller.player.snapshot()
    print(f"播放中: playing={st.playing} 位置={st.position:.2f}s "
          f"峰值={st.peak_db:+.1f}dB 有效值={st.rms_db:+.1f}dB")
    win.controller.stop()
    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0003)   # 取消置顶
    QTimer.singleShot(300, finish)


def finish():
    win.controller.shutdown()
    app.quit()


QTimer.singleShot(1000, stage1)
QTimer.singleShot(40000, app.quit)
app.exec()
print("结束")
