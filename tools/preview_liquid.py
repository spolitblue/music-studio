"""液态玻璃视觉验证：把环境背景 + 玻璃面板合成成一张预览图。"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from app import theme
from app.widgets.ambient import ambient, render_ambient
from app.widgets.glass import GlassStyle, paint_bloom, paint_glass
from app.widgets.liquid import LiquidParams, liquid_fill

OUT = _ROOT / "_shots"
OUT.mkdir(exist_ok=True)

W, H = 1100, 620
print("=" * 70)
print("环境背景")
print("=" * 70)
t0 = time.perf_counter()
bg = render_ambient(W, H)
print(f"  生成 {bg.width()}x{bg.height()}  {time.perf_counter() - t0:.3f}s")

ambient._pixmap = bg
t0 = time.perf_counter()
blurred = ambient.blurred(bg.size(), theme.finish.blur)
print(f"  预模糊（全窗口，只做一次）  {time.perf_counter() - t0:.3f}s")

canvas = QImage(W, H, QImage.Format.Format_RGB888)
canvas.fill(Qt.GlobalColor.black)
p = QPainter(canvas)
p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
p.drawPixmap(0, 0, bg)

panels = [
    # (x, y, w, h, radius, 说明)
    (40, 40, 420, 150, 26, "大面板"),
    (480, 40, 300, 150, 26, "中面板"),
    (40, 220, 240, 90, 45, "药丸形"),
    (300, 220, 200, 90, 20, "小面板"),
    (40, 340, 520, 110, 24, "宽面板"),
]

print()
print("=" * 70)
print("合成各面板")
print("=" * 70)
total = 0.0
for x, y, w, h, r, name in panels:
    from PySide6.QtCore import QRect

    t0 = time.perf_counter()
    src = ambient.crop_with_margin(QRect(x, y, w, h), blurred=True)
    glass = liquid_fill(src, w, h, r, LiquidParams.from_finish(theme.finish), preblurred=True)
    dt = time.perf_counter() - t0
    total += dt

    painter_rect = QRectF(x, y, w, h)
    # 面板外圈的泛光（选中/强调面才会用，这里演示效果）
    paint_bloom(p, painter_rect, r, theme.accents.blue_light, strength=0.22, spread=7)
    paint_glass(p, painter_rect, GlassStyle(
        radius=r, liquid_mix=1.0, specular=1.0, outline=1.0, grain=0.6,
    ), glass)
    print(f"  {name:<8} {w:4d}x{h:<4d}  {dt * 1000:6.1f} ms")

print(f"  合计 {total * 1000:.0f} ms（实际运行时只在尺寸变化时付一次，之后缓存复用）")

p.end()
path = OUT / "liquid_panels.png"
canvas.save(str(path))
print(f"\n  预览已保存: {path}")
