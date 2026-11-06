"""钢琴卷帘（PianoRollPage）的交互、数据一致性与绘制验证。

测试原则
--------
"界面有反应"和"工程数据真的变了"是两件事。这个文件里**每一条编辑动作
都断言工程里的 notes 真的变了** —— 只改界面不改数据是最容易蒙混过关的
一类假实现，而它在实际使用中等于什么都没做。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- 项目根路径引导（本文件位于子目录）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

app = QApplication([])

from app.audio import demo
from app.audio.project import Note, Project, Track
from app.ui.pages.piano_roll import (
    PianoRollPage, is_black_key, note_covering, pitch_span, quantize_unit,
    render_preview, snap_to_unit,
)

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def ev(kind, x, y, btn, buttons, mod=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), btn, buttons, mod)


class FakeController:
    """只提供 push_undo 的最小控制器。

    真实的 AppController 很重（渲染线程、播放器），而页面只用到 push_undo /
    undo / redo —— 另外再单独用真控制器验证一次接线，两边都不放过。
    """

    def __init__(self):
        self.labels = []
        self.can_undo = False
        self.can_redo = False

    def push_undo(self, label="编辑"):
        self.labels.append(label)
        self.can_undo = True

    def pop_undo(self, label=""):
        """和 AppController.pop_undo 同一份契约的轻量版。

        labels 这个列表同时充当"撤销栈"：这样"栈深有没有变"可以直接数出来。
        """
        if not self.labels:
            return False
        if label and self.labels[-1] != label:
            return False                    # 栈顶不是这一笔 → 宁可留着也不误删别人
        self.labels.pop()
        self.can_undo = bool(self.labels)
        return True


class Rig:
    """装好工程、页面与鼠标事件模拟的测试台。"""

    def __init__(self, project=None, size=(1280, 780)):
        self.project = project if project is not None else demo.demo_project()
        self.host = QWidget()
        self.host.resize(*size)
        lay = QVBoxLayout(self.host)
        lay.setContentsMargins(0, 0, 0, 0)
        self.page = PianoRollPage(self.project)
        lay.addWidget(self.page)
        self.host.show()
        app.processEvents()
        self.grid = self.page.grid
        self.lane = self.page.velocity
        self.ctrl = FakeController()
        self.page.bind(self.ctrl)
        self.edited_count = 0
        self.notes_edited_count = 0
        self.page.edited.connect(self._on_edited)
        self.page.notesEdited.connect(self._on_notes_edited)
        self.auditions = []
        self.page.auditioned.connect(lambda p, peak: self.auditions.append((p, peak)))

    def _on_edited(self):
        self.edited_count += 1

    def _on_notes_edited(self):
        self.notes_edited_count += 1

    # -- 鼠标 ------------------------------------------------------------

    def press(self, x, y, btn=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.grid.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, x, y, btn, btn, mod))

    def move(self, x, y, buttons=Qt.MouseButton.LeftButton,
             mod=Qt.KeyboardModifier.NoModifier):
        self.grid.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, x, y,
                                    Qt.MouseButton.NoButton, buttons, mod))

    def release(self, x, y, btn=Qt.MouseButton.LeftButton,
                mod=Qt.KeyboardModifier.NoModifier):
        self.grid.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, x, y, btn,
                                       Qt.MouseButton.NoButton, mod))

    def click(self, x, y, btn=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.press(x, y, btn, mod)
        self.release(x, y, btn, mod)

    # -- 坐标 ------------------------------------------------------------

    def cell(self, step, pitch):
        """某个 (步, 半音) 格子的中心点。"""
        g = self.grid
        return (g.x_for_step(step) + 1.5, g.y_for_pitch(pitch) + g.row_h / 2.0)

    def note_center(self, note):
        r = self.grid.note_rect(note)
        return (r.center().x(), r.center().y())

    @property
    def track(self):
        return self.page.current_track()

    def clear_track(self):
        self.track.notes.clear()
        self.page.refresh_notes()

    def scroll_to_start(self):
        self.page.scroll.horizontalScrollBar().setValue(0)
        app.processEvents()


# ==========================================================================
print("=" * 72)
print("1) 页面构造与绘制")
print("=" * 72)
# ==========================================================================
rig = Rig()
page, g = rig.page, rig.grid

check("示范工程里选到了旋律轨（kind=='synth'）",
      rig.track is not None and rig.track.kind == "synth",
      rig.track.name if rig.track else "无")
check("不是空状态", not page.is_empty_state())
check("轨道芯片数量 = 旋律轨数量",
      len(page._chips) == len([t for t in rig.project.tracks if t.kind == "synth"]),
      f"{len(page._chips)} 个")

# 布局不变量。这一组是**看图之后补上的**：最初工具条忘了 addWidget，
# 于是它以一个默认尺寸浮在页面左上角压住小节尺 —— 而 grab() 照样成功，
# 只有真的渲染出来看才发现。所以这里把"进布局"和"三块视图对齐"钉死。
bar = page._bar
check("工具条真的进了布局（横向铺满，不是浮在左上角的孤儿控件）",
      bar.width() > page.width() * 0.9 and bar.parent() is page,
      f"bar {bar.width()}x{bar.height()} / page {page.width()}x{page.height()}")
kb_top = page.keyboard.mapTo(page, QPoint(0, 0)).y()
scroll_top = page.scroll.mapTo(page, QPoint(0, 0)).y()
check("键盘顶边与网格顶边同高（行才能一一对齐）", kb_top == scroll_top,
      f"keyboard {kb_top} vs scroll {scroll_top}")
kb_bottom = page.keyboard.mapTo(page, QPoint(0, 0)).y() + page.keyboard.height()
scroll_bottom = page.scroll.mapTo(page, QPoint(0, 0)).y() + page.scroll.height()
check("键盘底边与网格底边同高（行数不会多出或少掉）", kb_bottom == scroll_bottom,
      f"keyboard {kb_bottom} vs scroll {scroll_bottom}")
check("力度条与网格列共用同一个 x 原点（列才对得齐）",
      page.velocity.mapTo(page, QPoint(0, 0)).x()
      == page.scroll.viewport().mapTo(page, QPoint(0, 0)).x(),
      f"lane {page.velocity.mapTo(page, QPoint(0, 0)).x()} "
      f"vs viewport {page.scroll.viewport().mapTo(page, QPoint(0, 0)).x()}")
check("页面能绘制（grab 不报错）", not page.grab().isNull(),
      f"{page.grab().width()}x{page.grab().height()}")
check("网格能绘制", not g.grab().isNull(), f"{g.width()}x{g.height()}")
check("键盘、尺子、力度条各自 grab 成功",
      not page.keyboard.grab().isNull()
      and not page.ruler.grab().isNull()
      and not rig.lane.grab().isNull())

# 悬停读数：鼠标一动就该看得见"这一格是什么音"，等 tooltip 弹出来太慢
hx, hy = rig.cell(4, (g.lo_pitch + g.hi_pitch) // 2)
rig.move(hx, hy, Qt.MouseButton.NoButton)
check("悬停读数给出音名与步号", "步 5" in g.readout(), g.readout())
check("悬停状态下网格能绘制（读数是最后画的一层）", not g.grab().isNull())

# 音高范围：按音符算 + 上下各 4 个半音余量 + 至少 2 个八度
pitches = [n.pitch for n in rig.track.notes]
span = g.hi_pitch - g.lo_pitch
check("音域按该轨音符算并留了 4 个半音余量",
      g.lo_pitch <= min(pitches) - 4 and g.hi_pitch >= max(pitches) + 4,
      f"音符 {min(pitches)}–{max(pitches)} → 网格 {g.lo_pitch}–{g.hi_pitch}")
check("音域至少覆盖两个八度", span >= 24, f"{span} 个半音")

print()
print("=" * 72)
print("2) 空状态：没有任何旋律轨也不许崩")
print("=" * 72)
empty_proj = Project(name="只有鼓", bpm=120.0, bars=4)
empty_proj.add_track(Track(name="鼓组", kind="drum"))
rig_empty = Rig(empty_proj)
pe, ge = rig_empty.page, rig_empty.grid
check("切换到了空状态页", pe.is_empty_state())
check("空状态下网格没有轨道", ge.track is None)
check("空状态能绘制", not pe.grab().isNull())
print("  空轨道上执行全部编辑动作，只看是否抛异常：")
try:
    ge.select_all()
    ge.clear_selection()
    ge.delete_selected()
    ge.transpose(12)
    ge.quantize(16)
    ge.set_position(1.0, True)
    pe.refresh_notes()
    pe.rebuild()
    pe.set_position(2.0, False)
    rig_empty.click(*rig_empty.cell(4, 60))
    rig_empty.click(4.0, 4.0, Qt.MouseButton.RightButton)
    pe._select_all()
    pe._delete_selected()
    pe._transpose(12)
    pe._quantize()
    pe._fit_width()
    pe.keyboard.notePressed.emit(60)
    pe.cycle_track(1)
    check("空轨道上所有编辑动作都不崩", True)
except Exception as exc:                                  # noqa: BLE001
    check("空轨道上所有编辑动作都不崩", False, f"{type(exc).__name__}: {exc}")
check("空轨道上画不出音符", ge.draw_note(0, 60) is None and len(empty_proj.tracks[0].notes) == 0)

print()
print("=" * 72)
print("3) 左键画音符：数据真的写进 project，且画的同时试听")
print("=" * 72)
rig.clear_track()
rig.scroll_to_start()
base_pitch = (g.lo_pitch + g.hi_pitch) // 2
n0 = len(rig.track.notes)
x, y = rig.cell(8, base_pitch)
rig.press(x, y)
n_mid = len(rig.track.notes)
rig.release(x, y)
drawn = rig.track.note_at(8, base_pitch)
check("按下时就写入了音符（不是松手才画）", n_mid == n0 + 1, f"{n0} → {n_mid}")
check("音符落在正确的步与音高上", drawn is not None,
      f"note_at(8, {base_pitch})")
check("默认时值 1 步", drawn is not None and drawn.length == 1,
      f"length={drawn.length if drawn else None}")
check("绘制动作发了 edited 信号", rig.edited_count > 0, f"{rig.edited_count} 次")
check("绘制前压了撤销点", rig.ctrl.labels and rig.ctrl.labels[-1] == "绘制音符",
      str(rig.ctrl.labels[-1:]))
check("画的时候试听了该音高",
      page.last_audition is not None and page.last_audition[0] == base_pitch,
      str(page.last_audition))
check("试听缓冲非静音", page.last_audition is not None and page.last_audition[1] > 0.001,
      f"峰值 {page.last_audition[1]:.4f}" if page.last_audition else "无")

# 直接验证渲染路径本身
buf = render_preview(rig.track, base_pitch, 0.85)
import numpy as np                                                # noqa: E402

check("render_synth_note 产出立体声非静音缓冲",
      buf is not None and buf.ndim == 2 and buf.shape[0] == 2
      and float(np.abs(buf).max()) > 0.001,
      f"shape={None if buf is None else buf.shape} 峰值={0.0 if buf is None else float(np.abs(buf).max()):.4f}")
check("长音符覆盖范围内也能命中（note_covering）",
      note_covering(rig.track, 8, base_pitch) is drawn)

# 拖动连画：划过 5 格应该多出 5 个音符
x0, y0 = rig.cell(20, base_pitch)
rig.press(x0, y0)
for s in range(21, 26):
    rig.move(*rig.cell(s, base_pitch))
rig.release(*rig.cell(25, base_pitch))
painted = [s for s in range(20, 26) if rig.track.note_at(s, base_pitch) is not None]
check("按住拖动连续画音符", len(painted) == 6, f"步 {painted}")
check("一整笔拖动只占一步撤销",
      rig.ctrl.labels.count("绘制音符") == 2, f"{rig.ctrl.labels}")

print()
print("=" * 72)
print("4) 左键点已有音符 = 删除；右键 = 擦除")
print("=" * 72)
before = len(rig.track.notes)
rx, ry = rig.note_center(drawn)
rig.click(rx, ry)
check("左键点已有音符把它删掉", len(rig.track.notes) == before - 1,
      f"{before} → {len(rig.track.notes)}")
check("删除后 note_at 取不到了", rig.track.note_at(8, base_pitch) is None)

rig.click(*rig.cell(30, base_pitch))
made = rig.track.note_at(30, base_pitch)
check("重新画一个用于右键测试", made is not None)
cx, cy = rig.note_center(made)
rig.click(cx, cy, Qt.MouseButton.RightButton)
check("右键擦除", rig.track.note_at(30, base_pitch) is None)

# 右键拖动连续擦除
for s in range(20, 26):
    rig.track.remove_note(s, base_pitch)
for s in range(20, 26):
    rig.grid.draw_note(s, base_pitch, audition_it=False)
n_before = len(rig.track.notes)
rig.press(*rig.cell(20, base_pitch), Qt.MouseButton.RightButton)
for s in range(21, 26):
    rig.move(*rig.cell(s, base_pitch), Qt.MouseButton.RightButton)
rig.release(*rig.cell(25, base_pitch), Qt.MouseButton.RightButton)
check("右键拖动连续擦除", len(rig.track.notes) == n_before - 6,
      f"{n_before} → {len(rig.track.notes)}")

print()
print("=" * 72)
print("5) 拖音符中间 = 改音高（钢琴卷帘的关键操作）")
print("=" * 72)
rig.clear_track()
rig.scroll_to_start()
p0 = (g.lo_pitch + g.hi_pitch) // 2
g.draw_note(12, p0, audition_it=False)
note = rig.track.note_at(12, p0)
check("先放一个音符", note is not None, f"pitch={p0}")

nx, ny = rig.note_center(note)
# 改音高之前记一次计数：改完必须**确实发了 edited**，否则编排者不会重渲染音频，
# 用户拖完听到的还是旧音高 —— 只有"pitch 变了"这一条断言是抓不到这个 bug 的。
edits_before_0 = rig.edited_count
notes_edited_before_0 = rig.notes_edited_count
rig.press(nx, ny)
rig.move(nx, ny - 3 * g.row_h)              # 往上拖 3 行 = 升 3 个半音
rig.release(nx, ny - 3 * g.row_h)
check("拖中间往上 = 升 3 个半音",
      rig.track.note_at(12, p0 + 3) is not None,
      f"note_at(12, {p0 + 3})={'有' if rig.track.note_at(12, p0 + 3) else '无'}")
check("原音高上不再有音符", rig.track.note_at(12, p0) is None)
check("步没有跟着变", len([n for n in rig.track.notes if n.step == 12]) == 1)
check("改音高的撤销标签正确", "移动音高" in rig.ctrl.labels, str(rig.ctrl.labels[-2:]))
check("改音高发了 edited（编排者据此重渲染音频）",
      rig.edited_count > edits_before_0,
      f"改前 {edits_before_0} → 改后 {rig.edited_count}")
check("改音高发了 notesEdited（兼容音序器命名的那条接线）",
      rig.notes_edited_count > notes_edited_before_0,
      f"改前 {notes_edited_before_0} → 改后 {rig.notes_edited_count}")
check("改音高只发一次 edited（拖动中不该每挪一行都发）",
      rig.edited_count - edits_before_0 == 1,
      f"{rig.edited_count - edits_before_0} 次")

moved = rig.track.note_at(12, p0 + 3)
nx, ny = rig.note_center(moved)
rig.press(nx, ny)
rig.move(nx, ny + 5 * g.row_h)              # 往下拖 5 行
rig.release(nx, ny + 5 * g.row_h)
check("往下拖 = 降 5 个半音", rig.track.note_at(12, p0 + 3 - 5) is not None,
      f"期待 pitch={p0 - 2}")
# 拖到网格音域之外要被钳住（音符绝不能拖到看不见的地方去）
note2 = rig.track.note_at(12, p0 - 2)
nx, ny = rig.note_center(note2)
rig.press(nx, ny)
rig.move(nx, ny + 4000)
rig.release(nx, ny + 4000)
lowest = min(n.pitch for n in rig.track.notes)
check("向下拖出音域时被钳在网格下沿（音符不会消失在视野外）",
      lowest == g.lo_pitch, f"最低 {lowest}，网格下沿 {g.lo_pitch}")

print()
print("=" * 72)
print("6) 拖音符右缘 = 改时值")
print("=" * 72)
rig.clear_track()
g.draw_note(40, p0, audition_it=False)
n = rig.track.note_at(40, p0)
r = g.note_rect(n)
L0 = n.length
rig.press(r.right() - 1.0, r.center().y())
rig.move(r.right() - 1.0 + 3 * g.step_w, r.center().y())
rig.release(r.right() - 1.0 + 3 * g.step_w, r.center().y())
check("右拖拉长时值", n.length == L0 + 3, f"{L0} → {n.length}")

r = g.note_rect(n)
rig.press(r.right() - 1.0, r.center().y())
rig.move(r.right() - 1.0 - 99 * g.step_w, r.center().y())
rig.release(r.right() - 1.0 - 99 * g.step_w, r.center().y())
check("时值下限被钳在 1 步", n.length == 1, f"length={n.length}")

r = g.note_rect(n)
rig.press(r.right() - 1.0, r.center().y())
rig.move(r.right() - 1.0 + 5000, r.center().y())
rig.release(r.right() - 1.0 + 5000, r.center().y())
check("时值不会越过工程末尾",
      n.step + n.length <= rig.project.total_steps,
      f"step={n.step} len={n.length} total={rig.project.total_steps}")

print()
print("=" * 72)
print("7) Shift + 上下拖 = 改力度")
print("=" * 72)
rig.clear_track()
g.draw_note(60, p0, audition_it=False)
n = rig.track.note_at(60, p0)
n.velocity = 0.5
r = g.note_rect(n)
sx, sy = r.left() + 2.0, r.center().y()
rig.press(sx, sy, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
rig.move(sx, sy - 45, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
rig.release(sx, sy - 45)
check("上拖提高力度", n.velocity > 0.5, f"0.50 → {n.velocity:.2f}")
check("力度上限 1.0", n.velocity <= 1.0, f"{n.velocity:.3f}")

rig.press(sx, sy, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
rig.move(sx, sy + 4000, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
rig.release(sx, sy + 4000)
check("下拖降低力度且有下限", 0.05 <= n.velocity < 0.3, f"{n.velocity:.3f}")
check("Shift 拖动没有把音符删掉（模式没串）", rig.track.note_at(60, p0) is n)

print()
print("=" * 72)
print("8) 量化到 1/16")
print("=" * 72)
# 默认工程 steps_per_beat=4 → 一个 step 就是 1/16，量化是恒等变换（不该损坏数据）
check("1/16 网格在 steps_per_beat=4 时等于 1 步",
      quantize_unit(rig.project, 16) == 1, f"unit={quantize_unit(rig.project, 16)}")
n_before = len(rig.track.notes)
steps_before = sorted(n.step for n in rig.track.notes)
g.quantize(16)
check("量化是恒等变换：步不变、音符不丢",
      sorted(n.step for n in rig.track.notes) == steps_before
      and len(rig.track.notes) == n_before,
      f"{steps_before} → {sorted(n.step for n in rig.track.notes)}")
check("四舍五入不是银行家舍入", snap_to_unit(3, 2) == 4 and snap_to_unit(5, 2) == 6
      and snap_to_unit(1, 2) == 2, f"{snap_to_unit(3, 2)},{snap_to_unit(5, 2)},{snap_to_unit(1, 2)}")

# 1/32 精度的工程里，1/16 网格 = 2 个 step —— 这时量化才真的吸附
fine = Project(name="1/32 精度", bpm=120.0, steps_per_beat=8, beats_per_bar=4, bars=4)
fine.add_track(Track(name="主音", kind="synth", notes=[
    Note(step=1, pitch=60, length=1, velocity=0.8),
    Note(step=5, pitch=62, length=1, velocity=0.8),
    Note(step=7, pitch=64, length=1, velocity=0.8),
]))
check("1/32 工程里 1/16 网格 = 2 个 step",
      quantize_unit(fine, 16) == 2, f"unit={quantize_unit(fine, 16)}")
rig_fine = Rig(fine)
rig_fine.page._quantize()
got = sorted(n.step for n in rig_fine.track.notes)
check("量化把音符吸附到正确的步", got == [2, 6, 8], f"[1,5,7] → {got}")
check("量化前压了撤销点", rig_fine.ctrl.labels[-1:] == ["量化到 1/16"],
      str(rig_fine.ctrl.labels[-1:]))
check("量化后音高/力度没被动过",
      sorted((n.pitch, round(n.velocity, 3)) for n in rig_fine.track.notes)
      == [(60, 0.8), (62, 0.8), (64, 0.8)])

# 量化撞车：两个音符被吸到同一格（step 1 和 2 都吸到 2），保留响的那个
crash = Project(name="撞车", bpm=120.0, steps_per_beat=8, bars=2)
crash.add_track(Track(name="主音", kind="synth", notes=[
    Note(step=1, pitch=60, length=1, velocity=0.2),
    Note(step=2, pitch=60, length=1, velocity=0.9),
]))
rig_crash = Rig(crash)
rig_crash.page._quantize()
survivors = rig_crash.track.notes
check("量化撞车时保留力度大的那个",
      len(survivors) == 1 and survivors[0].step == 2 and survivors[0].velocity > 0.8,
      f"{[(n.step, n.pitch, round(n.velocity, 2)) for n in survivors]}")

print()
print("=" * 72)
print("9) 全部选中 / 删除选中 / 整体升降八度")
print("=" * 72)
rig.clear_track()
for i, st in enumerate((4, 12, 20)):
    g.draw_note(st, p0 + i, audition_it=False)
total = len(rig.track.notes)
check("准备三个音符", total == 3, f"{total} 个")

page._select_all()
check("全部选中", len(g.selected) == 3, f"选中 {len(g.selected)} 个")
check("力度条的目标跟着选区走", rig.lane.target_note() is not None)

rig.page._transpose(12)
ups = sorted(n.pitch for n in rig.track.notes)
check("整体升八度真的改了 pitch", ups == [p0 + 12, p0 + 13, p0 + 14], f"{ups}")
check("升八度前压了撤销点", "升八度" in rig.ctrl.labels, str(rig.ctrl.labels[-1:]))
rig.page._transpose(-12)
check("整体降八度还原", sorted(n.pitch for n in rig.track.notes) == [p0, p0 + 1, p0 + 2],
      f"{sorted(n.pitch for n in rig.track.notes)}")

# 只移调选中的一个
g.clear_selection()
one = rig.track.note_at(12, p0 + 1)
g.set_primary(one)
rig.page._transpose(12)
check("只移调选中的音符",
      rig.track.note_at(12, p0 + 13) is not None
      and rig.track.note_at(4, p0) is not None
      and rig.track.note_at(20, p0 + 2) is not None,
      f"{sorted((n.step, n.pitch) for n in rig.track.notes)}")

page._select_all()
before = len(rig.track.notes)
rig.page._delete_selected()
check("删除选中：project 里的音符真的没了", len(rig.track.notes) == before - 3,
      f"{before} → {len(rig.track.notes)}")
check("删除后选区被清空", len(g.selected) == 0)

print()
print("=" * 72)
print("10) 力度条：显示选中音符的力度，可拖动修改")
print("=" * 72)
rig.clear_track()
rig.scroll_to_start()
g.draw_note(24, p0, audition_it=False)
n = rig.track.note_at(24, p0)
n.velocity = 0.4
g.set_primary(n)
check("力度条的目标就是选中音符", rig.lane.target_note() is n)
check("力度条能按 x 找到那根柱子", rig.lane.note_under(g.x_for_step(24) + 2.0) is n)

lx = g.x_for_step(24) + 2.0            # 横向偏移当前为 0，直接对齐网格坐标
rig.lane.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, lx, 2.0,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
check("拖到顶 = 力度拉满", n.velocity > 0.9, f"{n.velocity:.2f}")
rig.lane.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, lx, 60.0,
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
check("往下拖降低力度", n.velocity < 0.3, f"{n.velocity:.2f}")
rig.lane.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, lx, 60.0,
                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
check("力度下限 0.05", n.velocity >= 0.05, f"{n.velocity:.3f}")
check("力度条改动压了撤销点", "调整力度" in rig.ctrl.labels, str(rig.ctrl.labels[-1:]))

print()
print("=" * 72)
print("11) 播放头与自动跟随")
print("=" * 72)
rig.scroll_to_start()
page.set_position(0.0, True)
v_start = page.scroll.horizontalScrollBar().value()
check("播放头画在网格里（位置能换算成步）",
      abs(g.playhead_step) < 1e-6 and page.ruler._playhead == g.playhead_step)
late = rig.project.total_steps * 0.75 * rig.project.seconds_per_step
page.set_position(late, True)
v_late = page.scroll.horizontalScrollBar().value()
check("播放头推进时视口跟着滚", v_late > v_start, f"{v_start} → {v_late}")
v_hold = page.scroll.horizontalScrollBar().value()
page.set_position(late + 1.0, False)
check("暂停时不自动跟随",
      page.scroll.horizontalScrollBar().value() == v_hold,
      f"{v_hold}")
page.set_position(0.0, False)

print()
print("=" * 72)
print("12) 点左侧钢琴键盘试听：发声、且播放头不动")
print("=" * 72)
seeks = []
page.seekRequested.connect(lambda s: seeks.append(s))
kb = page.keyboard
hit_y = kb.y_for_pitch(p0) + kb._row_h / 2.0
kb.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, 10.0, hit_y,
                      Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
kb.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, 10.0, hit_y,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
check("点键盘试听了正确的音高",
      page.last_audition is not None and page.last_audition[0] == p0,
      str(page.last_audition))
check("键盘试听缓冲非静音", page.last_audition[1] > 0.001, f"峰值 {page.last_audition[1]:.4f}")
check("试听没有请求移动播放头", len(seeks) == 0, f"seek 次数 {len(seeks)}")
check("键盘行映射正确", kb.pitch_at_y(kb.y_for_pitch(p0) + 1.0) == p0)

print()
print("=" * 72)
print("13) 轨道切换：音域重算、芯片跟随、选区清空")
print("=" * 72)
page.rebuild()
tracks = [t for t in rig.project.tracks if t.kind == "synth"]
check("找到多条旋律轨", len(tracks) >= 2, f"{len(tracks)} 条")
if rig.track.notes:
    g.set_primary(rig.track.notes[0])
page.select_track(tracks[1])
check("切换后编辑的是新轨道", page.current_track() is tracks[1], page.current_track().name)
check("切换后音域按新轨道重算",
      (g.lo_pitch, g.hi_pitch) == pitch_span(tracks[1]),
      f"{g.lo_pitch}–{g.hi_pitch} vs {pitch_span(tracks[1])}")
check("切换后选区被清空（选区不该跨轨道残留）", len(g.selected) == 0)
check("芯片选中态跟随",
      page._chips[tracks[1].id].isChecked() and not page._chips[tracks[0].id].isChecked())
page.cycle_track(1)
check("`]` 能循环换轨", page.current_track() is not tracks[1] or len(tracks) == 1,
      page.current_track().name)

print()
print("=" * 72)
print("14) 黑键/白键行底色不同 + 三种网格线明度分级（真的看像素）")
print("=" * 72)
plain = Project(name="空谱", bpm=120.0, bars=8)
plain.add_track(Track(name="空旋律", kind="synth"))
rig_plain = Rig(plain)
img = rig_plain.grid.grab().toImage()
gg = rig_plain.grid
check("画布尺寸覆盖整首曲子", img.width() == int(gg.x_for_step(plain.total_steps)),
      f"{img.width()}px / {plain.total_steps} 步")


def row_lum(pitch):
    y = int(gg.y_for_pitch(pitch) + gg.row_h / 2.0)
    vals = []
    for x in range(4, img.width() - 4, 7):
        c = img.pixelColor(x, y)
        vals.append(0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF())
    return sum(vals) / max(1, len(vals))


c_lum = row_lum(60)          # C  = 白键行
cs_lum = row_lum(61)         # C# = 黑键行
check("黑键行底色确实比白键行暗",
      cs_lum < c_lum - 0.004, f"C={c_lum:.4f} C#={cs_lum:.4f} 差 {c_lum - cs_lum:.4f}")
check("上下相邻的 C 与 C# 能分开（差值不是浮点噪声）", abs(c_lum - cs_lum) > 0.004,
      f"差 {abs(c_lum - cs_lum):.4f}")

y_probe = int(gg.y_for_pitch(60) + gg.row_h / 2.0)
step_x = int(gg.x_for_step(66))          # 66 % 4 = 2 → 只是步线
beat_x = int(gg.x_for_step(68))          # 68 % 4 = 0 且 % 16 = 4 → 拍线
bar_x = int(gg.x_for_step(64))           # 64 % 16 = 0 → 小节线
check("三个探针都在画布内",
      max(step_x, beat_x, bar_x) < img.width(), f"{step_x}/{beat_x}/{bar_x} < {img.width()}")


def px_lum(x, y):
    c = img.pixelColor(int(x), int(y))
    return 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()


l_step, l_beat, l_bar = px_lum(step_x, y_probe), px_lum(beat_x, y_probe), px_lum(bar_x, y_probe)
check("步线 / 拍线 / 小节线三种明度递增",
      l_step < l_beat < l_bar,
      f"步 {l_step:.4f} < 拍 {l_beat:.4f} < 小节 {l_bar:.4f}")

print()
print("=" * 72)
print("15) 与真的 AppController 接线：撤销点与撤销后的轨道重解析")
print("=" * 72)
from app.ui.controller import AppController                      # noqa: E402

live_proj = demo.demo_project()
rig_live = Rig(live_proj)
ctrl = AppController(live_proj)
rig_live.page.bind(ctrl)
rig_live.clear_track()
live_track = rig_live.page.current_track()
live_id = live_track.id
n0 = len(live_track.notes)
xp = (rig_live.grid.lo_pitch + rig_live.grid.hi_pitch) // 2
rig_live.click(*rig_live.cell(6, xp))
check("真控制器：编辑后 can_undo 为真", ctrl.can_undo)
check("真控制器：撤销标签正确", ctrl.undo_label() == "绘制音符", ctrl.undo_label())
check("音符确实进了工程（不只是界面）", len(live_proj.track_by_id(live_id).notes) == n0 + 1,
      f"{n0} → {len(live_proj.track_by_id(live_id).notes)}")
ctrl.undo()
rig_live.page.rebuild()
check("撤销后音符回到之前", len(live_proj.track_by_id(live_id).notes) == n0,
      f"{len(live_proj.track_by_id(live_id).notes)}")
check("撤销后页面重新解析到了活着的轨道对象（不是孤儿引用）",
      rig_live.page.current_track() is live_proj.track_by_id(live_id))
ctrl.redo()
rig_live.page.rebuild()
check("重做恢复编辑", len(live_proj.track_by_id(live_id).notes) == n0 + 1,
      f"{len(live_proj.track_by_id(live_id).notes)}")
ctrl.shutdown()

print()
print("=" * 72)
print("16) 编辑后整曲仍能渲染")
print("=" * 72)
from app.audio import render                                        # noqa: E402

audio, stats = render.render_project(rig.project)
check("改动后渲染无 NaN", bool(np.isfinite(audio).all()))
check("改动后不削波", stats.peak <= 1.0, f"峰值 {stats.peak_db:+.2f} dBFS")
check("改动后音频非静音", float(np.abs(audio).max()) > 0.001,
      f"峰值 {float(np.abs(audio).max()):.3f}")

print()
print("=" * 72)
print("17) select_track 对鼓轨设防：public 入口不能把音符写进鼓轨")
print("=" * 72)
# 同模块另外四条路径（_resolve_track / rebuild / cycle_track / 芯片条）都过滤了
# kind=='synth'，只有 select_track 这个 public 入口没过滤 —— 编排者按"当前轨道"
# 把一条鼓轨传进来时，网格会切到鼓轨上，接着画的音符就落进了鼓轨的数据里。
# 等下一次 refresh 界面会自愈，但那条脏音符已经留在鼓轨里了。
guard_proj = Project(name="鼓轨守卫", bpm=120.0, bars=4)
guard_lead = guard_proj.add_track(Track(name="主音", kind="synth",
                                        notes=[Note(step=0, pitch=60, length=1, velocity=0.8)]))
guard_kit = guard_proj.add_track(Track(name="kit", kind="drum",
                                       notes=[Note(step=0, pitch=36, length=1, velocity=0.9)]))
rig_guard = Rig(guard_proj)
pg, gg = rig_guard.page, rig_guard.grid
check("准备：初始编辑的是旋律轨", pg.current_track() is guard_lead,
      pg.current_track().name if pg.current_track() else "无")
pg.select_track(guard_kit)
check("select_track 拒绝鼓轨（kind != 'synth'）", pg.current_track() is not guard_kit,
      f"current={pg.current_track().name if pg.current_track() else None}")
check("被拒之后仍在编辑旋律轨", pg.current_track() is guard_lead)
drum_before = len(guard_kit.notes)
gg.draw_note(0, 64, audition_it=False)
check("音符没有写进鼓轨", len(guard_kit.notes) == drum_before,
      f"鼓轨 {drum_before} → {len(guard_kit.notes)}")
check("音符写进了旋律轨", guard_lead.note_at(0, 64) is not None)
pg.select_track(None)
check("传 None 也不会把网格清空（沿用原行为）", pg.current_track() is guard_lead)

print()
print("=" * 72)
print("18) 只有真的改了数据才压撤销点（否则用户连按撤销都是空步骤）")
print("=" * 72)
# 原来只要在音符中间按下就发 editStarted("移动音高")：松手走了删除分支、
# 或者拖出去又拖回原位（净变化为零），撤销栈里都会留下一个"按下去毫无反应"的
# 格子 —— 用户连按几次撤销，看到的是界面一动不动的几步。
noop_proj = Project(name="空手势", bpm=120.0, bars=4)
noop_track = noop_proj.add_track(Track(name="主音", kind="synth",
                                       notes=[Note(step=10, pitch=60, length=1, velocity=0.8)]))
rig_noop = Rig(noop_proj)
gn = rig_noop.grid
noop_note = noop_track.note_at(10, 60)
nr = gn.note_rect(noop_note)

k0 = len(rig_noop.ctrl.labels)
rig_noop.press(nr.right() - 1.0, nr.center().y())
rig_noop.release(nr.right() - 1.0, nr.center().y())
check("按一下音符右缘却没拖：时值没变", noop_note.length == 1, f"length={noop_note.length}")
check("按一下音符右缘却没拖：不压撤销点",
      len(rig_noop.ctrl.labels) == k0, str(rig_noop.ctrl.labels[k0:]))

k0 = len(rig_noop.ctrl.labels)
rig_noop.press(nr.left() + 2.0, nr.center().y(),
               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
rig_noop.release(nr.left() + 2.0, nr.center().y())
check("Shift 按一下音符却没拖：力度没变", abs(noop_note.velocity - 0.8) < 1e-9,
      f"{noop_note.velocity:.3f}")
check("Shift 按一下音符却没拖：不压撤销点",
      len(rig_noop.ctrl.labels) == k0, str(rig_noop.ctrl.labels[k0:]))

k0 = len(rig_noop.ctrl.labels)
rig_noop.click(*rig_noop.cell(30, 60), Qt.MouseButton.RightButton)
check("右键点在空格子上（什么都没擦掉）：不压撤销点",
      len(rig_noop.ctrl.labels) == k0, str(rig_noop.ctrl.labels[k0:]))

# 拖出去又拖回原位：净变化为零，不该在撤销栈里留格子，也不该惊动编排者
k0 = len(rig_noop.ctrl.labels)
e0 = rig_noop.edited_count
cx, cy = rig_noop.note_center(noop_note)
rig_noop.press(cx, cy)
rig_noop.move(cx, cy - 4 * gn.row_h)
rig_noop.move(cx, cy)
rig_noop.release(cx, cy)
check("拖出去又拖回原位：音高确实回到原处",
      noop_track.note_at(10, 60) is noop_note, f"pitch={noop_note.pitch}")
check("拖出去又拖回原位：不压撤销点",
      len(rig_noop.ctrl.labels) == k0, str(rig_noop.ctrl.labels[k0:]))
check("拖出去又拖回原位：不发 edited（数据没变不必重渲染）",
      rig_noop.edited_count == e0, f"{rig_noop.edited_count - e0} 次")

# 正面对照：真的拖走了，就压且只压一步 —— 修法不能把正常的撤销点也修没了
k0 = len(rig_noop.ctrl.labels)
e0 = rig_noop.edited_count
rig_noop.press(cx, cy)
rig_noop.move(cx, cy - 2 * gn.row_h)
rig_noop.release(cx, cy - 2 * gn.row_h)
check("真的拖走音高：只压一步撤销且标签正确",
      rig_noop.ctrl.labels[k0:] == ["移动音高"], str(rig_noop.ctrl.labels[k0:]))
check("真的拖走音高：发了 edited", rig_noop.edited_count == e0 + 1,
      f"{rig_noop.edited_count - e0} 次")

# 同一条线索在键盘路径上的翻版：没有选区时 Delete 什么都删不掉，不该占一格撤销
gn.clear_selection()
k0 = len(rig_noop.ctrl.labels)
gn.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                           Qt.KeyboardModifier.NoModifier))
check("没有选区时按 Delete：不压撤销点",
      len(rig_noop.ctrl.labels) == k0, str(rig_noop.ctrl.labels[k0:]))
gn.set_primary(noop_track.note_at(10, 62))
k0 = len(rig_noop.ctrl.labels)
gn.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                           Qt.KeyboardModifier.NoModifier))
check("有选区时按 Delete：压一步撤销且音符真的没了",
      rig_noop.ctrl.labels[k0:] == ["删除音符"]
      and noop_track.note_at(10, 62) is None, str(rig_noop.ctrl.labels[k0:]))

print()
print("=" * 72)
print("19) 悬停高亮：无条件跟随光标（与音序器 StepGrid 同一套语义）")
print("=" * 72)
# 高亮是"光标在这一格"，不是"这一格能不能落笔"。原来用 Track.note_at 做条件判断
# 就两头不讨好：长音符中段（真的落不了笔）闪出一格高亮像个空格子，起始格反倒
# 什么都没有。现在索性无条件画 —— 音序器页的 StepGrid._paint_hover 就是这么做的
# （sequencer.py:890 起），同一个软件里两个网格用两套悬停语义，用户会以为坏了。
hover_proj = Project(name="悬停高亮", bpm=120.0, bars=8)
hover_track = hover_proj.add_track(Track(name="长音符", kind="synth",
                                         notes=[Note(step=24, pitch=60, length=4,
                                                     velocity=0.85)]))
rig_h = Rig(hover_proj)
gh = rig_h.grid


def cell_lum(step, pitch, img):
    """算某个格子中央一小块的平均亮度（避开 1px 网格线，避免采样到线上）。"""
    x0 = int(gh.x_for_step(step)) + 3
    y0 = int(gh.y_for_pitch(pitch)) + 2
    w = max(2, int(gh.step_w) - 6)
    h = max(2, int(gh.row_h) - 4)
    tot = 0.0
    for yy in range(y0, y0 + h):
        for xx in range(x0, x0 + w):
            c = img.pixelColor(xx, yy)
            tot += 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return tot / float(w * h)


def grab_hover(step, pitch):
    gh._hover = None if step is None else (int(step), int(pitch))
    gh.update()
    return gh.grab().toImage()


img_off = grab_hover(None, 60)
img_mid = grab_hover(26, 60)          # 长音符（步 24–27）的中段
img_head = grab_hover(24, 60)         # 长音符的起始格
img_empty = grab_hover(40, 60)        # 真正的空格子
d_empty = cell_lum(40, 60, img_empty) - cell_lum(40, 60, img_off)
d_mid = cell_lum(26, 60, img_mid) - cell_lum(26, 60, img_off)
d_head = cell_lum(24, 60, img_head) - cell_lum(24, 60, img_off)
check("空格子上画悬停高亮", d_empty > 0.004, f"Δ={d_empty:.4f}")
check("被音符盖住的格子上也画悬停高亮（长音符中段）", d_mid > 0.004, f"Δ={d_mid:.4f}")
check("被音符盖住的格子上也画悬停高亮（长音符起始格）", d_head > 0.004, f"Δ={d_head:.4f}")
# "不区分空格子还是有音符"要能验证到：中段与起始格都是音符实体，两者叠加同一层
# 白色的量应该几乎相同（只差音符自身顶边那 1px 高光）。
check("悬停高亮不区分空格子还是有音符（两处叠加的量一致）",
      abs(d_mid - d_head) < 0.005, f"中段 Δ={d_mid:.4f} vs 起始 Δ={d_head:.4f}")
# 跨页面一致性：音序器 StepGrid._paint_hover 就是无条件画悬停格的
# （本文件之外，不便直接调它，所以只在这里留一句出处，见 sequencer.py:890）
check("悬停高亮的位置就是光标那一格（不是别的地方被点亮）",
      cell_lum(41, 60, img_empty) - cell_lum(41, 60, img_off) < 0.001,
      f"邻格 Δ={cell_lum(41, 60, img_empty) - cell_lum(41, 60, img_off):.4f}")

print()
print("=" * 72)
print("20) 力度条拖到一半收不到 mouseRelease 也必须通知（离开控件 / 窗口失焦）")
print("=" * 72)
# notesChanged 原来只在 mouseRelease 里发。鼠标拖出控件再松手、或者窗口失焦时
# release 永远不会到达：力度已经写进工程了，却没有任何通知 —— 编排者不会重新
# 渲染，用户听到的还是旧力度，而"边调边听"正是这条反馈的全部意义。
lane_proj = Project(name="力度通知", bpm=120.0, bars=4)
lane_track = lane_proj.add_track(Track(name="主音", kind="synth",
                                       notes=[Note(step=8, pitch=60, length=1,
                                                   velocity=0.4)]))
rig_lane = Rig(lane_proj)
gl, lane = rig_lane.grid, rig_lane.lane
lane_note = lane_track.note_at(8, 60)
gl.set_primary(lane_note)
lane_changes = []
lane.notesChanged.connect(lambda: lane_changes.append(1))
lx = gl.x_for_step(8) + 2.0
lane.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, lx, 8.0,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
after_press = len(lane_changes)
edits_after_press = rig_lane.edited_count
check("按下（还没松手）力度就已经改到工程里了", lane_note.velocity > 0.8,
      f"0.40 → {lane_note.velocity:.3f}")
# 只发 mouseMove、**故意不发 mouseRelease** —— 模拟鼠标离开控件 / 窗口失焦
lane.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, lx, 20.0,
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
check("mouseMove 阶段力度确实又改了", 0.6 < lane_note.velocity < 0.8,
      f"{lane_note.velocity:.3f}")
check("mouseMove 阶段就发了 notesChanged（不依赖 mouseRelease）",
      len(lane_changes) > after_press,
      f"按下后 {after_press} 次 → 移动后 {len(lane_changes)} 次")
check("mouseMove 阶段就发了 edited（编排者据此重渲染音频）",
      rig_lane.edited_count > edits_after_press,
      f"{rig_lane.edited_count - edits_after_press} 次")
check("mouseMove 阶段就压了撤销点", "调整力度" in rig_lane.ctrl.labels,
      str(rig_lane.ctrl.labels[-2:]))
lane.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, lx, 20.0,
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
check("松手之后拖动态被清干净", lane._drag is None)

print()
print("=" * 72)
print("21) 右键擦除必须认得长音符的中段（修悬停高亮时顺着同一条线索翻出来的）")
print("=" * 72)
# 模块顶部写着"擦除也要认得长音符"，但 erase_note 走的是 Track.remove_note ——
# 那个只匹配音符的**起始步**。于是右键点一个 4 步长音符的中段一点反应都没有；
# 而同族的悬停读数 / 拖动 / 点删都认得它，用户会以为"这个音删不掉"。
erase_proj = Project(name="擦长音符", bpm=120.0, bars=4)
erase_track = erase_proj.add_track(Track(name="长音", kind="synth",
                                         notes=[Note(step=20, pitch=60, length=4,
                                                     velocity=0.8)]))
rig_e = Rig(erase_proj)
ge = rig_e.grid
check("准备：一个 4 步长的音符", len(erase_track.notes) == 1)
check("erase_note 认得长音符中段（步 22 落在 20–23 里）",
      ge.erase_note(22, 60) is True, f"擦除后剩 {len(erase_track.notes)} 个音符")
check("中段擦除之后那个音符真的没了", len(erase_track.notes) == 0)

erase_track.add_note(Note(step=20, pitch=60, length=4, velocity=0.8))
rig_e.page.refresh_notes()
k0 = len(rig_e.ctrl.labels)
rig_e.click(*rig_e.cell(22, 60), Qt.MouseButton.RightButton)
check("右键点长音符中段能擦掉它（鼠标路径也要通）", len(erase_track.notes) == 0,
      f"剩 {len(erase_track.notes)} 个音符")
check("擦除真的发生了才压撤销点，标签正确",
      rig_e.ctrl.labels[k0:] == ["擦除音符"], str(rig_e.ctrl.labels[k0:]))

print()
print("=" * 72)
print("22) 真控制器端到端：拖音高一路走到编排者的 paramsChanged")
print("=" * 72)
# 缺陷①的实测证据里就有"controller.paramsChanged 0 次"。第 5 节只证明了页面自己
# 发了 edited，这里按 main_window._wire_page 的真实接线（edited → ctrl.touch(False)）
# 走完整条链路，确认编排者真的会重渲染音频；顺便验证撤销点拍到的确实是
# "改动前"的状态（③ 里那套"退回去压点再改回来"就靠这条兜底）。
live2_proj = demo.demo_project()
rig_live2 = Rig(live2_proj)
ctrl2 = AppController(live2_proj)
rig_live2.page.bind(ctrl2)
params = []
ctrl2.paramsChanged.connect(lambda: params.append(1))
rig_live2.page.edited.connect(lambda: ctrl2.touch(False))     # = 编排者的接线
rig_live2.clear_track()
live2_track = rig_live2.page.current_track()
live2_id = live2_track.id
pz = (rig_live2.grid.lo_pitch + rig_live2.grid.hi_pitch) // 2
rig_live2.grid.draw_note(20, pz, audition_it=False)           # 直接放，不经过鼠标
n_drawn = live2_proj.track_by_id(live2_id).note_at(20, pz)
check("端到端：先有一个音符", n_drawn is not None, f"pitch={pz}")
cx2, cy2 = rig_live2.note_center(n_drawn)
k2, e2 = len(params), rig_live2.edited_count
rig_live2.press(cx2, cy2)
rig_live2.move(cx2, cy2 - 2 * rig_live2.grid.row_h)
rig_live2.release(cx2, cy2 - 2 * rig_live2.grid.row_h)
check("端到端：拖完音高真的变了",
      live2_proj.track_by_id(live2_id).note_at(20, pz + 2) is not None)
check("端到端：页面发了 edited", rig_live2.edited_count > e2,
      f"{rig_live2.edited_count - e2} 次")
check("端到端：编排者收到 paramsChanged（据此重渲染音频）", len(params) > k2,
      f"{len(params) - k2} 次")
check("端到端：撤销标签是「移动音高」", ctrl2.undo_label() == "移动音高",
      ctrl2.undo_label())
ctrl2.undo()
rig_live2.page.rebuild()
left = sorted((n.step, n.pitch) for n in live2_proj.track_by_id(live2_id).notes)
check("端到端：撤销把音高退回改动前（撤销点拍的是改动前的工程）",
      live2_proj.track_by_id(live2_id).note_at(20, pz) is not None
      and live2_proj.track_by_id(live2_id).note_at(20, pz + 2) is None, str(left))
ctrl2.shutdown()

print()
print("=" * 72)
print("23) 移调被占位挡住 = 什么都没发生：抹掉那个空撤销点")
print("=" * 72)
# 页面在移调**之前**压撤销点（推测性的），可移调常常一个音符都动不了：目标音高
# 被另一个音符占着时 transpose() 会跳过它（宁可少移一个音，也不悄悄吃掉别人的音）。
# 于是栈里留下一个空条目 —— 用户按一次撤销"没反应"。现在用 AppController
# 新加的 pop_undo(标签) 抹掉它：带标签抹，万一栈顶不是这一步就不会误删别人的。


def blocked_pair(name):
    """同一拍上放 60 / 61 / 72：

    * 60 往上移半音 → 61 被占着，动不了；
    * 60 往上移八度 → 72 被占着，动不了（工具栏那两个按钮就是 ±12）。
    61 往上移半音 → 62 空着，是"真能移"的对照。
    """
    proj = Project(name=name, bpm=120.0, bars=4)
    trk = proj.add_track(Track(name="主音", kind="synth", notes=[
        Note(step=4, pitch=60, length=1, velocity=0.8),
        Note(step=4, pitch=61, length=1, velocity=0.8),
        Note(step=4, pitch=72, length=1, velocity=0.8),
    ]))
    return proj, trk


# --- 栈深：键盘路径与工具栏路径都不该让栈变深（用能数深度的轻量控制器）---
bproj, btrack = blocked_pair("占位挡移调·栈深")
rig_b = Rig(bproj)
gb = rig_b.grid
b60, b61 = btrack.note_at(4, 60), btrack.note_at(4, 61)
gb.set_primary(b60)
depth0 = len(rig_b.ctrl.labels)
gb.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                           Qt.KeyboardModifier.NoModifier))
check("↑ 移调被占位挡住（transpose 什么都没动）",
      btrack.note_at(4, 60) is b60 and btrack.note_at(4, 61) is b61)
check("↑ 移调没改动 → 撤销栈深度不变（空条目被抹掉）",
      len(rig_b.ctrl.labels) == depth0, f"{depth0} → {len(rig_b.ctrl.labels)}")
rig_b.page._transpose(12)                  # 升八度：72 也被占着，同样动不了
check("工具栏「升八度」没改动 → 撤销栈深度不变",
      len(rig_b.ctrl.labels) == depth0, f"{depth0} → {len(rig_b.ctrl.labels)}")
check("工具栏「升八度」确实什么都没动", btrack.note_at(4, 72) is not None
      and btrack.note_at(4, 60) is b60)
# 正面对照：真的移调成功时撤销点必须留着，别把有效条目也抹了
gb.set_primary(b61)
depth1 = len(rig_b.ctrl.labels)
gb.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                           Qt.KeyboardModifier.NoModifier))
check("真的移调成功：撤销点留着且标签正确",
      len(rig_b.ctrl.labels) == depth1 + 1 and rig_b.ctrl.labels[-1] == "升降半音",
      str(rig_b.ctrl.labels[depth1:]))
check("真的移调成功：音符确实动了", btrack.note_at(4, 62) is not None)

# --- 真控制器：抹掉的是空条目而不是有效条目（最关键的证据）---
lproj, ltrack = blocked_pair("占位挡移调·真控制器")
rig_l = Rig(lproj)
ctrl_l = AppController(lproj)
rig_l.page.bind(ctrl_l)
gl = rig_l.grid
rig_l.click(*rig_l.cell(12, 60))                       # 先垫一个真实操作
check("先垫一个真实操作：栈顶是「绘制音符」", ctrl_l.undo_label() == "绘制音符",
      ctrl_l.undo_label())
gl.set_primary(ltrack.note_at(4, 60))
gl.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                           Qt.KeyboardModifier.NoModifier))
check("真控制器：被挡住的移调没有改变栈顶（空条目被 pop_undo 抹掉）",
      ctrl_l.undo_label() == "绘制音符", ctrl_l.undo_label())
check("真控制器：再按撤销能退到上一个真实操作（证明抹的是空条目）",
      ctrl_l.undo() and lproj.track_by_id(ltrack.id).note_at(12, 60) is None
      and len(lproj.track_by_id(ltrack.id).notes) == 3,
      f"{sorted((n.step, n.pitch) for n in lproj.track_by_id(ltrack.id).notes)}")
ctrl_l.shutdown()

print()
print("=" * 72)
print("全部通过" if ok else "存在失败项")
print("=" * 72)
sys.exit(0 if ok else 1)
