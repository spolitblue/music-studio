"""弹簧动效验证：物理积分 vs 解析解、速度继承、生命周期安全。"""

import gc
import math
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

from PySide6.QtCore import QElapsedTimer, QTimer, Property
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)

from app.motion import (
    SPRING_BOUNCY, SPRING_DEFAULT, SPRING_GENTLE, SPRING_SNAPPY, _Animator, _active,
)

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


class Probe(QWidget):
    def __init__(self):
        super().__init__()
        self._v = 0.0
        self._anim = _Animator(self)

    def _get(self):
        return self._v

    def _set(self, v):
        self._v = float(v)

    value = Property(float, _get, _set)


print("=" * 70)
print("1) 物理积分 vs 解析阶跃响应")
print("=" * 70)

for name, sp in (("snappy", SPRING_SNAPPY), ("default", SPRING_DEFAULT),
                 ("gentle", SPRING_GENTLE), ("bouncy", SPRING_BOUNCY)):
    # 用固定步长模拟，便于与解析解逐点对比
    w = Probe()
    anim = anim_obj = _Animator(w)
    a = anim.to("value", 1.0, sp)

    dt = 1.0 / 120.0
    t = 0.0
    samples = []
    while t < sp.settle_time() * 2.0:
        a.step(dt)
        t += dt
        samples.append((t, w.property("value")))

    final = float(w.property("value"))
    peak = max(v for _t, v in samples)
    # 解析解的过冲
    z = sp.zeta
    theory_peak = 1.0 + (math.exp(-math.pi * z / math.sqrt(1 - z * z)) if z < 1 else 0.0)

    check(f"{name:8s} 收敛到目标", abs(final - 1.0) < 1e-3, f"final={final:.6f}")
    check(f"{name:8s} 过冲符合理论", abs(peak - theory_peak) < 0.02,
          f"实测 {peak:.4f} / 理论 {theory_peak:.4f}")

print()
print("=" * 70)
print("2) 可中断性与速度继承（软毕达哥拉斯 —— 缓动曲线做不到这条）")
print("=" * 70)

w = Probe()
anim = _Animator(w)

# 先朝 1.0 弹，跑到一半时改朝 0.0
a1 = anim.to("value", 1.0, SPRING_DEFAULT)
vel_before = 0.0
for _ in range(12):
    a1.step(1.0 / 120.0)
    vel_before = a1.v
mid_value = float(w.property("value"))

# 中断：改目标为 0
a2 = anim.to("value", 0.0, SPRING_DEFAULT)
carried = a2.v

check("中断时从当前位置出发", abs(a2.x - mid_value) < 1e-9,
      f"x={a2.x:.5f} 实际={mid_value:.5f}")
check("中断时继承了速度", abs(carried - vel_before) < 1e-9,
      f"继承 v={carried:.4f}（中断前 v={vel_before:.4f}）")
check("速度方向正确（正向上冲）", carried > 0)

# 因为它带着向上的速度，理应继续上冲一段才回落 —— 这就是"手感"的来源
peak_after = mid_value
for _ in range(200):
    a2.step(1.0 / 120.0)
    peak_after = max(peak_after, float(w.property("value")))
check("继承速度后仍会继续上冲", peak_after > mid_value + 0.01,
      f"中断点 {mid_value:.4f} → 峰值 {peak_after:.4f}")

for _ in range(600):
    a2.step(1.0 / 120.0)
check("最终停在 0", abs(float(w.property("value"))) < 1e-3,
      f"{float(w.property('value')):.6f}")

print()
print("=" * 70)
print("3) 真实事件循环（临时对象、创建/销毁、无一崩溃）")
print("=" * 70)

widgets = []
for i in range(40):
    w = Probe()
    w._anim.to("value", 1.0, (SPRING_SNAPPY, SPRING_DEFAULT, SPRING_GENTLE, SPRING_BOUNCY)[i % 4])
    widgets.append(w)
# 中途丢掉一半，强制回收，检验陈旧引用不会炸
for w in widgets[::2]:
    w.deleteLater()
gc.collect()
widgets = widgets[1::2]

print(f"  已启动 40 条弹簧，丢弃 20 条；存活 {len(widgets)} 个")
print(f"  当前活跃弹簧数: {len(_active)}")


def done():
    vals = [round(float(w.property("value")), 4) for w in widgets]
    good = all(abs(v - 1.0) < 0.02 for v in vals)
    check("全部收敛到 1.0", good, str(vals[:8]) + " …")
    check("活跃队列已清空", len(_active) == 0, f"剩余 {len(_active)}")
    app.quit()


QTimer.singleShot(2200, done)
app.exec()

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
