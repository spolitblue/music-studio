"""鼓机页面的功能与边界验证。

覆盖：页面构造与绘制、点亮/擦除真的改 notes、拖动刷音符、
参数改动真的写进 DrumVoice、鼓型写入了正确的小节与鼓件、
试听缓冲非静音、空工程（没有鼓轨）不崩。

两条刻意的测试手法
------------------
1. **把试听器换成一个假的**：真试听器会去开一条 PortAudio 流（还可能出声），
   在离屏测试里既慢又吵。假试听器把送进来的缓冲记下来 —— 反而能验证
   "页面真的把**非静音**的缓冲交给了播放器"，比只听"没报错"强得多。
2. **控制器替身自己实现撤销栈**：行为和 AppController 一致（快照 + 就地恢复），
   于是"撤销换了 Track 对象之后页面还认不认"这条容易被忽略的路径也被真实覆盖。
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# --- 项目根路径引导（本文件位于子目录）---
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
# --- 引导结束 ---

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from app.audio import demo
from app.audio.drums import render_drum
from app.audio.project import (
    DEFAULT_KIT, DRUM_NAMES, Note, Project, Track, drum_to_pitch,
)
from app.ui.pages import drums as drums_page
from app.ui.pages.drums import (
    DEFAULT_STEP_W, EMPTY_ROWS_HINT, MAX_STEP_W, MIN_STEP_W, PATTERN_NAMES, PATTERNS,
    ROW_H, DrumPage, add_drum_track, render_piece,
)

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class FakeAudition:
    """假试听器：只记录收到了什么缓冲。"""

    def __init__(self):
        self.buffers = []

    def play_note(self, stereo):
        self.buffers.append(np.asarray(stereo))
        return True

    def clear(self):
        self.buffers.clear()

    def peak(self, index=0):
        b = self.buffers[index]
        return float(np.max(np.abs(b))) if b.size else 0.0


FAKE = FakeAudition()
# 页面在"要响的那一刻"才查 audition 这个名字，所以在这里换掉即可全局生效
drums_page.audition = lambda: FAKE


class FakeController(QObject):
    """AppController 的最小替身：撤销栈语义与真的那套一致。"""

    statusMessage = Signal(str, int)
    undoStateChanged = Signal(bool, bool)

    def __init__(self, project):
        super().__init__()
        self.project = project
        self.labels = []          # 每次 push_undo 的标签
        self.messages = []
        self.touched = 0
        self._undo = []
        self._redo = []
        self.statusMessage.connect(lambda m, ms: self.messages.append(m))

    def push_undo(self, label="编辑"):
        self._undo.append((label, self.project.to_dict()))
        self.labels.append(label)
        self.undoStateChanged.emit(True, False)

    @property
    def can_undo(self):
        return bool(self._undo)

    def undo(self):
        if not self._undo:
            return False
        label, snap = self._undo.pop()
        self._redo.append((label, self.project.to_dict()))
        self._restore(snap)
        return True

    def redo(self):
        if not self._redo:
            return False
        label, snap = self._redo.pop()
        self._undo.append((label, self.project.to_dict()))
        self._restore(snap)
        return True

    def _restore(self, snap):
        restored = Project.from_dict(snap)
        # 与 AppController._restore 一致：**就地替换内容**，不换 Project 对象本身
        self.project.__dict__.update(restored.__dict__)
        self.undoStateChanged.emit(bool(self._undo), bool(self._redo))

    def touch(self, structural=False):
        self.touched += 1


def ev(kind, x, y, btn, buttons, mod=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), btn, buttons, mod)


def key(k, mod=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QEvent.Type.KeyPress, k, mod)


def wheel(widget, dy, x=None, y=ROW_H + 8.0,
          mod=Qt.KeyboardModifier.ControlModifier):
    """造一个滚轮事件直接投给网格 —— 离屏平台没有真滚轮，只能自己构造。

    滚轮缩放是**唯一**不经过页面、直接改 grid.step_w 的入口，
    也正是它曾经和页面的 _zoom_level 各说各话。
    """
    pos = QPointF(widget.width() / 2.0 if x is None else x, y)
    return QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, int(dy)),
                       Qt.MouseButton.NoButton, mod,
                       Qt.ScrollPhase.NoScrollPhase, False)


class Rig:
    """一个装好工程与页面的测试台。"""

    def __init__(self, project):
        self.project = project
        self.page = DrumPage(project)
        self.page.resize(1280, 780)
        # 离屏平台下 show() 不会真的开窗口，但会让布局生效 ——
        # 没有这一步，滚动区视口宽度还是默认值，"自动跟随"就没法验证
        self.page.show()
        if self.page.layout() is not None:
            self.page.layout().activate()
        app.processEvents()
        self.ctrl = FakeController(project)
        self.page.bind(self.ctrl)
        self.grid = self.page.grid

    # -- 坐标 ------------------------------------------------------------

    def row_of(self, piece):
        return self.grid.rows.index(piece)

    def cell(self, row, step):
        return (self.grid._x_for_step(step) + max(2.0, self.grid.step_w / 2.0),
                self.grid._row_top(row) + ROW_H / 2.0)

    def header(self, row):
        return 40.0, self.grid._row_top(row) + ROW_H / 2.0

    def switch(self, row):
        r = self.grid._switch_rect(row)
        return r.center().x(), r.center().y()

    # -- 鼠标 ------------------------------------------------------------

    def press(self, pos, btn=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.grid.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, pos[0], pos[1],
                                     btn, btn, mod))

    def move(self, pos, btn=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.grid.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, pos[0], pos[1],
                                    Qt.MouseButton.NoButton, btn, mod))

    def release(self, pos, btn=Qt.MouseButton.LeftButton):
        self.grid.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, pos[0], pos[1],
                                       btn, Qt.MouseButton.NoButton))

    def click(self, pos, btn=Qt.MouseButton.LeftButton, mod=Qt.KeyboardModifier.NoModifier):
        self.press(pos, btn, mod)
        self.release(pos, btn)

    def seek_to_step(self, step):
        self.page.set_position(self.project.step_time(step))


def notes_at(track, piece):
    pitch = drum_to_pitch(piece)
    return sorted(n.step for n in track.notes if n.pitch == pitch)


# ==========================================================================
print("=" * 70)
print("1) 页面构造与绘制")
print("=" * 70)

rig = Rig(demo.demo_project())
page, grid, proj = rig.page, rig.grid, rig.project

check("页面拿到了鼓轨", page.track is not None and page.track.kind == "drum",
      f"{page.track.name if page.track else None}")
check("主体切到编辑器（不是空状态）", page.body.currentIndex() == 1,
      f"index={page.body.currentIndex()}")
check("12 件鼓都展开成行（DEFAULT_KIT 里启用的那些）", len(grid.rows) == 12,
      f"{len(grid.rows)} 行")
check("行顺序 = DEFAULT_KIT 顺序", grid.rows == list(DEFAULT_KIT),
      "、".join(DRUM_NAMES.get(p, p) for p in grid.rows[:4]) + " …")
check("鼓件名取 DRUM_NAMES 的中文", all(DRUM_NAMES.get(p, "") for p in grid.rows),
      "、".join(DRUM_NAMES.get(p, p) for p in grid.rows[:3]))
check("每件鼓颜色不同", len({grid._row_color(i).name() for i in range(6)}) == 6,
      f"{len({grid._row_color(i).name() for i in range(6)})} 种")
check("网格宽度跟随工程步数",
      grid.width() >= grid._x_for_step(proj.total_steps),
      f"{grid.width()}px / {proj.total_steps} 步")

pix = page.grab()
check("页面能绘制（page.grab 不报错）", not pix.isNull() and pix.width() > 100,
      f"{pix.width()}×{pix.height()}")
gpix = grid.grab()
check("网格能绘制", not gpix.isNull() and gpix.width() > 100, f"{gpix.width()}×{gpix.height()}")
check("参数面板已绑到某件鼓", page.params.piece in grid.rows, f"{page.params.piece}")
check("六个参数齐全",
      [p[0] for p in page.params.PARAMS] == ["level", "tune", "decay", "tone", "snap", "pan"],
      "、".join(p[0] for p in page.params.PARAMS))
check("鼓型至少 4 种", len(PATTERN_NAMES) >= 4, "、".join(PATTERN_NAMES))

# 布局：三块区域不能重叠，也不能把旋钮挤出面板（6 个旋钮 + 身份区很容易挤爆）
check("顶栏在主体之上",
      page.bar.geometry().bottom() <= page.body.geometry().top(),
      f"bar.bottom={page.bar.geometry().bottom()} ≤ body.top={page.body.geometry().top()}")
check("参数面板在网格之下",
      page.params.geometry().top() >= page.scroll.geometry().bottom(),
      f"params.top={page.params.geometry().top()} ≥ scroll.bottom="
      f"{page.scroll.geometry().bottom()}")
check("参数面板宽度与网格区一致（不缩成一条）",
      abs(page.params.width() - page.body.width()) <= 2,
      f"{page.params.width()} vs {page.body.width()}")
check("网格视口有真实高度", page.scroll.viewport().height() > 200,
      f"{page.scroll.viewport().width()}×{page.scroll.viewport().height()}")
last_col = page.params.columns[-1]
check("六个旋钮都排在面板内（最后一个没被挤出边界）",
      last_col.geometry().right() <= page.params.width(),
      f"最后一个旋钮右缘 {last_col.geometry().right()} / 面板宽 {page.params.width()}")


# ==========================================================================
print()
print("=" * 70)
print("2) 点亮 / 擦除真的改了 track.notes")
print("=" * 70)

# 用一个干净工程（空鼓轨）来测格子编辑 —— 没有旧音符干扰，断言才能是精确值
rig2 = Rig(demo.empty_project())
page2, grid2, proj2 = rig2.page, rig2.grid, rig2.project
kick_row = rig2.row_of("kick")
check("空鼓轨起点是 0 个音符", len(grid2.track.notes) == 0, f"{len(grid2.track.notes)}")

FAKE.clear()
rig2.click(rig2.cell(kick_row, 4))
n_kick = grid2.track.note_at(4, drum_to_pitch("kick"))
check("左键点亮写进了 notes", n_kick is not None, f"note_at(4, kick)={n_kick}")
check("音符数 0 → 1", len(grid2.track.notes) == 1, f"{len(grid2.track.notes)}")
check("写的是正确鼓件的音高", n_kick is not None and n_kick.pitch == drum_to_pitch("kick") == 36,
      f"pitch={n_kick.pitch if n_kick else None}")
check("点亮瞬间试听了该鼓件", len(FAKE.buffers) == 1 and grid2.audition_count == 1,
      f"缓冲 {len(FAKE.buffers)} 个 / audition_count={grid2.audition_count}")
check("试听缓冲非静音且是立体声 (2, n)",
      len(FAKE.buffers) == 1 and FAKE.buffers[0].shape[0] == 2 and FAKE.peak() > 0.1,
      f"形状 {FAKE.buffers[0].shape} · 峰值 {FAKE.peak():.3f}")

rig2.click(rig2.cell(kick_row, 4))
check("再点一次熄灭（真的从 notes 里删掉）",
      grid2.track.note_at(4, 36) is None and len(grid2.track.notes) == 0,
      f"{len(grid2.track.notes)} 个")

rig2.click(rig2.cell(kick_row, 6))
check("右键擦除前先有音符", grid2.track.note_at(6, 36) is not None)
FAKE.clear()
rig2.click(rig2.cell(kick_row, 6), btn=Qt.MouseButton.RightButton)
check("右键擦除了音符",
      grid2.track.note_at(6, 36) is None and len(grid2.track.notes) == 0,
      f"{len(grid2.track.notes)} 个")
check("擦除不发声（安静本身就是反馈）", len(FAKE.buffers) == 0, f"{len(FAKE.buffers)} 个缓冲")

snare_row = rig2.row_of("snare")
rig2.click(rig2.cell(snare_row, 8))
check("不同行写的是不同鼓件",
      grid2.track.note_at(8, drum_to_pitch("snare")) is not None
      and grid2.track.note_at(8, 36) is None, "军鼓在第 8 步，底鼓没被误写")


# ==========================================================================
print()
print("=" * 70)
print("3) 按住拖动连刷 + 一整笔只占一步撤销")
print("=" * 70)

u0 = len(rig2.ctrl.labels)
c0 = grid2.audition_count
FAKE.clear()
rig2.press(rig2.cell(kick_row, 20))
for s in range(21, 25):
    rig2.move(rig2.cell(kick_row, s))
rig2.release(rig2.cell(kick_row, 24))
strokes = notes_at(grid2.track, "kick")
check("拖一整笔刷出连续 5 个音符", strokes == [20, 21, 22, 23, 24], f"{strokes}")
check("拖动过程中每经过一格都试听", grid2.audition_count - c0 == 5 and len(FAKE.buffers) == 5,
      f"试听 {grid2.audition_count - c0} 次 / 收到 {len(FAKE.buffers)} 个缓冲")
pushed = len(rig2.ctrl.labels) - u0
check("一整笔只压一步撤销点", pushed == 1, f"压了 {pushed} 步（{rig2.ctrl.labels[-1:]}）")

before = len(grid2.track.notes)
# 从**空格**按下，然后拖过一串已经点亮的格子：
# 按下那一格是"切换"语义（点亮的可以点灭），但拖过的格子只加不删
rig2.press(rig2.cell(kick_row, 19))
for s in range(20, 25):
    rig2.move(rig2.cell(kick_row, s))
rig2.release(rig2.cell(kick_row, 24))
check("拖过已有音符不会把它们擦掉",
      notes_at(grid2.track, "kick") == list(range(19, 25))
      and len(grid2.track.notes) == before + 1,
      f"底鼓 {notes_at(grid2.track, 'kick')}（{before} → {len(grid2.track.notes)}）")

row_tom = rig2.row_of("tom_mid")
rig2.press(rig2.cell(kick_row, 30))
rig2.move(rig2.cell(row_tom, 31))
rig2.release(rig2.cell(row_tom, 31))
check("可以拖到另一行继续刷",
      grid2.track.note_at(31, drum_to_pitch("tom_mid")) is not None,
      f"音符总数 → {len(grid2.track.notes)}")


# ==========================================================================
print()
print("=" * 70)
print("4) 试听路径：render_drum 缓冲非静音（逐件验证）")
print("=" * 70)

track = grid2.track
silent = []
for piece in grid.rows:
    buf = render_piece(track, piece, 0.9)
    if buf is None or buf.ndim != 2 or buf.shape[0] != 2 or float(np.max(np.abs(buf))) < 0.001:
        silent.append(piece)
check("12 件鼓都能渲染出非静音立体声缓冲", not silent,
      f"静音的：{silent}" if silent else "峰值均 > 0.001")

buf_loud = render_drum("kick", track.kit["kick"], 0.95)
check("render_drum 返回 (2, n)", buf_loud.ndim == 2 and buf_loud.shape[0] == 2,
      f"{buf_loud.shape}")
check("render_drum 缓冲非静音", float(np.max(np.abs(buf_loud))) > 0.1,
      f"峰值 {float(np.max(np.abs(buf_loud))):.3f}")
check("render_piece(None) 不崩", render_piece(None, "kick") is None)


# ==========================================================================
print()
print("=" * 70)
print("5) 参数面板：改动真的写进 DrumVoice，而且立刻试听")
print("=" * 70)

FAKE.clear()
rig2.click(rig2.header(rig2.row_of("snare")))
check("点表头选中了那件鼓", page2.params.piece == "snare", f"{page2.params.piece}")
check("点表头也试听了一次（表头就是鼓垫）", len(FAKE.buffers) == 1, f"{len(FAKE.buffers)} 个缓冲")
voice = track.kit["snare"]
check("面板绑定的就是工程里那个 DrumVoice 对象", page2.params.voice is voice)

edited = {"n": 0}
page2.edited.connect(lambda: edited.__setitem__("n", edited["n"] + 1))

decay_knob = page2.params._column_of["decay"].knob
c0 = page2.audition_count
decay_knob.set_value(0.50, notify=True)
check("旋钮改动写进了 DrumVoice.decay", abs(voice.decay - 0.50) < 1e-6, f"decay={voice.decay}")
check("参数一变就请求试听", page2.audition_count == c0 + 1, f"{c0} → {page2.audition_count}")
check("参数变化会请求重渲染（edited 信号）", edited["n"] >= 1, f"edited×{edited['n']}")

page2._audition_timer.stop()
FAKE.clear()
page2._play_audition()          # 手动补一次，绕开节流，拿到确定的一次
check("改完参数立刻响一声（送到播放器的缓冲非静音）",
      len(FAKE.buffers) == 1 and FAKE.peak() > 0.05, f"峰值 {FAKE.peak():.3f}")

long_buf = render_piece(track, "snare", 0.9)
page2.params._column_of["decay"].knob.set_value(0.02, notify=True)
short_buf = render_piece(track, "snare", 0.9)
check("衰减调短 → 缓冲真的变短（衰减不是摆设）",
      short_buf.shape[1] < long_buf.shape[1],
      f"0.50s → {long_buf.shape[1]} 帧 / 0.02s → {short_buf.shape[1]} 帧")

page2.params._column_of["tune"].knob.set_value(7.0, notify=True)
check("tune 写进 DrumVoice", abs(voice.tune - 7.0) < 1e-6, f"tune={voice.tune}")
page2.params._column_of["tone"].knob.set_value(0.95, notify=True)
check("tone 写进 DrumVoice", abs(voice.tone - 0.95) < 1e-6, f"tone={voice.tone}")
page2.params._column_of["snap"].knob.set_value(0.10, notify=True)
check("snap 写进 DrumVoice", abs(voice.snap - 0.10) < 1e-6, f"snap={voice.snap}")

page2.params._column_of["pan"].knob.set_value(-1.0, notify=True)
check("pan 写进 DrumVoice", abs(voice.pan + 1.0) < 1e-6, f"pan={voice.pan}")
left_buf = render_piece(track, "snare", 0.9)
check("pan 真的作用到立体声缓冲（左满 → 左声道明显更响）",
      float(np.max(np.abs(left_buf[0]))) > float(np.max(np.abs(left_buf[1]))) * 2.0,
      f"L={float(np.max(np.abs(left_buf[0]))):.3f} R={float(np.max(np.abs(left_buf[1]))):.3f}")
page2.params._column_of["pan"].knob.set_value(0.0, notify=True)

page2.params._column_of["level"].knob.set_value(0.0, notify=True)
quiet = render_piece(track, "snare", 0.9)
check("level=0 → 缓冲几乎静音", float(np.max(np.abs(quiet))) < 0.01,
      f"峰值 {float(np.max(np.abs(quiet))):.5f}")
page2.params._column_of["level"].knob.set_value(0.82, notify=True)

# 连续拖动旋钮：撤销点只压一次，但试听请求一次都不少
u0 = len(rig2.ctrl.labels)
c0 = page2.audition_count
edited["n"] = 0
page2._last_param_push = 0.0
FAKE.clear()
for i in range(12):
    decay_knob.set_value(0.2 + i * 0.05, notify=True)
pushed = len(rig2.ctrl.labels) - u0
requested = page2.audition_count - c0
check("一次拖动只压一步撤销（不刷爆撤销栈）", pushed == 1, f"压了 {pushed} 步")
check("每次参数变化都请求了试听（节流只合并播放，不丢请求）", requested == 12,
      f"{requested} 次请求")
check("节流真的在合并播放（拖动时没有打 12 发）", len(FAKE.buffers) < 12,
      f"实际播放 {len(FAKE.buffers)} 次 / 请求 {requested} 次")
time.sleep(0.15)
app.processEvents()             # 让节流器的尾音补上
check("节流后仍然补响了最后那一次（不会漏听）", len(FAKE.buffers) == 1,
      f"补响 {len(FAKE.buffers)} 次 · 峰值 {FAKE.peak():.3f}")
check("拖动也请求了重渲染", edited["n"] >= 1, f"edited×{edited['n']}")
page2._audition_timer.stop()


# ==========================================================================
print()
print("=" * 70)
print("6) 鼓型：写进播放头所在的小节与对应鼓件")
print("=" * 70)

rig3 = Rig(demo.empty_project())
page3, grid3, proj3 = rig3.page, rig3.grid, rig3.project
tr3 = grid3.track
spb = proj3.steps_per_bar
check("工程一小节 16 步", spb == 16, f"{spb}")

rig3.seek_to_step(2 * spb)                       # 播放头放到第 3 小节
check("播放头落在第 3 小节", grid3.current_bar() == 2, f"bar={grid3.current_bar()}")

added = page3.apply_pattern("四拍")
base = 2 * spb
check("鼓型写进了 notes（不是只画在界面上）", added > 0 and len(tr3.notes) == added,
      f"apply_pattern 返回 {added}，notes={len(tr3.notes)}")
check("底鼓落在 0/4/8/12 拍",
      notes_at(tr3, "kick") == [base + s for s in (0, 4, 8, 12)],
      f"{notes_at(tr3, 'kick')}（小节基址 {base}）")
check("拍手落在反拍 4/12", notes_at(tr3, "clap") == [base + 4, base + 12],
      f"{notes_at(tr3, 'clap')}")
check("闭踩镲是八分音符摆",
      notes_at(tr3, "hat_closed") == [base + s for s in range(0, 16, 2)],
      f"{notes_at(tr3, 'hat_closed')}")
check("鼓型没漏到别的小节",
      all(base <= n.step < base + spb for n in tr3.notes),
      f"步范围 {min(n.step for n in tr3.notes)}–{max(n.step for n in tr3.notes)}")
check("没碰它不管的鼓件（ride 仍为空）", notes_at(tr3, "ride") == [])
check("重音力度更大（第 1 拍 > 第 3 拍）",
      tr3.note_at(base, 36).velocity > tr3.note_at(base + 4, 36).velocity,
      f"{tr3.note_at(base, 36).velocity} > {tr3.note_at(base + 4, 36).velocity}")
check("push_undo 的标签是「铺鼓型」", rig3.ctrl.labels[-1] == "铺鼓型",
      f"{rig3.ctrl.labels[-1]}")

page3.apply_pattern("四拍")
check("同一鼓型铺第二次不会翻倍（先清再写）", len(tr3.notes) == added,
      f"共 {len(tr3.notes)} 个")

page3.apply_pattern("碎拍")
check("碎拍写入了正确的底鼓切分",
      notes_at(tr3, "kick") == [base + s for s in (0, 3, 6, 10)],
      f"{notes_at(tr3, 'kick')}")
check("碎拍带上了边击", notes_at(tr3, "rim") == [base + 7, base + 14],
      f"{notes_at(tr3, 'rim')}")

last_base = (proj3.bars - 1) * spb
rig3.seek_to_step(last_base)
page3.apply_pattern("加花")
check("最后一小节也能铺鼓型",
      notes_at(tr3, "tom_low") == [last_base + 12, last_base + 13]
      and notes_at(tr3, "crash") == [last_base],
      f"低嗵 {notes_at(tr3, 'tom_low')} · 吊镲 {notes_at(tr3, 'crash')}")
check("别的小节的鼓点没被加花清掉", len(notes_at(tr3, "kick")) == 5,
      f"底鼓共 {len(notes_at(tr3, 'kick'))} 个（3 小节 4 个 + 加花 1 个）")

tr3.kit["clap"].enabled = False
page3.apply_pattern("四拍")            # 这次铺在最后一小节（播放头刚移到那里）
check("静音的鼓件被跳过（不写看不见的音）",
      not any(last_base <= s < last_base + spb for s in notes_at(tr3, "clap")),
      f"最后一小节的 clap={[s for s in notes_at(tr3, 'clap') if s >= last_base]}")
check("提示条说明了跳过了静音件",
      any("跳过静音件" in m for m in rig3.ctrl.messages),
      f"{rig3.ctrl.messages[-1] if rig3.ctrl.messages else ''}")
tr3.kit["clap"].enabled = True

# 非 4/4 拍号也要能铺：鼓型按"一小节的比例"映射，不是死绑 16 步
odd = Project(name="三拍子", bpm=120.0, steps_per_beat=4, beats_per_bar=3, bars=4)
add_drum_track(odd)
rig4 = Rig(odd)
check("3/4 工程一小节 12 步", odd.steps_per_bar == 12, f"{odd.steps_per_bar}")
rig4.page.apply_pattern("四拍")
check("3/4 拍按比例映射（16 格 → 12 步：0/4/8/12 → 0/3/6/9）",
      notes_at(rig4.grid.track, "kick") == [0, 3, 6, 9],
      f"{notes_at(rig4.grid.track, 'kick')}")
check("3/4 拍所有鼓点都在小节内",
      all(n.step < odd.steps_per_bar for n in rig4.grid.track.notes),
      f"最大步 {max(n.step for n in rig4.grid.track.notes)} / spb={odd.steps_per_bar}")


# ==========================================================================
print()
print("=" * 70)
print("7) 每行的静音/启用开关（写进 DrumVoice.enabled）")
print("=" * 70)

rig5 = Rig(demo.empty_project())
page5, grid5 = rig5.page, rig5.grid
cow_row = rig5.row_of("cowbell")
voice_cow = grid5.track.kit["cowbell"]
check("开关初始是启用的（12 行都在）", len(grid5.rows) == 12 and voice_cow.enabled)

rig5.click(rig5.switch(cow_row))
check("点开关写进 DrumVoice.enabled", voice_cow.enabled is False)
check("静音后该行从网格消失", "cowbell" not in grid5.rows and len(grid5.rows) == 11,
      f"{len(grid5.rows)} 行")
check("静音被记进撤销栈", rig5.ctrl.labels[-1] == "静音鼓件", f"{rig5.ctrl.labels[-1]}")
pix5 = grid5.grab()
check("静音后仍能正常绘制", not pix5.isNull(), f"{pix5.width()}×{pix5.height()}")

page5.params.set_voice("cowbell", voice_cow)
page5.params.btn_enable.click()
check("参数面板的启用按钮改的是同一个字段", voice_cow.enabled is True)
check("重新启用后行回来了", "cowbell" in grid5.rows and len(grid5.rows) == 12,
      f"{len(grid5.rows)} 行")

for i in range(12):
    grid5.toggle_enabled(0)
check("全部静音后没有可显示的行", grid5.rows == [], f"{grid5.rows}")
pix5b = grid5.grab()
check("全部静音时绘制不崩", not pix5b.isNull(), f"{pix5b.width()}×{pix5b.height()}")
check("全部静音时网格会给出文字提示（不是一片空白）",
      "静音" in EMPTY_ROWS_HINT and "开关" in EMPTY_ROWS_HINT, EMPTY_ROWS_HINT)
for piece in grid5.track.kit:
    grid5.track.kit[piece].enabled = True
grid5.rebuild()
check("重新启用全部鼓件可恢复", len(grid5.rows) == 12, f"{len(grid5.rows)} 行")


# ==========================================================================
print()
print("=" * 70)
print("8) 播放头显示与跟随 / 缩放")
print("=" * 70)

rig6 = Rig(demo.demo_project())
page6, grid6 = rig6.page, rig6.grid
page6.scroll.horizontalScrollBar().setValue(0)
page6.set_position(0.0, True)
v0 = page6.scroll.horizontalScrollBar().value()
late = rig6.project.total_steps * 0.7 * rig6.project.seconds_per_step
page6.set_position(late, True)
v1 = page6.scroll.horizontalScrollBar().value()
check("播放头推进时视口跟着滚", v1 > v0, f"{v0} → {v1}")
check("播放位置被记进网格（绘制时用它）", abs(grid6._position - late) < 1e-9,
      f"{grid6._position:.2f}s")
v_before = page6.scroll.horizontalScrollBar().value()
page6.set_position(late + 2.0, False)
check("暂停时不自动跟随", page6.scroll.horizontalScrollBar().value() == v_before)

page6._zoom_level = 1.0
grid6.set_step_width(DEFAULT_STEP_W)
page6._zoom(1.25)
a = grid6.step_w
page6._zoom(1.25)
check("缩放可累积放大", grid6.step_w > a, f"{a:.1f} → {grid6.step_w:.1f}")
page6._zoom_level = 1.0
page6._zoom(0.01)
check("缩放下限被钳制", MIN_STEP_W <= grid6.step_w < MIN_STEP_W + 1.5, f"{grid6.step_w:.1f}")
page6._zoom_level = 1.0
page6._zoom(100.0)
check("缩放上限被钳制", MAX_STEP_W - 1.5 < grid6.step_w <= MAX_STEP_W, f"{grid6.step_w:.1f}")


# ==========================================================================
print()
print("=" * 70)
print("9) 空工程 / 没有鼓轨：不崩、给引导、还能一键建鼓轨")
print("=" * 70)

bare = Project(name="空的")
rig7 = Rig(bare)
page7 = rig7.page
check("没有鼓轨时不崩", page7.drum_tracks() == [] and not page7.has_drum_track)
check("主体切到空状态（不是空白）", page7.body.currentIndex() == 0,
      f"index={page7.body.currentIndex()}")
check("空状态卡片存在", page7.empty_card.width() == 430 and page7.empty_card.height() == 190)
check("空状态下网格没有行", page7.grid.rows == [])

ops_ok = True
try:
    page7.apply_pattern("四拍")
    page7.clear_current_bar()
    page7.refresh_notes()
    page7.set_position(3.0, True)
    page7.rebuild()
except Exception as exc:                        # 空工程下任何一条路径都不许炸
    ops_ok = False
    print(f"       异常：{type(exc).__name__}: {exc}")
check("空状态下铺鼓型/清空/刷新/定位/重建都不崩", ops_ok)
check("空状态下铺鼓型返回 0", page7.apply_pattern("四拍") == 0)
pix7 = page7.grab()
check("空状态也能绘制", not pix7.isNull(), f"{pix7.width()}×{pix7.height()}")

synth_only = Project(name="只有合成器")
synth_only.add_track(Track(name="钢琴", kind="synth"))
rig8 = Rig(synth_only)
check("只有旋律轨时也走空状态",
      rig8.page.body.currentIndex() == 0 and rig8.page.drum_tracks() == [])

page7.btn_new_track.click()
check("空状态的按钮真的新建了鼓轨",
      len(bare.tracks) == 1 and bare.tracks[0].kind == "drum", f"{len(bare.tracks)} 轨")
check("建轨后切到编辑器并绑好默认套鼓",
      page7.body.currentIndex() == 1 and page7.track is bare.tracks[0]
      and len(page7.grid.rows) == 12, f"{len(page7.grid.rows)} 行")
check("建轨也压了撤销点", rig7.ctrl.labels[-1] == "新建鼓组轨道", f"{rig7.ctrl.labels[-1]}")
check("建轨通知了控制器（结构性变化）", rig7.ctrl.touched >= 1, f"touch×{rig7.ctrl.touched}")


# ==========================================================================
print()
print("=" * 70)
print("10) 撤销 / 多条鼓轨：页面必须跟着工程走（不能抱着旧对象）")
print("=" * 70)

rig9 = Rig(demo.empty_project())
page9, grid9 = rig9.page, rig9.grid
rig9.click(rig9.cell(rig9.row_of("kick"), 0))
rig9.click(rig9.cell(rig9.row_of("kick"), 4))
check("先铺两个底鼓", len(grid9.track.notes) == 2, f"{len(grid9.track.notes)}")
stale = grid9.track
rig9.ctrl.undo()                      # 撤销第二次点击
page9.rebuild()
check("撤销后 Track 对象被换掉（就地恢复的必然结果）", grid9.track is not stale)
check("撤销后页面绑到了工程里那条新轨", grid9.track is rig9.project.tracks[0])
check("撤销一步回到上一次编辑后的状态", len(grid9.track.notes) == 1,
      f"{len(grid9.track.notes)} 个")
rig9.ctrl.undo()                      # 再撤一步回到空轨
page9.rebuild()
check("撤销后音符数回到 0", len(grid9.track.notes) == 0, f"{len(grid9.track.notes)}")
rig9.ctrl.redo()
page9.rebuild()
check("重做后音符回来了", len(grid9.track.notes) == 1, f"{len(grid9.track.notes)}")

add_drum_track(rig9.project)
page9.rebuild()
check("识别到两条鼓轨", len(page9.drum_tracks()) == 2, f"{len(page9.drum_tracks())} 条")
check("轨道芯片跟着建好", len(page9.track_chips) == 2, f"{len(page9.track_chips)} 个芯片")
page9.set_track(page9.drum_tracks()[1])
check("切换鼓轨后网格跟着换",
      page9.grid.track is page9.drum_tracks()[1] and page9.track is page9.drum_tracks()[1])
check("切换后仍然是 12 行", len(page9.grid.rows) == 12, f"{len(page9.grid.rows)}")
check("切换后的轨道是空的", len(page9.grid.track.notes) == 0,
      f"{len(page9.grid.track.notes)}")


# ==========================================================================
print()
print("=" * 70)
print("11) 键盘 / 小节尺 / 越界坐标")
print("=" * 70)

rig10 = Rig(demo.empty_project())
page10, grid10 = rig10.page, rig10.grid
seeks = []
page10.seekRequested.connect(lambda s: seeks.append(s))

rig10.click((grid10._x_for_step(32) + 4, 10.0))
check("点小节尺请求了 seek",
      len(seeks) == 1 and abs(seeks[0] - rig10.project.step_time(32)) < 1e-9,
      f"{seeks[0]:.4f}s vs {rig10.project.step_time(32):.4f}s")

# 方向键以"当前播放头"为基准。播放位置由控制器回灌（页面只发 seekRequested），
# 所以这里先手动同步一次位置 —— 这也正是真实运行时控制器做的事
rig10.seek_to_step(32)
grid10.keyPressEvent(key(Qt.Key.Key_Right))
check("→ 键基于当前播放头挪一步",
      len(seeks) == 2 and abs(seeks[1] - rig10.project.step_time(33)) < 1e-9,
      f"{seeks[-1]:.4f}s")
grid10.keyPressEvent(key(Qt.Key.Key_Left))
check("← 键往回挪一步",
      len(seeks) == 3 and abs(seeks[2] - rig10.project.step_time(31)) < 1e-9,
      f"{seeks[-1]:.4f}s")

rig10.click(rig10.cell(rig10.row_of("kick"), 0))
rig10.click(rig10.cell(rig10.row_of("kick"), 4))
rig10.click(rig10.cell(rig10.row_of("snare"), 8))
check("清空前有 3 个音符", len(grid10.track.notes) == 3, f"{len(grid10.track.notes)}")
grid10.selected_row = rig10.row_of("kick")
grid10.keyPressEvent(key(Qt.Key.Key_Delete))
check("Delete 只清掉当前鼓件",
      len(grid10.track.notes) == 1
      and grid10.track.notes[0].pitch == drum_to_pitch("snare"),
      f"剩 {len(grid10.track.notes)} 个")

rig10.click((grid10.width() - 2.0, grid10._row_top(0) + ROW_H / 2))
check("点最右侧不会写出越界步号",
      all(0 <= n.step < rig10.project.total_steps for n in grid10.track.notes),
      f"最大步 {max((n.step for n in grid10.track.notes), default=-1)} / 上限 "
      f"{rig10.project.total_steps - 1}")
before_notes = len(grid10.track.notes)
rig10.click(rig10.header(0))          # 点鼓件名（而不是行尾那个开关）
check("点表头名字不会写音符（那是选中/试听的地盘）",
      len(grid10.track.notes) == before_notes,
      f"{before_notes} → {len(grid10.track.notes)}")
check("点表头名字只切换参数面板的选中件（不会误触静音开关）",
      page10.params.piece == "kick" and grid10.track.kit["kick"].enabled
      and "kick" in grid10.rows,
      f"{page10.params.piece} · enabled={grid10.track.kit['kick'].enabled}")

rig10.seek_to_step(0)
rig10.click(rig10.cell(rig10.row_of("kick"), 2))
rig10.seek_to_step(rig10.project.steps_per_bar)
rig10.click(rig10.cell(rig10.row_of("kick"), rig10.project.steps_per_bar + 2))
in_first_bar = [n for n in grid10.track.notes
                if 0 <= n.step < rig10.project.steps_per_bar]
expected = len(in_first_bar)
total_before = len(grid10.track.notes)
rig10.seek_to_step(0)
removed = page10.clear_current_bar()
check("清空小节只清掉播放头所在那一小节",
      removed == expected and len(grid10.track.notes) == total_before - expected,
      f"删了 {removed} 个（该小节原有 {expected} 个）· {total_before} → "
      f"{len(grid10.track.notes)}")
check("清空小节压了撤销点", rig10.ctrl.labels[-1] == "清空小节", f"{rig10.ctrl.labels[-1]}")


# ==========================================================================
print()
print("=" * 70)
print("12) 撤销点必须压在改动**之前**：参数面板的撤销真的回退")
print("=" * 70)

# 复核者实测：decay 0.34 → 0.88 之后按撤销，值仍是 0.88。
# 根因是顺序：面板先 setattr 写进 DrumVoice，页面才收到信号去 push_undo ——
# 压点那一刻快照里已经是新值，撤销自然"撤了个寂寞"。
rigU = Rig(demo.empty_project())
pageU, gridU = rigU.page, rigU.grid

vu = gridU.track.kit["snare"]
vu.decay = 0.34
vu.enabled = True
pageU.params.set_voice("snare", vu)
check("测试起点：snare.decay = 0.34", abs(vu.decay - 0.34) < 1e-9, f"{vu.decay}")

u0 = len(rigU.ctrl.labels)
pageU.params._column_of["decay"].knob.set_value(0.88, notify=True)
check("旋钮改动写进了 DrumVoice", abs(vu.decay - 0.88) < 1e-9, f"decay={vu.decay}")
check("旋钮改动压了撤销点", len(rigU.ctrl.labels) == u0 + 1,
      f"压了 {len(rigU.ctrl.labels) - u0} 步")

rigU.ctrl.undo()
pageU.rebuild()
check("撤销旋钮改动 → decay 回到 0.34（而不是停在新值 0.88）",
      abs(gridU.track.kit["snare"].decay - 0.34) < 1e-6,
      f"撤销后 decay={gridU.track.kit['snare'].decay}")
rigU.ctrl.redo()
pageU.rebuild()
check("重做旋钮改动 → decay 回到 0.88",
      abs(gridU.track.kit["snare"].decay - 0.88) < 1e-6,
      f"重做后 decay={gridU.track.kit['snare'].decay}")

# 面板的启用开关同理：必须**先压点、后翻转** enabled
vu2 = gridU.track.kit["snare"]
vu2.enabled = True
pageU.params.set_voice("snare", vu2)
u1 = len(rigU.ctrl.labels)
pageU.params.btn_enable.click()
check("面板开关写进了 DrumVoice.enabled", vu2.enabled is False, f"enabled={vu2.enabled}")
check("面板开关压了撤销点，且标签说清了动作", rigU.ctrl.labels[-1] == "静音鼓件",
      f"「{rigU.ctrl.labels[-1]}」")
check("一次点击只压一步（面板与页面不许各压一次）", len(rigU.ctrl.labels) == u1 + 1,
      f"压了 {len(rigU.ctrl.labels) - u1} 步")
rigU.ctrl.undo()
pageU.rebuild()
check("撤销面板静音 → enabled 回到 True",
      gridU.track.kit["snare"].enabled is True,
      f"撤销后 enabled={gridU.track.kit['snare'].enabled}")

# 而"网格/鼓型/清空"三条老路径本来就压得早，这里钉住不许被顺手改坏
u2 = len(rigU.ctrl.labels)
rigU.click(rigU.cell(rigU.row_of("kick"), 6))
check("网格点音符仍然只压一步撤销", len(rigU.ctrl.labels) == u2 + 1,
      f"「{rigU.ctrl.labels[-1]}」")
rigU.ctrl.undo()
pageU.rebuild()
check("网格编辑的撤销也真的回退（老路径没被改坏）",
      gridU.track.note_at(6, drum_to_pitch("kick")) is None,
      f"剩 {len(gridU.track.notes)} 个音符")


# ==========================================================================
print()
print("=" * 70)
print("13) 缩放状态同步：Ctrl+滚轮 与「放大/缩小」按钮必须共用同一基准")
print("=" * 70)

# 复核者实测：16.0 →（Ctrl+滚轮）34.0 →（点放大）**20.0**，点放大反而变小。
# 根因是 grid.step_w 被滚轮改了，而页面的 _zoom_level 没人通知 ——
# 两个写入者各存一份"当前缩放"，点按钮时用的是那份过期的。
rigZ = Rig(demo.empty_project())
pageZ, gridZ = rigZ.page, rigZ.grid
pageZ.scroll.horizontalScrollBar().setValue(0)
check("缩放的起点是默认步宽", abs(gridZ.step_w - DEFAULT_STEP_W) < 1e-9,
      f"{gridZ.step_w}px")

for _ in range(3):                      # 16 → 18.4 → 21.16 → 24.33
    gridZ.wheelEvent(wheel(gridZ, 120))
zoomed = gridZ.step_w
check("Ctrl+滚轮真的放大了步宽", zoomed > DEFAULT_STEP_W * 1.4,
      f"{DEFAULT_STEP_W} → {zoomed:.2f}")
check("滚轮改的步宽同步进了 _zoom_level（此前只有按钮会改它）",
      abs(pageZ._zoom_level - zoomed / DEFAULT_STEP_W) < 1e-6,
      f"_zoom_level={pageZ._zoom_level:.4f} / 实际 {zoomed / DEFAULT_STEP_W:.4f}")
pageZ._zoom(1.25)
check("滚轮放大后点「放大」只会更大（不会反而变小）", gridZ.step_w > zoomed,
      f"{zoomed:.2f} → {gridZ.step_w:.2f}")

for _ in range(2):                      # 反向也一样
    gridZ.wheelEvent(wheel(gridZ, -120))
shrunk = gridZ.step_w
check("Ctrl+滚轮缩小真的变小", shrunk < zoomed, f"{zoomed:.2f} → {shrunk:.2f}")
pageZ._zoom(0.8)
check("滚轮缩小后点「缩小」只会更小", gridZ.step_w < shrunk,
      f"{shrunk:.2f} → {gridZ.step_w:.2f}")

# 滚到顶之后再点放大：不许掉头（这正是复核者看到的那一幕）
pageZ._zoom_level = 1.0
gridZ.set_step_width(DEFAULT_STEP_W)
while gridZ.step_w < MAX_STEP_W:
    gridZ.wheelEvent(wheel(gridZ, 120))
    if abs(gridZ.step_w - MAX_STEP_W) < 1e-9:
        break
check("滚轮可以一路放大到上限", abs(gridZ.step_w - MAX_STEP_W) < 1e-9, f"{gridZ.step_w:.2f}px")
pageZ._zoom(1.25)
check("已经到上限时点「放大」保持在上限（不会跌回 20）",
      abs(gridZ.step_w - MAX_STEP_W) < 1e-9, f"{gridZ.step_w:.2f}px")
pageZ._zoom(0.8)
check("从上限点「缩小」按当前步宽算（34 → 27.2，而不是拿过期的 1.0 算成 12.8）",
      abs(gridZ.step_w - MAX_STEP_W * 0.8) < 1e-6, f"{gridZ.step_w:.2f}px")


# ==========================================================================
print()
print("=" * 70)
print("14) 空操作不许压撤销点（否则撤销栈里全是「什么都没发生」）")
print("=" * 70)

# 复核者实测：标签出现了，但音符数一个都没变 —— 撤销一下，界面纹丝不动。
# 撤销栈是用户唯一的"时间机器"，里面塞空操作等于把机器弄脏。
rigN = Rig(demo.empty_project())
pageN, gridN = rigN.page, rigN.grid
kickN = rigN.row_of("kick")
rideN = rigN.row_of("ride")
started = []
gridN.editStarted.connect(lambda label: started.append(label))
n0 = len(rigN.ctrl.labels)

# a) 右键点空格子
rigN.click(rigN.cell(kickN, 5), btn=Qt.MouseButton.RightButton)
check("右键点空格子：确实什么都没发生", len(gridN.track.notes) == 0,
      f"{len(gridN.track.notes)} 个音符")
check("右键点空格子不压撤销点、也不发 editStarted",
      len(rigN.ctrl.labels) == n0 and started == [],
      f"压了 {len(rigN.ctrl.labels) - n0} 步 / editStarted={started}")

# b) 但右键擦掉真音符照旧要压（别为了修空操作把正常路径也砍了）
rigN.click(rigN.cell(kickN, 5))
n1 = len(rigN.ctrl.labels)
started.clear()
rigN.click(rigN.cell(kickN, 5), btn=Qt.MouseButton.RightButton)
check("右键擦掉真音符照旧压一步撤销",
      len(rigN.ctrl.labels) == n1 + 1 and started == ["擦除鼓点"],
      f"压了 {len(rigN.ctrl.labels) - n1} 步 / editStarted={started}")

# c) 右键从**空格子**起手、拖到有音符的格子：撤销点必须跟着"第一次真改动"走，
#    否则这一笔擦除撤不回来（这是修 a) 时最容易踩出来的新洞）
rigN.click(rigN.cell(kickN, 8))
n2 = len(rigN.ctrl.labels)
started.clear()
rigN.press(rigN.cell(kickN, 7), btn=Qt.MouseButton.RightButton)
rigN.move(rigN.cell(kickN, 8), btn=Qt.MouseButton.RightButton)
rigN.release(rigN.cell(kickN, 8), btn=Qt.MouseButton.RightButton)
check("右键从空格起手拖过去：音符真的被擦掉了",
      gridN.track.note_at(8, drum_to_pitch("kick")) is None,
      f"剩 {len(gridN.track.notes)} 个音符")
check("这一笔仍然只压了一步撤销（擦除撤得回来）",
      len(rigN.ctrl.labels) == n2 + 1 and started == ["擦除鼓点"],
      f"压了 {len(rigN.ctrl.labels) - n2} 步 / editStarted={started}")

# d) Delete 清空一件本来就没音符的鼓
gridN.selected_row = rideN
n3 = len(rigN.ctrl.labels)
started.clear()
gridN.keyPressEvent(key(Qt.Key.Key_Delete))
check("Delete 清空没有音符的鼓件：不压撤销点",
      len(rigN.ctrl.labels) == n3 and started == [],
      f"压了 {len(rigN.ctrl.labels) - n3} 步 / editStarted={started}")

rigN.click(rigN.cell(kickN, 20))
gridN.selected_row = kickN
n4 = len(rigN.ctrl.labels)
started.clear()
gridN.keyPressEvent(key(Qt.Key.Key_Delete))
check("Delete 清空有音符的鼓件：照旧压一步",
      len(rigN.ctrl.labels) == n4 + 1 and started == ["清空鼓件"]
      and len(gridN.track.notes) == 0,
      f"压了 {len(rigN.ctrl.labels) - n4} 步 / editStarted={started} / "
      f"剩 {len(gridN.track.notes)} 个")

# e) 清空本来就没有鼓点的小节
rigN.seek_to_step(0)
n5 = len(rigN.ctrl.labels)
removed = pageN.clear_current_bar()
check("清空空小节：removed=0 且不压撤销点",
      removed == 0 and len(rigN.ctrl.labels) == n5,
      f"removed={removed} / 压了 {len(rigN.ctrl.labels) - n5} 步")

rigN.click(rigN.cell(kickN, 3))
n6 = len(rigN.ctrl.labels)
removed2 = pageN.clear_current_bar()
check("清空有鼓点的小节：照旧压一步「清空小节」",
      removed2 == 1 and len(rigN.ctrl.labels) == n6 + 1
      and rigN.ctrl.labels[-1] == "清空小节",
      f"removed={removed2} / 「{rigN.ctrl.labels[-1]}」")


# ==========================================================================
print()
print("=" * 70)
print("15) 试听失败必须出声报错（不许 except: return None 静默吞掉）")
print("=" * 70)

# 复核者实测：鼓件渲染失败时用户**完全没反馈** —— 点了不响、提示条一个字没有。
# 而"边调边听"正是本页面的主流程，静默失败等于把"坏了"伪装成"就这样"。
rigF = Rig(demo.empty_project())
pageF, gridF = rigF.page, rigF.grid
kickF = rigF.row_of("kick")
kick_name = DRUM_NAMES["kick"]
orig_render = drums_page.drum_engine.render_drum


def boom(*_args, **_kwargs):
    raise RuntimeError("模拟鼓件渲染失败")


drums_page.drum_engine.render_drum = boom
try:
    rigF.ctrl.messages.clear()
    pageF._hint.setText("")
    rigF.click(rigF.cell(kickF, 3))
    edited_ok = gridF.track.note_at(3, drum_to_pitch("kick")) is not None
    hint = pageF._hint.text()
    click_msgs = list(rigF.ctrl.messages)

    pageF._hint.setText("")
    rigF.ctrl.messages.clear()
    rigF.click(rigF.header(kickF))          # 点鼓件名（最常用的试听入口）
    header_hint = pageF._hint.text()

    pageF._hint.setText("")
    rigF.ctrl.messages.clear()
    pageF._play_audition()                  # 面板「试听」按钮走的那条路
    panel_hint = pageF._hint.text()
    panel_msgs = list(rigF.ctrl.messages)

    none_ok = render_piece(gridF.track, "kick") is None
finally:
    drums_page.drum_engine.render_drum = orig_render

check("渲染失败也不许丢掉这次编辑（音符照样写进工程）", edited_ok,
      f"{len(gridF.track.notes)} 个音符")
check("render_piece 渲染失败时返回 None（异常不许冒到 UI 上）", none_ok)
check("点格子时渲染失败：提示条说了话（不是静静吃掉）", hint.strip() != "",
      f"提示条：{hint!r}")
check(f"提示里点名了是哪件鼓（{kick_name}）并给出原因",
      kick_name in hint and ("没" in hint or "失败" in hint),
      f"提示条：{hint!r}")
check("渲染失败同时报到了状态栏（statusMessage），不会被后来的提示挤掉",
      any(("没" in m or "失败" in m) for m in click_msgs),
      f"状态栏：{click_msgs}")
check("点鼓件名（表头）试听失败时，提示条留得住（不被『选中…』盖掉）",
      kick_name in header_hint and ("没" in header_hint or "失败" in header_hint),
      f"提示条：{header_hint!r}")
check("面板「试听」路径失败同样会报（提示条 + 状态栏）",
      panel_hint.strip() != "" and any(("没" in m or "失败" in m) for m in panel_msgs),
      f"提示条：{panel_hint!r} / 状态栏：{panel_msgs}")

# 反向：正常渲染时不许误报（失败提示不能变成狼来了）
rigF.ctrl.messages.clear()
pageF._hint.setText("")
rigF.click(rigF.cell(kickF, 9))
check("正常渲染时不误报失败",
      pageF._hint.text().strip() == ""
      and not [m for m in rigF.ctrl.messages if "没" in m or "失败" in m],
      f"提示条：{pageF._hint.text()!r} / 状态栏：{list(rigF.ctrl.messages)}")
check("正常渲染时缓冲照样送到播放器（修完没把试听改坏）",
      len(FAKE.buffers) >= 1 and FAKE.peak() > 0.05,
      f"{len(FAKE.buffers)} 个缓冲 · 峰值 {FAKE.peak():.3f}")


# ==========================================================================
print()
print("=" * 70)
print("16) 与**真实** AppController 对接（替身之外的最后一关）")
print("=" * 70)

from app.ui.controller import AppController

real_proj = demo.empty_project()
real_ctrl = AppController(real_proj)
status = []
real_ctrl.statusMessage.connect(lambda m, ms: status.append(m))

real_page = DrumPage(real_proj)
real_page.resize(1100, 700)
real_page.show()
real_page.bind(real_ctrl)
app.processEvents()
real_grid = real_page.grid
check("bind 接上了真控制器", real_page._controller is real_ctrl)

# 用真控制器走一遍编辑 → 撤销 → 重做
spb_real = real_proj.steps_per_bar
real_page.set_position(0.0)
added_real = real_page.apply_pattern("反拍")
check("真控制器下铺鼓型有音符落地", added_real > 0 and len(real_grid.track.notes) == added_real,
      f"{added_real} 个")
check("真控制器记下了撤销标签", real_ctrl.undo_label() == "铺鼓型",
      f"「{real_ctrl.undo_label()}」")
check("真控制器可以通过 can_undo 报告状态", real_ctrl.can_undo)
check("状态提示走的是控制器的 statusMessage",
      any("反拍" in m for m in status), f"{status[-1] if status else ''}")

k_row = real_grid.rows.index("kick")
real_grid.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress,
                             real_grid._x_for_step(1) + 8, real_grid._row_top(k_row) + ROW_H / 2,
                             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier))
real_grid.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease,
                               real_grid._x_for_step(1) + 8, real_grid._row_top(k_row) + ROW_H / 2,
                               Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
check("点格子也压进了真撤销栈", real_ctrl.can_undo and real_ctrl.undo_label() == "编辑鼓点",
      f"「{real_ctrl.undo_label()}」")

before_undo = len(real_grid.track.notes)
real_page._undo()
check("页面上的撤销按钮真的撤销了（并重新绑好轨道）",
      len(real_grid.track.notes) == before_undo - 1
      and real_grid.track is real_proj.tracks[0],
      f"{before_undo} → {len(real_grid.track.notes)}")
real_page._redo()
check("重做按钮也通", len(real_grid.track.notes) == before_undo,
      f"{len(real_grid.track.notes)}")

real_ctrl.undo()                      # 先撤掉那一次手动点的鼓
real_page.rebuild()
check("再撤一步回到铺鼓型之后的状态", len(real_grid.track.notes) == added_real,
      f"{len(real_grid.track.notes)} 个")
real_ctrl.undo()                      # 再撤掉整次铺鼓型
real_page.rebuild()
check("撤销整次铺鼓型后网格空了", len(real_grid.track.notes) == 0,
      f"{len(real_grid.track.notes)} 个")
pix_real = real_page.grab()
check("真控制器下页面仍能绘制", not pix_real.isNull(), f"{pix_real.width()}×{pix_real.height()}")
real_ctrl.shutdown()


# ==========================================================================
print()
print("=" * 70)
print("全部通过" if ok else "存在失败项")
print("=" * 70)
sys.exit(0 if ok else 1)
