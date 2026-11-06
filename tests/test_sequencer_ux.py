"""音序器的 10 个易用性细节验证。"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.audio import demo
from app.audio.project import drum_to_pitch
from app.ui.main_window import MainWindow

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def ev(kind, x, y, btn, buttons, mod=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), btn, buttons, mod)


class Rig:
    """一个装好工程与网格的测试台。"""

    def __init__(self):
        self.win = MainWindow(demo.demo_project())
        self.win.resize(1280, 800)
        self.win.show()
        self.win._navigate("sequencer")
        self.page = self.win.sequencer
        self.grid = self.page.grid
        self.proj = self.win.project

    def total(self):
        return sum(len(t.notes) for t in self.proj.tracks)

    def drum_lane(self):
        return next(i for i, l in enumerate(self.grid.lanes) if l.is_drum)

    def synth_lane(self):
        return next(i for i, l in enumerate(self.grid.lanes) if not l.is_drum)

    def cell(self, li, step, frac=0.5):
        lane = self.grid.lanes[li]
        x = self.grid._x_for_step(step) + 3
        y = self.grid._lane_top(li) + lane.height * frac
        return x, y

    def press(self, x, y, btn=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.grid.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, x, y, btn, btn, mod))

    def move(self, x, y, buttons=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.grid.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, x, y,
                                    Qt.MouseButton.NoButton, buttons, mod))

    def release(self, x, y, btn=Qt.MouseButton.LeftButton):
        self.grid.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, x, y, btn,
                                       Qt.MouseButton.NoButton))

    def click(self, x, y, btn=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.press(x, y, btn, mod)
        self.release(x, y, btn)

    def close(self):
        self.win.controller.shutdown()


rig = Rig()
g = rig.grid

print("=" * 70)
print("1) 输入时试听")
print("=" * 70)
from app.ui.pages.sequencer import _render_audition

dl = g.lanes[rig.drum_lane()]
buf = _render_audition(dl, drum_to_pitch(dl.piece), 0.85)
check("鼓件试听能渲染出音频", buf is not None and buf.size > 0,
      f"{buf.shape if buf is not None else None}")
sl = g.lanes[rig.synth_lane()]
sbuf = _render_audition(sl, 60, 0.85)
check("旋律试听能渲染出音频", sbuf is not None and sbuf.size > 0,
      f"{sbuf.shape if sbuf is not None else None}")
if buf is not None:
    check("试听缓冲是立体声且非静音",
          buf.shape[0] == 2 and float(abs(buf).max()) > 0.001,
          f"峰值 {float(abs(buf).max()):.3f}")

print()
print("=" * 70)
print("2) 撤销 / 重做")
print("=" * 70)
# 先清空这条鼓件行 —— 示范曲里本来就有音符，直接点会点到已有的音（那是删除语义）
li = rig.drum_lane()
g._selected_lane = li
g.clear_selected_lane()
n0 = rig.total()
x, y = rig.cell(li, 70)
rig.click(x, y)
n1 = rig.total()
check("编辑后可撤销", rig.win.controller.can_undo and n1 == n0 + 1, f"{n0} → {n1}")
rig.win.controller.undo()
check("撤销还原音符数", rig.total() == n0, f"{rig.total()}")
rig.win.controller.redo()
check("重做恢复编辑", rig.total() == n1, f"{rig.total()}")
rig.win.controller.undo()      # 回到干净状态
g.clear_selected_lane()

# 一整笔拖动只应占一步撤销（同样先清空，否则拖过已有音符会被跳过）
g.clear_selected_lane()
n_before = rig.total()
u_before = len(rig.win.controller._undo)
x0, y0 = rig.cell(li, 80)
rig.press(x0, y0)
for s in range(81, 90):
    xx, yy = rig.cell(li, s)
    rig.move(xx, yy)
rig.release(*rig.cell(li, 89))
added = rig.total() - n_before
pushed = len(rig.win.controller._undo) - u_before
check("拖一整笔刷出多个音符", added == 10, f"{added} 个（拖了 10 格）")
check("一整笔只占一步撤销", pushed == 1, f"压了 {pushed} 步")
rig.win.controller.undo()

print()
print("=" * 70)
print("3) Shift + 上下拖 = 调力度")
print("=" * 70)
si = rig.synth_lane()
lane = g.lanes[si]
x, y = rig.cell(si, 100, 0.5)
rig.click(x, y)
pitch = lane.pitch_at(lane.height * 0.5)
note = lane.track.note_at(100, pitch)
check("先放一个音符", note is not None)
if note is not None:
    v0 = note.velocity
    sx = g._note_rect(si, note).center().x()
    sy = g._note_rect(si, note).center().y()
    rig.press(sx, sy, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    rig.move(sx, sy - 40, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    rig.release(sx, sy - 40)
    check("上拖提高力度", note.velocity > v0, f"{v0:.2f} → {note.velocity:.2f}")
    check("力度上限被钳在 1.0", note.velocity <= 1.0, f"{note.velocity:.2f}")

    rig.press(sx, sy, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    rig.move(sx, sy + 400, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    rig.release(sx, sy + 400)
    check("下拖降低力度且有下限", 0.05 <= note.velocity < 0.5, f"{note.velocity:.2f}")

print()
print("=" * 70)
print("4) 拖音符右缘 = 调时值")
print("=" * 70)
x, y = rig.cell(si, 105, 0.5)
rig.click(x, y)
pitch2 = lane.pitch_at(lane.height * 0.5)
note2 = lane.track.note_at(105, pitch2)
check("先放一个音符", note2 is not None)
if note2 is not None:
    L0 = note2.length
    r = g._note_rect(si, note2)
    rig.press(r.right() - 1.0, r.center().y())
    rig.move(r.right() - 1.0 + 3 * g.step_w, r.center().y())
    rig.release(r.right() - 1.0 + 3 * g.step_w, r.center().y())
    check("右拖拉长时值", note2.length == L0 + 3, f"{L0} → {note2.length}")

print()
print("=" * 70)
print("5) Alt + 拖动 = 框选，Delete 删除")
print("=" * 70)
a = g._x_for_step(10)
b = g._x_for_step(40)
ya = g._lane_top(si)
yb = ya + lane.height
rig.press(a, ya, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier)
rig.move(b, yb, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier)
rig.release(b, yb)
sel = g.selection
check("框选产生了选区", sel is not None, f"{sel}")
if sel is not None:
    check("选区步范围正确", sel[2] in range(9, 12) and sel[3] in range(39, 42),
          f"步 {sel[2]}–{sel[3]}")

    picked = len(g._notes_in_selection())
    check("选区内确实有音符", picked > 0, f"{picked} 个")
    before = rig.total()
    g.editStarted.emit("删除")
    g.delete_selection()
    check("Delete 删掉了选区内音符", rig.total() == before - picked,
          f"{before} → {rig.total()}")
    rig.win.controller.undo()

g.select_all()
check("Ctrl+A 全选", g.selection == (0, len(g.lanes) - 1, 0, rig.proj.total_steps - 1))
g.clear_selection()
check("Esc 清空选区", g.selection is None)

print()
print("=" * 70)
print("6) 复制 / 粘贴 / 复制小节")
print("=" * 70)
spb = rig.proj.steps_per_bar
# 把播放头放在第 2 小节开头，然后复制这一小节
rig.page.seekRequested.emit(rig.proj.step_time(spb))
rig.win.controller.seek(rig.proj.step_time(spb))
g.copy_selection()
clip_n = len(g._clipboard)
check("无选区时退化为复制当前小节", clip_n > 0, f"剪贴板 {clip_n} 个音符")

before = rig.total()
g.editStarted.emit("粘贴")
g.paste()
check("粘贴到播放头增加了音符", rig.total() > before, f"{before} → {rig.total()}")
rig.win.controller.undo()

# 复制小节
rig.win.controller.seek(rig.proj.step_time(0))
before = rig.total()
g.editStarted.emit("复制小节")
g.duplicate_bar()
check("Ctrl+D 复制小节到下一小节", rig.total() > before, f"{before} → {rig.total()}")
rig.win.controller.undo()

print()
print("=" * 70)
print("7) 播放头自动跟随")
print("=" * 70)
rig.page.scroll.horizontalScrollBar().setValue(0)
rig.win.controller.seek(0.0)
rig.page.set_position(0.0, True)
v_start = rig.page.scroll.horizontalScrollBar().value()
# 跳到后半段 —— 应该滚动过去
late = rig.proj.total_steps * 0.7 * rig.proj.seconds_per_step
rig.page.set_position(late, True)
v_late = rig.page.scroll.horizontalScrollBar().value()
check("播放头推进时视口跟着滚", v_late > v_start, f"{v_start} → {v_late}")

v_before_stop = rig.page.scroll.horizontalScrollBar().value()
rig.page.set_position(late + 1.0, False)
check("暂停时不自动跟随", rig.page.scroll.horizontalScrollBar().value() == v_before_stop)

print()
print("=" * 70)
print("8) 缩放（含边界钳制与累积）")
print("=" * 70)
rig.page._zoom_level = 1.0
g.set_step_width(15.0)
rig.page._zoom(1.25)
a = g.step_w
rig.page._zoom(1.25)
b = g.step_w
check("连续放大累积", b > a, f"{a:.1f} → {b:.1f}")
for f, lo, hi in ((0.01, 6.0, 8.0), (100.0, 33.0, 35.0)):
    rig.page._zoom_level = 1.0
    rig.page._zoom(f)
    check(f"缩放 {f} 被钳制", lo <= g.step_w <= hi, f"step_w={g.step_w:.1f}")

print()
print("=" * 70)
print("8b) 滚轮缩放与工具条缩放必须共用同一个真值源")
print("=" * 70)
# 回归自鼓机模块的同构缺陷：滚轮改了 step_w，页面 _zoom_level 却停在旧值，
# 导致「点放大反而变小」。
rig.page._zoom_level = 1.0
g.set_step_width(16.0)
before = g.step_w
g.set_step_width(before * 1.5)              # 模拟 Ctrl+滚轮放大
after_wheel = g.step_w
check("滚轮放大后 _zoom_level 同步了",
      abs(rig.page._zoom_level - after_wheel / 15.0) < 1e-6,
      f"_zoom_level={rig.page._zoom_level:.4f} 期望 {after_wheel / 15.0:.4f}")

rig.page._zoom(1.25)                        # 再点工具条「放大」
check("滚轮放大后点放大按钮只会更大（不会反而变小）",
      g.step_w > after_wheel, f"{after_wheel:.2f} → {g.step_w:.2f}")

# 顶到上限后再点放大，不该跌回去
g.set_step_width(34.0)
rig.page._zoom(1.25)
check("已在上限时点放大仍停在上限", abs(g.step_w - 34.0) < 1e-6, f"{g.step_w:.2f}")
rig.page._zoom(0.8)
check("从上限点缩小按真实步宽算（不是按过期的 _zoom_level）",
      abs(g.step_w - 27.2) < 0.01, f"{g.step_w:.2f} 期望 27.20")

# 边界钳制仍然有效
g.set_step_width(1000.0)
check("上限钳制", abs(g.step_w - 34.0) < 1e-6, f"{g.step_w:.2f}")
g.set_step_width(0.1)
check("下限钳制", abs(g.step_w - 7.0) < 1e-6, f"{g.step_w:.2f}")

print()
print("=" * 70)
print("9) 折叠没有音符的鼓件行")
print("=" * 70)
full = len(g.lanes)
g.set_fold_drums(True)
folded = len(g.lanes)
check("折叠后泳道减少", folded < full, f"{full} → {folded}")
used = {n.pitch for t in rig.proj.tracks if t.kind == "drum" for n in t.notes}
check("折叠后只剩有音符的鼓件", folded <= len(used) + 4,
      f"{folded} 行 / 用到 {len(used)} 件")
check("鼓件行都用到了", all(
    drum_to_pitch(l.piece) in used for l in g.lanes if l.is_drum))
g.set_fold_drums(False)
check("展开可恢复", len(g.lanes) == full, f"{len(g.lanes)}")

print()
print("=" * 70)
print("10) 悬停读数")
print("=" * 70)
lane = g.lanes[rig.synth_lane()]
top = g._lane_top(rig.synth_lane())
hi_pitch = lane.pitch_at(1.0)
lo_pitch = lane.pitch_at(lane.height - 1.0)
check("读数用的音高与绘制用的音高一致（上高下低）", hi_pitch > lo_pitch,
      f"{hi_pitch} > {lo_pitch}")
# 读数还会显示已有音符的力度与时值 —— 验证取的是同一个音符
from app.audio.project import Note

lane.track.add_note(Note(step=3, pitch=hi_pitch, length=2, velocity=0.42))
found = lane.track.note_at(3, hi_pitch)
check("读数能取到音符的力度/时值",
      found is not None and abs(found.velocity - 0.42) < 1e-9 and found.length == 2,
      f"vel={found.velocity} len={found.length}" if found else "取不到")

print()
print("=" * 70)
print("11) 增量编辑后全曲仍可渲染")
print("=" * 70)
from app.audio import render

audio, stats = render.render_project(rig.proj)
check("改动后渲染无 NaN", bool(__import__("numpy").isfinite(audio).all()))
check("改动后不削波", stats.peak <= 1.0, f"峰值 {stats.peak_db:+.2f} dBFS")

rig.close()

print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
