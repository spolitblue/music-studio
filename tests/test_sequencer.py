"""音序器验证：泳道构建、坐标换算、增删改、小节收缩。"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- 项目根路径引导 ---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.audio import demo
from app.audio.project import drum_to_pitch
from app.ui.pages.sequencer import (
    HEADER_W, RULER_H, StepGrid, build_lanes,
)

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def press(widget, x, y, button=Qt.MouseButton.LeftButton):
    e = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
                    button, button, Qt.KeyboardModifier.NoModifier)
    widget.mousePressEvent(e)


def move(widget, x, y, button=Qt.MouseButton.LeftButton):
    e = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
                    Qt.MouseButton.NoButton, button, Qt.KeyboardModifier.NoModifier)
    widget.mouseMoveEvent(e)


def release(widget, x, y):
    e = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier)
    widget.mouseReleaseEvent(e)


print("=" * 70)
print("1) 泳道构建")
print("=" * 70)
project = demo.demo_project()
lanes = build_lanes(project)
drums = [l for l in lanes if l.is_drum]
synths = [l for l in lanes if not l.is_drum]
print(f"  鼓件行 {len(drums)} 条，旋律行 {len(synths)} 条")
check("鼓轨展开成多行", len(drums) >= 8, f"{len(drums)} 条")
check("旋律轨各占一行", len(synths) == 4, f"{len(synths)} 条")
check("鼓件行音高互不重复",
      len({drum_to_pitch(l.piece) for l in drums}) == len(drums))
for l in synths:
    check(f"「{l.label}」音域覆盖八度以上", l.hi_pitch - l.lo_pitch >= 12,
          f"{l.lo_pitch}–{l.hi_pitch}")

print()
print("=" * 70)
print("2) 坐标换算（最容易错的地方）")
print("=" * 70)
grid = StepGrid(project)
grid.rebuild()
grid.set_step_width(16.0)

# 步 → x → 步 应当往复一致
roundtrip_ok = True
for s in (0, 1, 7, 16, 63, 127):
    x = grid._x_for_step(s) + 2
    if grid._step_at_x(x) != s:
        roundtrip_ok = False
        print(f"      步 {s} ↔ x 往返失败")
check("步 ↔ x 坐标往返一致", roundtrip_ok)

# 小节尺 y 不应落在任何泳道
check("小节尺区域不命中泳道", grid._lane_at_y(RULER_H - 2) is None)
# 表头右侧第一像素应落在第 0 步
check("网格左边界对应第 0 步", grid._step_at_x(HEADER_W + 0.5) == 0)

# 旋律行：y 越高音高越高
sl = next(l for l in grid.lanes if not l.is_drum)
top_pitch = sl.pitch_at(1.0)
bot_pitch = sl.pitch_at(sl.height - 1.0)
check("旋律行上方=高音、下方=低音", top_pitch > bot_pitch,
      f"上 {top_pitch} / 下 {bot_pitch}")
check("音高落在设定音域内",
      sl.lo_pitch <= bot_pitch <= top_pitch <= sl.hi_pitch,
      f"[{sl.lo_pitch},{sl.hi_pitch}] 内")

print()
print("=" * 70)
print("3) 点击增删")
print("=" * 70)
# 找一条鼓件行，点一下应加、再点一下应删
di = next(i for i, l in enumerate(grid.lanes) if l.is_drum)
dl = grid.lanes[di]
dy = grid._lane_top(di) + dl.height / 2
pitch = drum_to_pitch(dl.piece)
before = len(dl.track.notes)

press(grid, grid._x_for_step(4) + 3, dy)
mid = len(dl.track.notes)
check("左键点亮", mid == before + 1, f"{before} → {mid}")
check("音符落在正确的步与音高",
      dl.track.note_at(4, pitch) is not None,
      f"step=4 pitch={pitch}")

press(grid, grid._x_for_step(4) + 3, dy)
after = len(dl.track.notes)
check("再点一次擦除", after == before, f"{mid} → {after}")

# 右键擦除
press(grid, grid._x_for_step(9) + 3, dy)     # 先点亮
press(grid, grid._x_for_step(9) + 3, dy, Qt.MouseButton.RightButton)
check("右键擦除", dl.track.note_at(9, pitch) is None)

print()
print("=" * 70)
print("4) 拖动刷音符")
print("=" * 70)
# 先在 20 步放一个音，然后从 18 拖到 24 —— 经过 20 时不应把它擦掉
press(grid, grid._x_for_step(20) + 3, dy)
press(grid, grid._x_for_step(18) + 3, dy)
for s in range(19, 25):
    move(grid, grid._x_for_step(s) + 3, dy, Qt.MouseButton.LeftButton)
release(grid, grid._x_for_step(24) + 3, dy)

steps_hit = sorted(n.step for n in dl.track.notes if n.pitch == pitch)
check("拖动连续刷出音符", all(s in steps_hit for s in (18, 19, 22, 23, 24)),
      f"落在 {steps_hit}")
check("拖动经过已有音符不会擦掉它", 20 in steps_hit)

print()
print("=" * 70)
print("5) 旋律行写旋律")
print("=" * 70)
si = next(i for i, l in enumerate(grid.lanes) if not l.is_drum)
sm = grid.lanes[si]
top = grid._lane_top(si)
trk = sm.track
# 该列可能本来就有音符（示范曲），先记下已有的，只比较**本次新加**的
existing = {n.pitch for n in trk.notes if n.step == 30}
n0 = len(trk.notes)
# 在同一列的不同高度点三下 → 应该得到三个不同音高
for frac in (0.15, 0.5, 0.85):
    press(grid, grid._x_for_step(30) + 3, top + sm.height * frac)
added = sorted((p for p in {n.pitch for n in trk.notes if n.step == 30} - existing),
               reverse=True)   # 降序：上面那次点击应给出最高的音
check("同一列可写出多个音高", len(added) == 3, f"新增音高 {added}（原有 {sorted(existing)}）")
check("音高随点击高度单调（上=高音）", added[0] > added[1] > added[2],
      f"{added[0]} > {added[1]} > {added[2]}")
check("音符总数增加了 3", len(trk.notes) == n0 + 3)

print()
print("=" * 70)
print("6) 工具条操作")
print("=" * 70)
from app.ui.pages.sequencer import SequencerPage

page = SequencerPage(project)
page.rebuild()
bars0 = project.bars
steps0 = project.total_steps
page._change_bars(-1)
check("减少小节生效", project.bars == bars0 - 1, f"{bars0} → {project.bars}")
check("总步数同步缩短", project.total_steps == steps0 - project.steps_per_bar)
out_of_range = [n for t in project.tracks for n in t.notes if n.step >= project.total_steps]
check("越界音符已清理（不留幽灵音符）", not out_of_range,
      f"{len(out_of_range)} 个越界")

# 清空当前行
grid._selected_lane = next(i for i, l in enumerate(grid.lanes) if l.is_drum)
cl = grid.lanes[grid._selected_lane]
cpitch = drum_to_pitch(cl.piece)
grid.clear_selected_lane()
check("清空当前行只影响该件",
      all(n.pitch != cpitch for n in cl.track.notes),
      f"该件剩 {sum(1 for n in cl.track.notes if n.pitch == cpitch)} 个")

print()
print("=" * 70)
print("7) 缩放的边界")
print("=" * 70)
# 注意读 page.grid —— page 有自己的 grid 实例，读外部那个变量测不到东西
# 重置**真实步宽**而不是 _zoom_level —— 修复后 _zoom 以 grid.step_w 为基准累积
# （这正是「点放大反而变小」那个 bug 的修法），所以基准要设在真值源上
for f, expect_lo, expect_hi in ((0.05, 6.0, 8.0), (1.0, 14.0, 16.0), (99.0, 33.0, 35.0)):
    page.grid.set_step_width(15.0)
    page._zoom(f)
    sw = page.grid.step_w
    check(f"缩放系数 {f} → step_w 落在 [{expect_lo},{expect_hi}]",
          expect_lo <= sw <= expect_hi, f"step_w={sw:.1f}")
# 连续缩放应真正累积，而不是每次都被钳到同一个值
page.grid.set_step_width(15.0)
page._zoom(1.25)
a = page.grid.step_w
page._zoom(1.25)
b = page.grid.step_w
check("连续放大确实累积", b > a, f"{a:.1f} → {b:.1f}")

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
