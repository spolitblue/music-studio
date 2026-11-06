"""音序器 —— 多轨步进网格，全曲骨架的编辑入口。

设计取舍（为什么长这样）
------------------------
市面上两种步进音序器各有硬伤：

* **一行一个默认音高**（FL 的 Channel Rack 那种）：编曲快，但写不了旋律 ——
  想要变化音高就得切到钢琴卷帘，来回跳。
* **每轨一个完整钢琴卷帘**：什么都能编，但那就不是"一览"了，
  8 小节的曲子铺满屏幕，反而看不出结构。

这里取中间路线：**鼓轨展开成多行（每件一行），旋律轨压成一行但纵向按音高映射**。
旋律行本身就是一个"压缩钢琴卷帘" —— 点哪里，横坐标决定步、纵坐标决定音高。

鼠标语义（一张表看懂）
----------------------
======================  ==========================================
左键点空格               加音符，**并立刻试听**
左键点已有音符           删掉它
左键按住拖动             连续刷音符（经过已有音符不会擦掉它）
右键                     擦除
**Shift + 上下拖**       **调力度**（横向不动，只改力度）
**拖音符右缘 5px**       **调时值**
**Alt + 拖动**           **框选**（划一块矩形，选中范围内的音符）
滚轮 / Shift+滚轮       纵向滚动 / 横向滚动
Ctrl + 滚轮              **缩放网格**
======================  ==========================================

键盘：`Delete` 删除选中 · `Ctrl+A` 全选 · `Ctrl+C/V` 复制粘贴（锚在播放头）·
`Ctrl+D` 复制当前小节到下一小节 · `Ctrl+Z/Y` 撤销重做 · `←/→` 挪播放头一步
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import dsp
from app.audio.project import (
    DEFAULT_KIT, DRUM_NAMES, Note, Project, Track, drum_to_pitch, pitch_to_drum,
)
from app.theme import accents, labels, materials, radii, spacing, type_scale
from app.widgets.glass import GlassCard, GlassImageButton, GlassPanel, GlassStyle, paint_glass

# --------------------------------------------------------------------------
# 尺寸常量
# --------------------------------------------------------------------------

HEADER_W = 172
RULER_H = 26
DRUM_LANE_H = 21
SYNTH_LANE_H = 46
NOTE_H = 4.4
NOTE_GRAB_EDGE = 5.0        # 音符右缘多少像素内算"抓时值"

MIN_STEP_W = 7.0
MAX_STEP_W = 34.0
DEFAULT_STEP_W = 15.0

AUDITION_SECONDS = 0.30     # 试听时音符发声多长


# --------------------------------------------------------------------------
# 泳道
# --------------------------------------------------------------------------


@dataclass
class Lane:
    """网格里的一行。

    ``piece`` 非空表示鼓件行（单行开关语义）；否则是旋律行（纵向映射音高）。
    """

    track: Track
    kind: str
    piece: str | None = None
    label: str = ""
    lo_pitch: int = 48
    hi_pitch: int = 72
    height: float = DRUM_LANE_H
    color: QColor = field(default_factory=lambda: accents.blue)

    @property
    def is_drum(self) -> bool:
        return self.kind == "drum"

    @property
    def pitch(self) -> int:
        """鼓件行对应哪个 MIDI 音高。"""
        return drum_to_pitch(self.piece or "kick")

    def pitch_at(self, y_in_lane: float) -> int:
        span = max(1, self.hi_pitch - self.lo_pitch)
        frac = max(0.0, min(1.0, y_in_lane / max(1.0, self.height)))
        return int(round(self.hi_pitch - frac * span))

    def y_for_pitch(self, pitch: int) -> float:
        span = max(1, self.hi_pitch - self.lo_pitch)
        frac = (self.hi_pitch - pitch) / span
        return max(0.0, min(self.height - NOTE_H, frac * self.height))


def build_lanes(project: Project, *, fold_drums: bool = False) -> list[Lane]:
    """把工程摊平成泳道列表。

    ``fold_drums`` 为真时只保留**有音符**的鼓件行 —— 12 件套全展开要占 250px 纵向，
    实际常用的只有三四件，折叠能省下大量空间。
    """
    lanes: list[Lane] = []
    for track in project.tracks:
        color = QColor(track.color)
        if track.kind == "drum":
            track.ensure_kit()
            used = {pitch_to_drum(n.pitch) for n in track.notes}
            for piece in DEFAULT_KIT:
                voice = track.kit.get(piece)
                if voice is None or not voice.enabled:
                    continue
                if fold_drums and piece not in used:
                    continue
                lanes.append(Lane(
                    track=track, kind="drum", piece=piece,
                    label=DRUM_NAMES.get(piece, piece),
                    height=DRUM_LANE_H, color=color,
                ))
        else:
            pitches = [n.pitch for n in track.notes]
            lo = min(pitches) if pitches else 48
            hi = max(pitches) if pitches else 72
            lo = min(lo - 3, 57)
            hi = max(hi + 3, 69)
            lanes.append(Lane(
                track=track, kind="synth", label=track.name,
                lo_pitch=lo, hi_pitch=hi, height=SYNTH_LANE_H, color=color,
            ))
    return lanes


# --------------------------------------------------------------------------
# 试听
# --------------------------------------------------------------------------


def _render_audition(lane: Lane, pitch: int, velocity: float):
    """渲染一个音用于试听。

    这里**故意不走整轨渲染**（那要几秒），只合成这一个音。
    """
    try:
        if lane.is_drum:
            from app.audio.drums import render_drum

            voice = lane.track.kit.get(lane.piece or "kick")
            if voice is None:
                return None
            return render_drum(lane.piece or "kick", voice, velocity)
        from app.audio.synth import render_synth_note

        return render_synth_note(lane.track.synth, pitch, velocity, AUDITION_SECONDS)
    except Exception:
        return None


def _play_audition(lane: Lane, pitch: int, velocity: float) -> None:
    try:
        from app.audio.player import audition

        p = audition()
        if p is None:
            return
        buf = _render_audition(lane, pitch, velocity)
        if buf is not None:
            p.play_note(buf)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 主网格
# --------------------------------------------------------------------------


class StepGrid(QWidget):
    """步进网格本体：绘制泳道/小节尺/播放头/选区/悬停读数，并处理全部交互。"""

    notesChanged = Signal()
    seekRequested = Signal(float)
    statusHint = Signal(str)
    #: 一次编辑**即将开始**时发一次（按下鼠标那一刻）。
    #: 拖动全程只发一次，所以"拖一整笔"在撤销栈里是一步 —— 符合直觉。
    editStarted = Signal(str)

    #: 步宽变化（含 Ctrl+滚轮与工具条两条路径）。
    #: 发这个信号是为了让页面把 `_zoom_level` 镜像成真实值 —— 早先滚轮直接改
    #: `step_w` 而不通知页面，页面的 `_zoom_level` 就停在旧值上，
    #: 于是「Ctrl+滚轮放大到 34px 后再点放大按钮」反而会算成 15×1.25=18.75px，
    #: 表现为「点放大反而变小」。这个 bug 是鼓机 agent 复核时顺手发现的同构问题。
    stepWidthChanged = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.lanes: list[Lane] = []
        self.step_w = DEFAULT_STEP_W
        self.fold_drums = False

        self._position = 0.0
        self._playing = False
        self._hover: tuple[int, int] | None = None
        self._cursor_pos = QPointF(0, 0)

        # 拖拽状态机
        self._mode = ""                      # paint/erase/velocity/length/marquee
        self._drag_note: Note | None = None
        self._drag_start = QPointF(0, 0)
        self._drag_origin_vel = 0.0
        self._drag_origin_len = 1
        self._last_cell: tuple[int, int, int] | None = None
        self._marquee: QRectF | None = None

        # 选区：以「泳道区间 × 步区间」表示，而不是记录音符对象。
        # 用区间表示的好处是它不依赖对象身份 —— 撤销/重做会重建整个工程，
        # 按对象记的选区会全部失效。
        self._sel: tuple[int, int, int, int] | None = None
        self._clipboard: list[tuple[int, int, int, int, float, int]] = []
        self._clip_span = 1

        self._selected_lane = 0
        self.follow = True

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # -- 数据 ------------------------------------------------------------

    def set_project(self, project: Project) -> None:
        self.project = project
        self.rebuild()

    def rebuild(self) -> None:
        self.lanes = build_lanes(self.project, fold_drums=self.fold_drums)
        self._selected_lane = max(0, min(self._selected_lane, len(self.lanes) - 1))
        self._sel = None
        self._resize_to_content()
        self.update()

    def set_fold_drums(self, value: bool) -> None:
        if value == self.fold_drums:
            return
        self.fold_drums = value
        self.rebuild()

    def _content_size(self) -> tuple[int, int]:
        steps = self.project.total_steps
        w = int(HEADER_W + steps * self.step_w + 8)
        h = int(RULER_H + sum(l.height for l in self.lanes) + 8)
        return max(w, 200), max(h, 120)

    def _resize_to_content(self) -> None:
        w, h = self._content_size()
        self.setFixedSize(w, h)
        self.update()

    def set_step_width(self, px: float) -> None:
        """改变步宽。**这是唯一允许改 `step_w` 的入口** ——
        统一在这里钳制并广播真实值，才不会出现两个写入者各说各话。"""
        new_w = max(MIN_STEP_W, min(MAX_STEP_W, float(px)))
        if abs(new_w - self.step_w) < 1e-9:
            return
        self.step_w = new_w
        self._resize_to_content()
        self.stepWidthChanged.emit(new_w)

    def set_position(self, seconds: float, playing: bool = False) -> None:
        self._position = float(seconds)
        self._playing = bool(playing)
        self.update()

    # -- 坐标换算 --------------------------------------------------------

    def _lane_at_y(self, y: float) -> tuple[int, float] | None:
        if y < RULER_H:
            return None
        cur = RULER_H
        for i, lane in enumerate(self.lanes):
            if cur <= y < cur + lane.height:
                return i, y - cur
            cur += lane.height
        return None

    def _lane_top(self, index: int) -> float:
        return RULER_H + sum(l.height for l in self.lanes[:index])

    def _step_at_x(self, x: float) -> int:
        s = int((x - HEADER_W) // self.step_w)
        return max(0, min(self.project.total_steps - 1, s))

    def _x_for_step(self, step: float) -> float:
        return HEADER_W + step * self.step_w

    def _note_rect(self, li: int, note: Note) -> QRectF:
        lane = self.lanes[li]
        top = self._lane_top(li)
        x = self._x_for_step(note.step) + 0.8
        w = max(2.0, note.length * self.step_w - 1.6)
        if lane.is_drum:
            y = top + 3.5
            h = lane.height - 7.0
        else:
            y = top + lane.y_for_pitch(note.pitch)
            h = NOTE_H
        return QRectF(x, y, w, h)

    def _hit_test(self, x: float, y: float) -> tuple[int, Note] | None:
        """找到鼠标下的音符。返回 (泳道序号, 音符)。

        从后往前找 —— 后加的音符画在上面，命中优先级也应更高。
        """
        hit = self._lane_at_y(y)
        if hit is None:
            return None
        li, _ = hit
        lane = self.lanes[li]
        pad = 3.0
        for note in reversed(lane.track.notes):
            if lane.is_drum and note.pitch != lane.pitch:
                continue
            r = self._note_rect(li, note)
            if r.adjusted(-pad, -pad, pad, pad).contains(QPointF(x, y)):
                return li, note
        return None

    # -- 选区 ------------------------------------------------------------

    @property
    def selection(self) -> tuple[int, int, int, int] | None:
        return self._sel

    def select_all(self) -> None:
        if not self.lanes:
            return
        self._sel = (0, len(self.lanes) - 1, 0, self.project.total_steps - 1)
        self.update()

    def clear_selection(self) -> None:
        self._sel = None
        self.update()

    def _notes_in_selection(self) -> list[tuple[int, Note]]:
        if self._sel is None:
            return []
        l0, l1, s0, s1 = self._sel
        out: list[tuple[int, Note]] = []
        for li in range(max(0, l0), min(len(self.lanes), l1 + 1)):
            lane = self.lanes[li]
            for n in lane.track.notes:
                if not (s0 <= n.step <= s1):
                    continue
                if lane.is_drum and n.pitch != lane.pitch:
                    continue
                out.append((li, n))
        return out

    def delete_selection(self) -> None:
        """删除选中区的音符；没有选区时清空当前行。"""
        if self._sel is None:
            self.clear_selected_lane()
            return
        items = self._notes_in_selection()
        if not items:
            self.statusHint.emit("选区内没有音符")
            return
        for li, note in items:
            track = self.lanes[li].track
            try:
                track.notes.remove(note)
            except ValueError:
                pass
        self.statusHint.emit(f"已删除 {len(items)} 个音符")
        self.notesChanged.emit()
        self.update()

    def clear_selected_lane(self) -> None:
        if not self.lanes:
            return
        lane = self.lanes[self._selected_lane]
        if lane.is_drum:
            lane.track.notes = [n for n in lane.track.notes if n.pitch != lane.pitch]
        else:
            lane.track.notes = []
        self.statusHint.emit(f"已清空「{lane.label}」")
        self.notesChanged.emit()
        self.rebuild()

    # -- 复制 / 粘贴 / 复制小节 ------------------------------------------

    def copy_selection(self) -> None:
        """复制选中区（无选区时复制当前小节里的全部音符）。"""
        items = self._notes_in_selection()
        if not items:
            l0, s0 = 0, None
            items = []
        if not items:
            # 退化为"复制当前小节"
            spb = self.project.steps_per_bar
            # 以播放头所在小节为准
            bar = int((self._position / max(1e-6, self.project.seconds_per_step)) // spb)
            s0, s1 = bar * spb, bar * spb + spb - 1
            for li, lane in enumerate(self.lanes):
                for n in lane.track.notes:
                    if s0 <= n.step <= s1 and (not lane.is_drum or n.pitch == lane.pitch):
                        items.append((li, n))
            self._clipboard = [
                (li, n.step - s0, n.pitch, n.length, n.velocity, li)
                for li, n in items
            ]
            self._clip_span = spb
            self.statusHint.emit(f"已复制第 {bar + 1} 小节（{len(items)} 个音符）")
            return

        l0, l1, s0, s1 = self._sel
        self._clipboard = [
            (li, n.step - s0, n.pitch, n.length, n.velocity, li)
            for li, n in items
        ]
        self._clip_span = max(1, s1 - s0 + 1)
        self.statusHint.emit(f"已复制 {len(items)} 个音符")

    def paste(self) -> None:
        if not self._clipboard:
            self.statusHint.emit("剪贴板是空的")
            return
        at = int(round(self._position / max(1e-6, self.project.seconds_per_step)))
        limit = self.project.total_steps
        added = 0
        for _li, offset, pitch, length, vel, _ in self._clipboard:
            step = at + offset
            if step >= limit:
                continue
            # 每个音符回到它原来的泳道 —— 那才是"粘贴"该有的语义
            lane = self._lane_for(_li, pitch)
            if lane is None:
                continue
            track = lane.track
            if lane.is_drum:
                track.remove_note(step, pitch)
            else:
                track.remove_note(step, pitch)
            track.add_note(Note(step=step, pitch=pitch, length=length, velocity=vel))
            added += 1
        self.statusHint.emit(f"已粘贴 {added} 个音符")
        self.notesChanged.emit()
        self.update()

    def _lane_for(self, index: int, pitch: int) -> Lane | None:
        """粘贴时把音符放回合适的泳道（泳道可能因折叠而变了位置）。"""
        target_piece = pitch_to_drum(pitch)
        for lane in self.lanes:
            if lane.is_drum and lane.piece == target_piece:
                return lane
        if 0 <= index < len(self.lanes) and not self.lanes[index].is_drum:
            return self.lanes[index]
        return None

    def duplicate_bar(self) -> None:
        """把播放头所在的整小节复制到下一小节。"""
        spb = self.project.steps_per_bar
        step_now = self._position / max(1e-6, self.project.seconds_per_step)
        bar = int(step_now // spb)
        src0 = bar * spb
        dst0 = src0 + spb
        if dst0 + spb > self.project.total_steps:
            self.statusHint.emit("已经是最后一小节，先增加小节数")
            return

        moved = 0
        for lane in self.lanes:
            picks = [
                n for n in lane.track.notes
                if src0 <= n.step < src0 + spb and (not lane.is_drum or n.pitch == lane.pitch)
            ]
            for n in picks:
                step = n.step + spb
                lane.track.remove_note(step, n.pitch)
                lane.track.add_note(Note(step=step, pitch=n.pitch,
                                         length=n.length, velocity=n.velocity))
                moved += 1
        self.statusHint.emit(f"第 {bar + 1} 小节 → 第 {bar + 2} 小节（{moved} 个音符）")
        self.notesChanged.emit()
        self.update()

    # -- 键盘 ------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        mod = event.modifiers()
        ctrl = bool(mod & Qt.KeyboardModifier.ControlModifier)

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.editStarted.emit("删除音符")
            self.delete_selection()
        elif ctrl and key == Qt.Key.Key_A:
            self.select_all()
        elif ctrl and key == Qt.Key.Key_C:
            self.copy_selection()
        elif ctrl and key == Qt.Key.Key_V:
            self.editStarted.emit("粘贴")
            self.paste()
        elif ctrl and key == Qt.Key.Key_D:
            self.editStarted.emit("复制小节")
            self.duplicate_bar()
        elif key == Qt.Key.Key_Escape:
            self.clear_selection()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            # 方向键挪播放头一步：比拖鼠标精确，改完立刻能试听
            step = self._position / max(1e-6, self.project.seconds_per_step)
            step += -1 if key == Qt.Key.Key_Left else 1
            step = max(0, min(self.project.total_steps - 1, step))
            self.seekRequested.emit(self.project.step_time(step))
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # -- 滚轮 ------------------------------------------------------------

    def wheelEvent(self, event):
        """交给外层滚动区处理；Ctrl 时缩放。

        默认 Qt 会把滚轮事件在可滚动父级里冒泡，但这里我们让父级**横向**滚动
        成为默认（网格是横着铺开的），纵向交给 Shift —— 与多数 DAW 编排视图一致。
        """
        parent = self.parent()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parent()
        if parent is None:
            return
        mod = event.modifiers()
        dy = event.angleDelta().y()
        if mod & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if dy > 0 else 1 / 1.15
            new_w = max(MIN_STEP_W, min(MAX_STEP_W, self.step_w * factor))
            # 以鼠标位置为锚点缩放，视口内容才不会"跑掉"
            anchor_x = event.position().x()
            step_at = (anchor_x - HEADER_W) / max(1e-6, self.step_w)
            self.set_step_width(new_w)
            new_anchor = self._x_for_step(step_at)
            bar = parent.horizontalScrollBar()
            bar.setValue(bar.value() + int(new_anchor - anchor_x))
            event.accept()
            return
        if mod & Qt.KeyboardModifier.ShiftModifier:
            bar = parent.horizontalScrollBar()
            bar.setValue(bar.value() - dy)
        else:
            vbar = parent.verticalScrollBar()
            vbar.setValue(vbar.value() - dy)
        event.accept()

    # -- 鼠标 ------------------------------------------------------------

    def mousePressEvent(self, event):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = event.position()
        x, y = pos.x(), pos.y()
        button = event.button()
        mod = event.modifiers()

        # 点小节尺 = 定位播放头
        if y < RULER_H:
            if button == Qt.MouseButton.LeftButton:
                self.seekRequested.emit(self.project.step_time(self._step_at_x(x)))
            return
        # 点表头 = 选中该行 / 切换静音独奏
        if x < HEADER_W:
            hit = self._lane_at_y(y)
            if hit is not None:
                self._header_click(hit[0], x, y)
            return

        hit = self._lane_at_y(y)
        if hit is None:
            return
        li, ly = hit
        self._selected_lane = li
        lane = self.lanes[li]

        # Alt + 拖动 = 框选
        if mod & Qt.KeyboardModifier.AltModifier:
            self._mode = "marquee"
            self._drag_start = QPointF(x, y)
            self._marquee = QRectF(x, y, 0, 0)
            self._sel = None
            self.update()
            return

        note_hit = self._hit_test(x, y)

        # 拖音符右缘 = 调时值
        if note_hit is not None and button == Qt.MouseButton.LeftButton:
            _, note = note_hit
            r = self._note_rect(note_hit[0], note)
            if x >= r.right() - NOTE_GRAB_EDGE:
                self._mode = "length"
                self.editStarted.emit("调整时值")
                self._drag_note = note
                self._drag_start = QPointF(x, y)
                self._drag_origin_len = note.length
                self.statusHint.emit("拖动调整时值")
                return
            # Shift + 上下拖 = 调力度
            if mod & Qt.KeyboardModifier.ShiftModifier:
                self._mode = "velocity"
                self.editStarted.emit("调整力度")
                self._drag_note = note
                self._drag_start = QPointF(x, y)
                self._drag_origin_vel = note.velocity
                self.statusHint.emit("上下拖动调节力度")
                return

        step = self._step_at_x(x)
        pitch = lane.pitch if lane.is_drum else lane.pitch_at(ly)

        if button == Qt.MouseButton.RightButton or (mod & Qt.KeyboardModifier.ControlModifier):
            self._mode = "erase"
            self.editStarted.emit("擦除音符")
            self._erase(lane, step, pitch)
        else:
            self._mode = "paint"
            self.editStarted.emit("编辑音符")
            self._place(lane, step, pitch, ly)
        self._last_cell = (li, step, pitch)
        self.update()

    def _header_click(self, li: int, x: float, y: float) -> None:
        """表头：左边选中行，右边两个小圆点切静音/独奏。"""
        self._selected_lane = li
        lane = self.lanes[li]
        cy = self._lane_top(li) + lane.height / 2
        if not lane.is_drum and abs(y - cy) < 13:
            if x >= HEADER_W - 20:
                lane.track.solo = not lane.track.solo
            elif x >= HEADER_W - 46:
                lane.track.mute = not lane.track.mute
            self.notesChanged.emit()
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        x, y = pos.x(), pos.y()
        self._cursor_pos = pos

        hit = self._lane_at_y(y)
        self._hover = None if hit is None else (hit[0], self._step_at_x(x))

        if self._mode == "marquee":
            self._marquee = QRectF(self._drag_start, pos).normalized()
            self.update()
            return

        if self._mode == "velocity" and self._drag_note is not None:
            dy = self._drag_start.y() - y            # 往上拖 = 变响
            self._drag_note.velocity = max(0.05, min(1.0, self._drag_origin_vel + dy / 90.0))
            self.update()
            return

        if self._mode == "length" and self._drag_note is not None:
            delta = int(round((x - self._drag_start.x()) / max(1e-6, self.step_w)))
            self._drag_note.length = max(1, self._drag_origin_len + delta)
            self.update()
            return

        if self._mode in ("paint", "erase") and hit is not None:
            li, ly = hit
            lane = self.lanes[li]
            step = self._step_at_x(x)
            pitch = lane.pitch if lane.is_drum else lane.pitch_at(ly)
            cell = (li, step, pitch)
            # 同一格不重复处理 —— 否则拖过已有音符会反复增删
            if cell != self._last_cell:
                if self._mode == "erase":
                    self._erase(lane, step, pitch)
                else:
                    self._place(lane, step, pitch, ly, only_add=True)
                self._last_cell = cell
        self.update()

    def mouseReleaseEvent(self, event):
        if self._mode == "marquee" and self._marquee is not None:
            self._apply_marquee(self._marquee)
            self._marquee = None
        if self._mode == "velocity" and self._drag_note is not None:
            self.statusHint.emit(f"力度 {round(self._drag_note.velocity * 100)}%")
        if self._mode == "length" and self._drag_note is not None:
            self.statusHint.emit(f"时值 {self._drag_note.length} 步")
        if self._mode in ("paint", "erase", "velocity", "length"):
            self.notesChanged.emit()
        self._mode = ""
        self._drag_note = None
        self._last_cell = None
        self.update()

    def leaveEvent(self, event):
        self._hover = None
        self.update()
        super().leaveEvent(event)

    # -- 编辑动作 --------------------------------------------------------

    def _place(self, lane: Lane, step: int, pitch: int, y_in_lane: float,
               only_add: bool = False) -> None:
        """点一下：没有则加并试听；有则删（``only_add`` 时不动它）。"""
        track = lane.track
        if lane.is_drum:
            pitch = lane.pitch
        existing = track.note_at(step, pitch)
        if existing is not None:
            if not only_add:
                try:
                    track.notes.remove(existing)
                except ValueError:
                    pass
            return
        track.add_note(Note(step=step, pitch=pitch, length=1, velocity=0.85))
        # **边写边听**：听不到自己写的是什么，编曲就是盲写
        _play_audition(lane, pitch, 0.85)

    def _erase(self, lane: Lane, step: int, pitch: int) -> None:
        if lane.is_drum:
            pitch = lane.pitch
        lane.track.remove_note(step, pitch)

    def _apply_marquee(self, rect: QRectF) -> None:
        l0 = l1 = None
        for i in range(len(self.lanes)):
            top = self._lane_top(i)
            if top + self.lanes[i].height >= rect.top() and top <= rect.bottom():
                if l0 is None:
                    l0 = i
                l1 = i
        if l0 is None:
            self._sel = None
            self.update()
            return
        s0 = self._step_at_x(rect.left())
        s1 = self._step_at_x(rect.right())
        if s0 > s1:
            s0, s1 = s1, s0
        self._sel = (l0, l1, s0, s1)
        n = len(self._notes_in_selection())
        self.statusHint.emit(f"选中 {n} 个音符（Delete 删除 · Ctrl+C 复制）")
        self.update()

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        steps = self.project.total_steps
        spb = self.project.steps_per_bar
        grid_right = self._x_for_step(steps)
        lanes_bottom = self._lane_top(len(self.lanes)) if self.lanes else RULER_H

        painter.fillRect(self.rect(), QColor(12, 13, 18, 212))

        self._paint_lane_bands(painter, grid_right)
        self._paint_grid_lines(painter, steps, spb, grid_right, lanes_bottom)
        self._paint_loop(painter, lanes_bottom)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_selection(painter)
        for i, lane in enumerate(self.lanes):
            top = self._lane_top(i)
            if top > self.height() or top + lane.height < 0:
                continue
            self._paint_lane_notes(painter, i, lane, top)

        self._paint_marquee(painter)
        self._paint_hover(painter)
        self._paint_playhead(painter, grid_right, lanes_bottom)

        # 表头与尺子最后画，压在网格之上
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(QRectF(0, 0, HEADER_W, lanes_bottom), QColor(10, 11, 15, 242))
        painter.fillRect(QRectF(0, 0, self.width(), RULER_H), QColor(10, 11, 15, 248))
        self._paint_header(painter)
        self._paint_ruler(painter, spb)
        self._paint_readout(painter)

        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.drawLine(HEADER_W, 0, HEADER_W, int(lanes_bottom))
        painter.end()

    def _paint_lane_bands(self, painter: QPainter, grid_right: float) -> None:
        for i, lane in enumerate(self.lanes):
            top = self._lane_top(i)
            if i == self._selected_lane:
                c = QColor(lane.color)
                c.setAlphaF(0.10)
                painter.fillRect(QRectF(HEADER_W, top, grid_right - HEADER_W, lane.height), c)
            elif i % 2 == 1:
                painter.fillRect(QRectF(HEADER_W, top, grid_right - HEADER_W, lane.height),
                                 QColor(255, 255, 255, 6))

    def _paint_grid_lines(self, painter: QPainter, steps: int, spb: int,
                          grid_right: float, lanes_bottom: float) -> None:
        spbeat = self.project.steps_per_beat
        # 步太密时省掉最细的线，否则网格会糊成一片实色
        draw_fine = self.step_w >= 11.0
        for s in range(steps + 1):
            if s % spb == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 36), 1))
            elif s % spbeat == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 18), 1))
            elif draw_fine:
                painter.setPen(QPen(QColor(255, 255, 255, 8), 1))
            else:
                continue
            x = int(self._x_for_step(s))
            painter.drawLine(x, RULER_H, x, int(lanes_bottom))

        painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
        for i in range(len(self.lanes) + 1):
            y = self._lane_top(i) if i < len(self.lanes) else lanes_bottom
            painter.drawLine(HEADER_W, int(y), int(grid_right), int(y))

    def _paint_loop(self, painter: QPainter, lanes_bottom: float) -> None:
        lo, hi = self.project.loop_range
        if hi > lo:
            painter.fillRect(
                QRectF(self._x_for_step(lo), RULER_H,
                       (hi - lo) * self.step_w, lanes_bottom - RULER_H),
                QColor(255, 255, 255, 9),
            )

    def _paint_selection(self, painter: QPainter) -> None:
        if self._sel is None:
            return
        l0, l1, s0, s1 = self._sel
        top = self._lane_top(l0)
        bot = self._lane_top(l1) + self.lanes[l1].height
        rect = QRectF(self._x_for_step(s0), top,
                      (s1 - s0 + 1) * self.step_w, bot - top)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(120, 190, 255, 34))
        painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(150, 205, 255, 150), 1.2, Qt.PenStyle.DashLine))
        painter.drawRect(rect)

    def _paint_marquee(self, painter: QPainter) -> None:
        if self._marquee is None:
            return
        painter.setBrush(QColor(120, 190, 255, 40))
        painter.setPen(QPen(QColor(170, 215, 255, 190), 1.0, Qt.PenStyle.DashLine))
        painter.drawRect(self._marquee)

    def _paint_lane_notes(self, painter: QPainter, index: int, lane: Lane,
                          top: float) -> None:
        for note in lane.track.notes:
            if lane.is_drum and note.pitch != lane.pitch:
                continue
            r = self._note_rect(index, note)
            if r.right() < HEADER_W or r.left() > self.width():
                continue
            c = QColor(lane.color)
            c.setAlphaF(0.34 + 0.66 * float(max(0.0, min(1.0, note.velocity))))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawRoundedRect(r, min(2.2, r.height() / 2.0), min(2.2, r.height() / 2.0))
            # 高力度给一道亮顶边 —— 不改变颜色的前提下把力度差异再拉开一档
            if note.velocity > 0.75 and r.height() > 3.5:
                hl = QColor(255, 255, 255, 90)
                painter.setBrush(hl)
                painter.drawRect(QRectF(r.x() + 0.5, r.y() + 0.5, r.width() - 1.0, 1.0))

    def _paint_hover(self, painter: QPainter) -> None:
        if self._hover is None or self._hover[0] >= len(self.lanes):
            return
        li, st = self._hover
        lane = self.lanes[li]
        top = self._lane_top(li)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 24))
        painter.drawRoundedRect(
            QRectF(self._x_for_step(st) + 1, top + 1, self.step_w - 2, lane.height - 2),
            2.5, 2.5,
        )

    def _paint_playhead(self, painter: QPainter, grid_right: float,
                        lanes_bottom: float) -> None:
        spt = self.project.seconds_per_step
        step_f = self._position / spt if spt > 0 else 0.0
        px = self._x_for_step(step_f)
        if not (HEADER_W <= px <= grid_right):
            return
        g = QLinearGradient(px, RULER_H, px, lanes_bottom)
        g.setColorAt(0.0, theme.white(0.95))
        g.setColorAt(1.0, theme.white(0.35))
        painter.setPen(QPen(QBrush(g), 1.6))
        painter.drawLine(int(px), RULER_H, int(px), int(lanes_bottom))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.white(0.95))
        painter.drawEllipse(QRectF(px - 3.0, RULER_H - 1.0, 6.0, 6.0))

    def _paint_ruler(self, painter: QPainter, spb: int) -> None:
        painter.setFont(type_scale.caption2.font())
        for bar in range(self.project.bars):
            x = self._x_for_step(bar * spb)
            if x > self.width():
                break
            painter.setPen(QPen(labels.tertiary))
            painter.drawText(QRectF(x + 4, 0, max(20.0, spb * self.step_w - 6), RULER_H),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             str(bar + 1))
        painter.setPen(QPen(QColor(255, 255, 255, 22), 1))
        painter.drawLine(0, RULER_H - 1, self.width(), RULER_H - 1)

    def _paint_header(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for i, lane in enumerate(self.lanes):
            top = self._lane_top(i)
            h = lane.height

            if i == self._selected_lane:
                painter.setPen(Qt.PenStyle.NoPen)
                bar = QColor(lane.color)
                bar.setAlphaF(0.95)
                painter.setBrush(bar)
                painter.drawRoundedRect(QRectF(3.0, top + 3.0, 3.0, h - 6.0), 1.5, 1.5)

            if lane.is_drum:
                pm = icons.pixmap("drums" if lane.piece.startswith(("kick", "snare", "clap"))
                                  else ("hat_closed" if "hat" in lane.piece else "bell"),
                                  13, opacity=0.75, dpr=self.devicePixelRatioF())
                painter.drawPixmap(10, int(top + (h - 13) / 2), pm)
                painter.setFont(type_scale.caption1.font())
                painter.setPen(QPen(labels.secondary if i == self._selected_lane
                                    else labels.tertiary))
                painter.drawText(QRectF(28, top, HEADER_W - 96, h),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 lane.label)
            else:
                pm = icons.pixmap(lane.track.icon, 18, opacity=0.9,
                                  dpr=self.devicePixelRatioF())
                painter.drawPixmap(9, int(top + 5), pm)
                f = type_scale.caption1.font()
                f.setWeight(QFont.Weight.DemiBold)
                painter.setFont(f)
                fm = QFontMetricsF(f)
                painter.setPen(QPen(labels.label))
                painter.drawText(QRectF(31, top + 3, HEADER_W - 100, 15),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 fm.elidedText(lane.track.name, Qt.TextElideMode.ElideRight,
                                               HEADER_W - 100))
                painter.setFont(type_scale.caption2.font())
                painter.setPen(QPen(labels.quaternary))
                painter.drawText(QRectF(31, top + 18, HEADER_W - 100, 13),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 f"{dsp.pitch_name(lane.lo_pitch)} – {dsp.pitch_name(lane.hi_pitch)}")

            if not lane.is_drum:
                cy = top + h / 2 - 11
                self._paint_toggle(painter, HEADER_W - 52, cy, lane.track.mute,
                                   accents.red, "M")
                self._paint_toggle(painter, HEADER_W - 26, cy, lane.track.solo,
                                   accents.yellow, "S")

    def _paint_toggle(self, painter: QPainter, x: float, y: float, on: bool,
                      color: QColor, letter: str) -> None:
        r = QRectF(x, y, 20, 20)
        if on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(r)
        else:
            painter.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(r)
        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(theme.black(0.85) if on else labels.quaternary))
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, letter)

    def _paint_readout(self, painter: QPainter) -> None:
        """跟随光标的读数：音高名 · 步 · 力度。

        不用工具提示（tooltip）是刻意的：提示要悬停半秒才弹，还会挡住视线；
        编曲时需要的是一眼就能扫到的即时反馈。
        """
        if self._hover is None or self._hover[0] >= len(self.lanes):
            return
        li, step = self._hover
        lane = self.lanes[li]
        hit = self._lane_at_y(self._cursor_pos.y())
        if hit is None:
            return
        pitch = lane.pitch if lane.is_drum else lane.pitch_at(hit[1])
        note = lane.track.note_at(step, pitch)
        text = f"{dsp.pitch_name(pitch)} · 步 {step + 1}"
        if note is not None:
            text += f" · 力度 {round(note.velocity * 100)}% · 时值 {note.length}"

        f = type_scale.caption1.font()
        painter.setFont(f)
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text) + 16
        h = 20.0
        x = min(self._cursor_pos.x() + 14, self.width() - w - 4)
        y = self._cursor_pos.y() - h - 8
        if y < RULER_H:
            y = self._cursor_pos.y() + 14
        r = QRectF(x, y, w, h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(16, 17, 23, 235))
        painter.drawRoundedRect(r, 5, 5)
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(r, 5, 5)
        painter.setPen(QPen(labels.label))
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------


class SequencerPage(QWidget):
    """音序器页面：工具条 + 网格 + 提示条。"""

    notesEdited = Signal()
    #: 契约要求：用户改了工程内容，需要重渲染。
    #: 本页历史信号名叫 notesEdited，这里**转发**而不是改名 ——
    #: 改名字要牵动一堆已有连接，转发零风险，且让集成层能一视同仁处理 7 个页面。
    edited = Signal()
    seekRequested = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._zoom_level = 1.0

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)

        # ---- 工具条 -------------------------------------------------------
        bar = GlassPanel(self, material=theme.black(0.20), radius=radii.control)
        bar.setFixedHeight(42)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(5)

        def mk(icon, tip, slot, size=28, isize=16):
            b = GlassImageButton(icon, bar, size=size, icon_size=isize, tooltip=tip)
            b.clicked.connect(slot)
            bl.addWidget(b)
            return b

        self.btn_undo = mk("undo", "撤销 (Ctrl+Z)", self._undo)
        self.btn_redo = mk("redo", "重做 (Ctrl+Y)", self._redo)
        bl.addSpacing(6)
        self.btn_copy = mk("clipboard", "复制选区/小节 (Ctrl+C)", self._copy)
        self.btn_paste = mk("memo", "粘贴到播放头 (Ctrl+V)", self._paste)
        self.btn_dup = mk("refresh", "复制本小节到下一小节 (Ctrl+D)", self._dup_bar)
        bl.addSpacing(6)
        self.btn_fold = mk("folder", "折叠没有音符的鼓件行", self._toggle_fold)
        self.btn_clear = mk("delete", "清空当前行 (Delete)", self._clear_lane)
        bl.addStretch(1)
        self.btn_zoomout = mk("search", "缩小网格 (Ctrl+滚轮)", lambda: self._zoom(0.8), isize=15)
        self.btn_zoomin = mk("zoom_in", "放大网格 (Ctrl+滚轮)", lambda: self._zoom(1.25), isize=17)
        bl.addSpacing(6)
        self.btn_bars_less = mk("remove", "减少小节", lambda: self._change_bars(-1), isize=15)
        self.btn_bars_more = mk("add", "增加小节", lambda: self._change_bars(1), isize=15)
        self.btn_home = mk("arrow_back", "回到开头", lambda: self.seekRequested.emit(0.0))
        self._bar = bar

        # ---- 网格 ---------------------------------------------------------
        self.grid = StepGrid(project, self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidget(self.grid)
        self.scroll.setWidgetResizable(False)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:horizontal,QScrollBar:vertical{background:transparent;"
            "width:9px;height:9px;margin:0;}"
            "QScrollBar::handle{background:rgba(255,255,255,45);border-radius:4px;}"
            "QScrollBar::handle:hover{background:rgba(255,255,255,75);}"
            "QScrollBar::add-line,QScrollBar::sub-line{width:0;height:0;}"
            "QScrollBar::add-page,QScrollBar::sub-page{background:transparent;}"
        )

        # ---- 提示条 -------------------------------------------------------
        hint = GlassCard(self, radius=radii.control)
        hint.setFixedHeight(30)
        hl = QHBoxLayout(hint)
        hl.setContentsMargins(12, 0, 12, 0)
        from PySide6.QtWidgets import QLabel

        self._hint = QLabel("左键点亮（会发声）· 右键擦除 · Shift+上下拖改力度 · "
                            "拖右缘改时值 · Alt+拖框选", hint)
        self._hint.setFont(type_scale.caption1.font())
        hl.addWidget(self._hint)
        hl.addStretch(1)
        self._count = QLabel("", hint)
        self._count.setFont(type_scale.caption1.font())
        hl.addWidget(self._count)
        self._hint_bar = hint

        root.addWidget(bar)
        root.addWidget(self.scroll, 1)
        root.addWidget(hint)

        # ---- 接线 ---------------------------------------------------------
        self.grid.notesChanged.connect(self._on_notes_changed)
        self.notesEdited.connect(self.edited)
        self.grid.seekRequested.connect(self.seekRequested)
        self.grid.statusHint.connect(self._set_hint)
        # 网格在改动之前会发 editStarted —— 撤销点就压在那里，
        # 于是"拖一整笔"只占一步，而不是几十步
        self.grid.editStarted.connect(self._commit)
        self.grid.stepWidthChanged.connect(self._on_step_width_changed)
        self._apply_label_colors()
        self.rebuild()

    # -- 外观 ------------------------------------------------------------

    def _apply_label_colors(self) -> None:
        c = labels.tertiary
        self._hint.setStyleSheet(
            f"color: rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f});background:transparent;")
        c2 = labels.secondary
        self._count.setStyleSheet(
            f"color: rgba({c2.red()},{c2.green()},{c2.blue()},{c2.alphaF():.3f});background:transparent;")

    def _set_hint(self, text: str) -> None:
        self._hint.setText(text)

    # -- 与控制器对接 ----------------------------------------------------

    def bind(self, controller) -> None:
        """接上控制器：撤销栈、自动跟随都需要它。"""
        self._controller = controller
        controller.undoStateChanged.connect(self._on_undo_state)

    def _on_undo_state(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_undo.set_dimmed(not can_undo)
        self.btn_redo.set_dimmed(not can_redo)

    def _undo(self) -> None:
        ctrl = getattr(self, "_controller", None)
        if ctrl is not None:
            ctrl.undo()
            self.grid.rebuild()
            self._update_count()

    def _redo(self) -> None:
        ctrl = getattr(self, "_controller", None)
        if ctrl is not None:
            ctrl.redo()
            self.grid.rebuild()
            self._update_count()

    def _commit(self, label: str) -> None:
        """在改动**之前**压撤销点。"""
        ctrl = getattr(self, "_controller", None)
        if ctrl is not None:
            ctrl.push_undo(label)

    # -- 操作 ------------------------------------------------------------

    def _on_notes_changed(self) -> None:
        self._update_count()
        self.notesEdited.emit()

    def _update_count(self) -> None:
        total = sum(len(t.notes) for t in self.project.tracks)
        self._count.setText(f"全曲 {total} 个音符")

    def set_project(self, project: Project) -> None:
        self.project = project
        self.grid.set_project(project)
        self._update_count()

    def rebuild(self) -> None:
        self.grid.rebuild()
        self._update_count()

    def refresh_notes(self) -> None:
        self.grid.update()
        self._update_count()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        self.grid.set_position(seconds, playing)
        if playing and self.grid.follow:
            self._follow(float(seconds))

    def _follow(self, seconds: float) -> None:
        """让播放头留在视口内。

        留出左右各 15% 的余量再滚 —— 卡在边缘就滚会让画面持续抽动，
        预判一点滚动观感稳得多。
        """
        spt = self.project.seconds_per_step
        if spt <= 0:
            return
        step = seconds / spt
        x = HEADER_W + step * self.grid.step_w
        hbar = self.scroll.horizontalScrollBar()
        left = hbar.value() + HEADER_W
        right = hbar.value() + self.scroll.viewport().width()
        margin = max(60.0, (right - left) * 0.15)
        if x < left + margin or x > right - margin:
            target = int(x - (right - left) * 0.35)
            hbar.setValue(max(0, target))

    def _clear_lane(self) -> None:
        self._commit("清空行")
        self.grid.clear_selected_lane()

    def _copy(self) -> None:
        self.grid.copy_selection()

    def _paste(self) -> None:
        self.grid.paste()

    def _dup_bar(self) -> None:
        self.grid.duplicate_bar()

    def _toggle_fold(self) -> None:
        self.grid.set_fold_drums(not self.grid.fold_drums)
        self.btn_fold.set_style_kind("tinted" if self.grid.fold_drums else "plain")
        self._update_count()

    def _zoom(self, factor: float) -> None:
        # 以 **grid 的真实步宽** 为基准累积，而不是以 _zoom_level 为基准 ——
        # 这样即便步宽是被滚轮改过的，工具条按钮也不会算错方向
        target = self.grid.step_w * factor
        self.grid.set_step_width(target)
        self._zoom_level = self.grid.step_w / DEFAULT_STEP_W

    def _on_step_width_changed(self, px: float) -> None:
        """把 `_zoom_level` 同步成真实步宽的读数。

        只做读数镜像，**不反向去改步宽** —— 否则又会绕回去变成两个写入者。
        """
        self._zoom_level = float(px) / DEFAULT_STEP_W

    def _change_bars(self, delta: int) -> None:
        bars = max(1, min(64, self.project.bars + delta))
        if bars == self.project.bars:
            return
        self._commit("修改小节数")
        self.project.bars = bars
        limit = self.project.total_steps
        for track in self.project.tracks:
            track.notes = [n for n in track.notes if n.step < limit]
        self.grid.rebuild()
        self.notesEdited.emit()
        self._update_count()

__all__ = ["SequencerPage", "StepGrid", "Lane", "build_lanes"]
