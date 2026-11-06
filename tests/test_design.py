"""冒烟测试：设计系统 / 图标 / 玻璃绘制 / 平台能力 是否通路。

不弹窗口 —— 用 offscreen 平台跑，可安全在无人值守时执行。
"""

import os
import sys

# --- 项目根路径引导（本文件位于子目录，保证从任何 cwd 都能 import app.*）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap  # noqa: E402

app = QGuiApplication.instance() or QGuiApplication(sys.argv)

from app import icons, theme  # noqa: E402
from app.platform import win_glass  # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    mark = "OK  " if cond else "FAIL"
    if not cond:
        ok = False
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")


print("=" * 64)
print("1) 设计系统")
print("=" * 64)
check("字体解析", bool(theme.font_family()), theme.font_family())
f = theme.type_scale.large_title.font()
check("大标题负字距", f.letterSpacing() < 0, f"letterSpacing={f.letterSpacing():.2f}")
f2 = theme.type_scale.caption2.font()
check("小字正字距", f2.letterSpacing() > 0, f"letterSpacing={f2.letterSpacing():.2f}")
check("三层材质有序", theme.materials.panel.alphaF() < theme.materials.control.alphaF(),
      f"panel={theme.materials.panel.alphaF():.3f} < control={theme.materials.control.alphaF():.3f}")

p = theme.squircle(QRectF(0, 0, 200, 80), 16)
check("squircle 路径非空", not p.isEmpty(), f"{p.elementCount()} 段")
check("squircle 有缓存", theme.squircle(QRectF(0, 0, 200, 80), 16) is p)

# 形状正确性：填充覆盖率应接近"圆角矩形"的理论面积
#   普通圆角矩形 = W·H − (4−π)r²  ；n=4 的超椭圆角比圆更饱满，略大于此值
_poly = p.toFillPolygon()
_xs = [_poly.at(i).x() for i in range(_poly.count())]
_ys = [_poly.at(i).y() for i in range(_poly.count())]
check("squircle 落在包围盒内",
      min(_xs) >= -0.6 and max(_xs) <= 200.6 and min(_ys) >= -0.6 and max(_ys) <= 80.6,
      f"x[{min(_xs):.1f},{max(_xs):.1f}] y[{min(_ys):.1f},{max(_ys):.1f}]")
_area_ratio = abs(p.toFillPolygon().boundingRect().width() * p.toFillPolygon().boundingRect().height())
check("squircle 包围盒尺寸正确", abs(_area_ratio - 200 * 80) < 1.0, f"{_area_ratio:.0f} vs 16000")

print()
print("=" * 64)
print("2) 弹簧动效")
print("=" * 64)
import math  # noqa: E402

from app.motion import SPRING_BOUNCY, SPRING_DEFAULT  # noqa: E402


def peak_overshoot(sp):
    """阻尼谐振子的理论最大过冲 = exp(-πζ/√(1-ζ²))。"""
    z = sp.zeta
    if z >= 1.0:
        return 0.0
    return math.exp(-math.pi * z / math.sqrt(1.0 - z * z))


check("弹簧终点收敛", abs(SPRING_DEFAULT.value_at(5.0) - 1.0) < 0.01)

# 过冲必须出现在理论峰值时刻 t = π/ωd
t_peak = math.pi / (SPRING_DEFAULT.omega0 * math.sqrt(1 - SPRING_DEFAULT.zeta ** 2))
got = SPRING_DEFAULT.value_at(t_peak) - 1.0
want = peak_overshoot(SPRING_DEFAULT)
check("默认弹簧过冲 = 理论值", abs(got - want) < 0.002,
      f"实测 {got * 100:.2f}% / 理论 {want * 100:.2f}%")

b_peak = SPRING_BOUNCY.value_at(
    math.pi / (SPRING_BOUNCY.omega0 * math.sqrt(1 - SPRING_BOUNCY.zeta ** 2))) - 1.0
check("弹跳档过冲更明显", b_peak > 0.03, f"{b_peak * 100:.2f}%")
check("落定时间合理", 0.05 < SPRING_DEFAULT.settle_time() < 2.0,
      f"{SPRING_DEFAULT.settle_time():.3f}s")

print()
print("=" * 64)
print("3) 图标系统 (Twemoji)")
print("=" * 64)
all_names = icons.names()
check("图标总数", len(all_names) >= 120, f"{len(all_names)} 个")
pm = icons.pixmap("play", 32)
check("play 渲染", not pm.isNull() and pm.width() >= 32, f"{pm.width()}x{pm.height()}")
check("中文名可读", bool(icons.label("piano")), f"piano → {icons.label('piano')}")
tinted = icons.pixmap("play", 32, tint=theme.accents.blue)
check("单色化", not tinted.isNull())
ai = icons.app_icon(128)
check("应用图标合成", not ai.isNull() and not ai.pixmap(128).isNull())
bg = icons.badge_pixmap("piano", 40)
check("徽标合成", not bg.isNull())

print()
print("=" * 64)
print("4) 玻璃绘制")
print("=" * 64)
img = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)
img.fill(Qt.GlobalColor.transparent)
painter = QPainter(img)
from app.widgets.glass import GlassStyle, paint_glass  # noqa: E402

_rect = QRectF(8, 8, 304, 184)
paint_glass(painter, _rect, GlassStyle(radius=16))
painter.end()
# 统计非透明像素，确认真的画上了东西
ptr = img.constBits()
import numpy as np  # noqa: E402

arr = np.frombuffer(ptr, dtype=np.uint8, count=320 * 200 * 4).reshape(200, 320, 4)
nonzero = int((arr[..., 3] > 0).sum())

# 理论：n=4 超椭圆角面积系数 = Γ(1.25)²/Γ(1.5) ≈ 0.92714，每角损失 (1−0.92714)·r²
_theory_px = _rect.width() * _rect.height() - 4 * (1 - 0.92714) * 16 ** 2
check("玻璃覆盖率符合几何预期", abs(nonzero - _theory_px) / _theory_px < 0.01,
      f"实测 {nonzero} px / 理论 {_theory_px:.0f} px "
      f"({nonzero / (320 * 200) * 100:.2f}% 画面占比)")

# 形状必须精确落在给定矩形内 —— 之前这里有个斜切 bug，会挖掉大块三角
_ys, _xs = np.nonzero(arr[..., 3] > 0)
check("玻璃未越界", _xs.min() >= 7 and _xs.max() <= 312 and _ys.min() >= 7 and _ys.max() <= 192,
      f"x[{_xs.min()},{_xs.max()}] y[{_ys.min()},{_ys.max()}]")

print()
print("=" * 64)
print("5) 模糊（真·背景虚化）")
print("=" * 64)
from app.widgets.glass import blur_pixmap  # noqa: E402

src = QPixmap(64, 64)
src.fill(Qt.GlobalColor.transparent)
p2 = QPainter(src)
p2.fillRect(0, 0, 32, 64, theme.accents.orange)
p2.fillRect(32, 0, 32, 64, theme.accents.blue)
p2.end()
import time  # noqa: E402

t0 = time.perf_counter()
blurred = blur_pixmap(src, 16.0)
dt = time.perf_counter() - t0
check("模糊输出有效", not blurred.isNull())
check("模糊速度可接受", dt < 0.5, f"{dt * 1000:.1f}ms (64x64, r=16, 3-pass)")

print()
print("=" * 64)
print("6) Windows 平台能力")
print("=" * 64)
check("DWM 可用", win_glass.AVAILABLE, win_glass.REASON)
check("系统透明效果开启", win_glass.transparency_enabled())
check("系统动画开启", win_glass.animations_enabled())

print()
print("=" * 64)
print("7) 批处理换行符（防「双击打不开」事故复发）")
print("=" * 64)
# cmd.exe 的批处理解析器要求 CRLF。LF 换行的 .bat 会被从任意位置劈开，变成
# 一堆 "'orlevel' 不是内部或外部命令"，双击后窗口一闪就没 —— 用户看到的只是
# "这个文件打不开"。而 write 工具默认写 LF，所以这条断言必须常驻。
from pathlib import Path  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_bats = sorted(p for p in _ROOT.rglob("*.bat") if ".venv" not in p.parts)
check("找到批处理文件", len(_bats) > 0, f"{len(_bats)} 个")
for _b in _bats:
    _raw = _b.read_bytes()
    _lone = _raw.replace(b"\r\n", b"").count(b"\n")
    check(f"{_b.name} 为 CRLF", _lone == 0,
          "OK" if _lone == 0 else f"{_lone} 处孤立 LF → 跑 tools/fix_bat_eol.py")

print()
print("=" * 64)
print("全部通过" if ok else "存在失败项")
print("=" * 64)
sys.exit(0 if ok else 1)
