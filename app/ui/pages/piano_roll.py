"""钢琴卷帘 —— 单条旋律轨的精修台：音高、时值、力度。

为什么必须单独做一页
--------------------
音序器里那条"压缩钢琴卷帘"是给**结构**用的：一屏能看完整首曲子，代价是
旋律行只有 46px 高，一个半音摊不到 2px —— 想改音高基本靠碰运气。
"看结构"和"改细节"是两个尺度的活，硬塞进一个视图里两边都做不好。
所以这里独立成一页，把尺度彻底拉开：

* **纵向**：一个半音一行（默认 14px，Ctrl+滚轮可调），键盘在左侧常驻；
* **横向**：一个 step 一列，步 / 拍 / 小节三种明度分开；
* **底部**：力度条 —— 力度不再是个看不见的数字，而是一排能拖的柱子。

鼠标语义
--------
和音序器**刻意保持一致**的只有"右键擦除""Shift+上下拖改力度"两条；
其余按钢琴卷帘的通行约定走（点音符 = 删），因为这里的操作密度高得多，
再让"点一下"变成"选中"就会多出一次无意义的点击。

======================================================  ==========================
左键点空格子                                            画一个 1 步音符，**并立刻试听**
左键点已有音符                                          **删掉它**
拖音符中间上下                                          **改音高**（卷帘的核心操作）
拖音符右缘 6px                                          **改时值**
Shift + 上下拖                                          **改力度**
右键（可按住拖动）                                      **擦除**
Ctrl + 点音符                                           加入 / 移出选区（不删）
Alt + 拖动                                              **框选**
点左侧键盘                                              试听该音高，**播放头不动**
======================================================  ==========================

最后一条是刻意设计的：试音是为了"确认这个音高对不对"，如果顺手把播放头
挪走，正在播放的曲子就被打断了 —— 试听是只读动作，不该有副作用。

键盘：`Delete` 删除选中 · `Ctrl+A` 全选 · `↑/↓` 选中音符升降半音 ·
`Shift+↑/↓` 升降八度 · `←/→` 挪播放头一步 · `Ctrl+Z/Y` 撤销重做
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen, QPolygonF,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import dsp
from app.audio.player import audition
from app.audio.project import Note, Project, Track
from app.audio.synth import render_synth_note
from app.theme import accents, labels, radii, spacing, type_scale
from app.widgets.glass import (
    GlassCard, GlassChip, GlassImageButton, GlassPanel, make_label,
)

# ==========================================================================
# 尺寸与常量
# ==========================================================================

KEY_W = 62                  # 左侧键盘宽度：要放得下 "C#4" 这种带升号的全名
RULER_H = 24                # 顶部小节尺高度
VEL_H = 62                  # 底部力度条高度

ROW_H = 14.0                # 一个半音一行。14px 时音符还有 13px 实体，好点也好拖
MIN_ROW_H = 8.0             # 更矮就分辨不出相邻半音了
MAX_ROW_H = 32.0

STEP_W = 18.0               # 一个 step 一列（默认 1/16 音符）
MIN_STEP_W = 5.0
MAX_STEP_W = 44.0

NOTE_EDGE = 6.0             # 音符右缘多少像素内算"抓时值"
DRAG_SLOP = 3.0             # 位移小于这个值算"点击"而不是"拖动"

AUDITION_SECONDS = 0.35     # 试听发声时长
VEL_DRAG_SPAN = 130.0       # Shift 上下拖时，多少像素对应满力度

PITCH_MARGIN = 4            # 音域上下各留几个半音的余量
MIN_SPAN = 24               # 至少覆盖两个八度
DEFAULT_LO, DEFAULT_HI = 48, 72

BLACK_KEYS = frozenset({1, 3, 6, 8, 10})
BLACK_KEY_W = 0.60          # 黑键画到键盘宽度的百分之多少（其余露出底色）

#: 三种网格明度。步线必须够淡，否则 128 列铺满时整块网格会糊成灰板
LINE_STEP = QColor(255, 255, 255, 13)
LINE_BEAT = QColor(255, 255, 255, 26)
LINE_BAR = QColor(255, 255, 255, 58)

GRID_BG = QColor(12, 13, 18, 214)
KEY_BG = QColor(10, 11, 15, 244)


# ==========================================================================
# 纯函数（不碰 Qt，方便单独验证）
# ==========================================================================


def is_black_key(pitch: int) -> bool:
    """黑键 = 音名里带升号的那五个。"""
    return (int(pitch) % 12) in BLACK_KEYS


def synth_tracks_of(project: Project) -> list[Track]:
    """页面只编辑 kind=='synth' 的轨道 —— 鼓轨该用鼓机页，不是卷帘。"""
    return [t for t in project.tracks if t.kind == "synth"]


def pitch_span(track: Track | None) -> tuple[int, int]:
    """按轨道已有音符算纵向音域。

    两头各留 ``PITCH_MARGIN`` 个半音：贴着边缘画音符时，若不留余量，
    刚写下的音就会掉到视野外。空轨或音域过窄时补到两个八度 ——
    只有两三个音符时严格按音符算的话网格会窄得像一条缝，上下移调都没地方去。
    """
    pitches = [int(n.pitch) for n in track.notes] if track is not None else []
    if pitches:
        lo, hi = min(pitches) - PITCH_MARGIN, max(pitches) + PITCH_MARGIN
    else:
        lo, hi = DEFAULT_LO, DEFAULT_HI

    span = hi - lo
    if span < MIN_SPAN:
        pad = (MIN_SPAN - span + 1) // 2
        lo -= pad
        hi += pad
    lo = max(0, lo)
    hi = min(127, hi)
    if hi - lo < 1:            # 极端兜底：音高贴在 MIDI 边界上时
        hi = min(127, lo + 1)
    return int(lo), int(hi)


def note_covering(track: Track | None, step: int, pitch: int) -> Note | None:
    """找**覆盖**该格子的音符。

    不能用 ``Track.note_at`` —— 那个只匹配音符的起始步。一个 4 步长的音符
    在第 2 步上也是"有音符的"，拖动、擦除、点删都必须认得出来。
    """
    if track is None:
        return None
    for n in track.notes:
        if n.pitch == int(pitch) and n.step <= step < n.step + max(1, n.length):
            return n
    return None


def snap_to_unit(value: int, unit: int) -> int:
    """把某个量吸附到 unit 的整数倍上。

    用 ``floor(x + 0.5)`` 而不是内置 ``round`` —— 后者是银行家舍入
    （round(2.5) == 2），量化这种"四舍五入"语义里会导致同一批音符
    有的往上有的往下，听感上非常怪。
    """
    unit = max(1, int(unit))
    return int(math.floor(value / unit + 0.5)) * unit


def quantize_unit(project: Project, grid: int = 16) -> int:
    """把"1/grid 音符"换算成本工程的 step 数。

    **这里必须通用换算，不能写死 1**：默认工程 ``steps_per_beat=4``，
    一个 step 正好是 1/16，量化到 1/16 就是恒等变换（一个整数 step 本来就
    落在 1/16 网格上）。但工程完全可以有 ``steps_per_beat=8``（1/32 精度），
    那时 1/16 网格 = 2 个 step，量化才真正起作用。
    """
    # 一个 step 是 1/(steps_per_beat*4) 个全音符，目标是 1/grid 个全音符
    unit = project.steps_per_beat * 4.0 / max(1, int(grid))
    return max(1, int(round(unit)))


def render_preview(track: Track | None, pitch: int, velocity: float,
                   seconds: float = AUDITION_SECONDS) -> np.ndarray | None:
    """渲染一个音用于试听。

    只合这一个音，不做整轨渲染（那要几百毫秒，边写边听必须在一瞬间）。
    """
    if track is None:
        return None
    try:
        return render_synth_note(track.synth, int(pitch), float(velocity), float(seconds))
    except Exception:
        return None


def play_preview(track: Track | None, pitch: int, velocity: float) -> float:
    """渲染 + 试听，返回缓冲峰值。

    返回峰值而不是 None 是刻意的：测试与"真的发声了吗"的自检都需要一个
    可断言的东西，而"调用没报错"证明不了任何事（缓冲可能是全零）。
    """
    buf = render_preview(track, pitch, velocity)
    if buf is None or getattr(buf, "size", 0) == 0:
        return 0.0
    peak = float(np.abs(buf).max())
    try:
        player = audition()
        if player is not None:
            player.play_note(buf)
    except Exception:
        pass                    # 没声卡 / 设备忙都不该让界面崩
    return peak


# ==========================================================================
# 左侧：钢琴键盘
# ==========================================================================


class PianoKeyboard(QWidget):
    """纵向钢琴键盘。

    它不是一个"装饰图"：**每一行都能点响**。写旋律时最常做的动作是
    "先听一下这个音高，再把音符放上去"，所以键盘必须在手边，而不是
    藏在某个菜单里。

    黑键只画到宽度的 60%，露出右侧底色 —— 这是真钢琴的视觉投影，
    一眼就能看出哪些行是黑键行；网格那边也用同样的规则给行底色分级。
    """

    notePressed = Signal(int)      # 需要试听的音高

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(KEY_W)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self._lo, self._hi = DEFAULT_LO, DEFAULT_HI
        self._row_h = ROW_H
        self._offset = 0.0
        self._inset = 0            # 右下角滚动条占掉的高度，画的时候要让出来
        self._pressed = -1
        self._hover = -1

    # -- 视图 ------------------------------------------------------------

    def set_view(self, lo: int, hi: int, row_h: float, offset: float,
                 inset: int = 0) -> None:
        self._lo, self._hi = int(lo), int(hi)
        self._row_h = max(MIN_ROW_H, float(row_h))
        self._offset = float(offset)
        self._inset = int(inset)
        self.update()

    def pitch_at_y(self, y: float) -> int:
        """屏幕 y → 音高（含滚动偏移）。超出范围会被钳到两端。"""
        p = self._hi - int((y + self._offset) // max(1.0, self._row_h))
        return max(self._lo, min(self._hi, p))

    def y_for_pitch(self, pitch: int) -> float:
        return (self._hi - int(pitch)) * self._row_h - self._offset

    # -- 交互 ------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        p = self.pitch_at_y(event.position().y())
        self._pressed = p
        self.notePressed.emit(p)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        p = self.pitch_at_y(pos.y())
        changed = False
        if p != self._hover:
            self._hover = p
            changed = True
        # 按住拖动 = 滑奏试听：想找"哪个音高对"时，一路划过去比一个个点快得多
        if self._pressed >= 0 and (event.buttons() & Qt.MouseButton.LeftButton):
            if p != self._pressed:
                self._pressed = p
                self.notePressed.emit(p)
                changed = True
        if changed:
            self.update()

    def mouseReleaseEvent(self, event):
        if self._pressed >= 0:
            self._pressed = -1
            self.update()

    def leaveEvent(self, event):
        self._pressed = -1
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        h = max(0, self.height() - self._inset)
        painter.fillRect(self.rect(), KEY_BG)

        black_w = KEY_W * BLACK_KEY_W
        small = self._row_h >= 11.0
        font = type_scale.caption2.font()
        fm = QFontMetricsF(font)
        painter.setFont(font)

        for p in range(self._lo, self._hi + 1):
            y = self.y_for_pitch(p)
            if y + self._row_h < 0 or y > h:
                continue
            row = QRectF(0.0, y, KEY_W, self._row_h - 0.6)
            black = is_black_key(p)

            if black:
                # 黑键画成"更短的一条"，右侧露出键缝底色 —— 这是真钢琴的视觉投影，
                # 一眼就能看出哪些行是黑键行，而不是只靠明暗差去猜。
                painter.fillRect(row, QColor(30, 32, 40, 255))
                painter.fillRect(QRectF(0.0, y, black_w, self._row_h - 0.6),
                                 QColor(11, 12, 16, 255))
                painter.setPen(QPen(QColor(255, 255, 255, 30), 1.0))
                painter.drawLine(int(black_w) - 1, int(y), int(black_w) - 1,
                                 int(y + self._row_h - 0.6))
            else:
                painter.fillRect(row, QColor(231, 234, 241, 236))

            # C 音要一眼看得到：整行加一道强调色横线 + 音名用强调色
            if p % 12 == 0:
                painter.setPen(Qt.PenStyle.NoPen)
                c = QColor(accents.blue)
                c.setAlphaF(0.95)
                painter.setBrush(c)
                painter.drawRect(QRectF(0.0, y, KEY_W, 1.6))

            if p == self._pressed:
                # 按下态只覆盖"琴键实体"，不覆盖黑键右侧的键缝，否则按下的
                # 一瞬间行的形状会变，反而看不出按的是哪个键
                c = QColor(accents.blue)
                c.setAlphaF(0.55 if black else 0.38)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(c)
                painter.drawRect(QRectF(0.0, y, black_w if black else KEY_W,
                                        self._row_h - 0.6))
            elif p == self._hover and not black:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.white(0.10))
                painter.drawRect(row)

            if not small:
                continue
            # 音名画在各自键内靠右：白键上写深色字，黑键上写浅色字。
            # 升号音名比白键长一位，黑键只有 60% 宽，字号再大就溢出了。
            name = dsp.pitch_name(p) if p % 12 == 0 else dsp.pitch_name(p, with_octave=False)
            if p % 12 == 0:
                f = type_scale.caption2.font()
                f.setWeight(QFont.Weight.DemiBold)
                painter.setFont(f)
                painter.setPen(QPen(QColor(accents.blue).darker(115)))
            else:
                painter.setFont(font)
                if black:
                    painter.setPen(QPen(QColor(196, 202, 214, 175)))
                else:
                    painter.setPen(QPen(QColor(96, 102, 116, 235)))
            box = QRectF(2.0, y, KEY_W - 6.0, self._row_h)
            painter.drawText(box, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             fm.elidedText(name, Qt.TextElideMode.ElideRight, KEY_W - 8.0))

        # 键盘与网格的分界线：没有它，白键行的浅色会和网格底色糊在一起
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
        painter.drawLine(int(KEY_W) - 1, 0, int(KEY_W) - 1, int(h))
        painter.end()


# ==========================================================================
# 顶部：小节尺
# ==========================================================================


class StepRuler(QWidget):
    """小节尺。点一下 = 把播放头挪过去 —— 编曲时最频繁的定位动作。"""

    seekRequested = Signal(int)     # step

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(RULER_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._step_w = STEP_W
        self._offset = 0.0
        self._total_steps = 128
        self._spb = 16
        self._spbeat = 4
        self._playhead = 0.0

    def set_view(self, step_w: float, offset: float, total_steps: int,
                 steps_per_bar: int, steps_per_beat: int) -> None:
        self._step_w = max(1.0, float(step_w))
        self._offset = float(offset)
        self._total_steps = max(1, int(total_steps))
        self._spb = max(1, int(steps_per_bar))
        self._spbeat = max(1, int(steps_per_beat))
        self.update()

    def set_playhead(self, step: float) -> None:
        self._playhead = float(step)
        self.update()

    def step_at_x(self, x: float) -> int:
        s = int((x + self._offset) // self._step_w)
        return max(0, min(self._total_steps - 1, s))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.seekRequested.emit(self.step_at_x(event.position().x()))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), KEY_BG)

        # 刻度：拍线短、小节线长，和网格的三种明度呼应
        first = int(self._offset // self._step_w)
        last = int((self._offset + self.width()) // self._step_w) + 1
        painter.setFont(type_scale.caption2.font())
        for s in range(max(0, first), min(self._total_steps, last) + 1):
            x = s * self._step_w - self._offset
            if s % self._spb == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
                painter.drawLine(int(x), 4, int(x), RULER_H)
                painter.setPen(QPen(labels.tertiary))
                painter.drawText(QRectF(x + 4, 1, max(24.0, self._spb * self._step_w - 8), RULER_H - 2),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 str(s // self._spb + 1))
            elif s % self._spbeat == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 32), 1))
                painter.drawLine(int(x), RULER_H - 8, int(x), RULER_H)

        # 播放头标记：卷帘里播放头是纵向的一条线，尺子上得有个"头"才好认
        px = self._playhead * self._step_w - self._offset
        if 0 <= px <= self.width():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.white(0.95))
            painter.drawPolygon(QPolygonF([QPointF(px - 4.5, 1.0),
                                           QPointF(px + 4.5, 1.0),
                                           QPointF(px, 8.0)]))

        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.drawLine(0, RULER_H - 1, self.width(), RULER_H - 1)
        painter.end()


# ==========================================================================
# 主区：卷帘网格
# ==========================================================================


class RollGrid(QWidget):
    """卷帘网格本体：画音符、处理全部编辑操作。

    坐标约定
    --------
    * 横向 ``x = step * step_w``，**不含任何表头偏移** —— 滚动偏移由外层
      滚动区的滚动条承担，尺子/力度条各自减去同一个偏移量就能对齐。
      把几块视图的偏移量统一成"滚动条的值"，是避免对齐 bug 的最省心做法。
    * 纵向 ``y = (hi_pitch - pitch) * row_h``，**上高下低**。
    """

    notesChanged = Signal()
    seekRequested = Signal(float)
    statusHint = Signal(str)
    auditioned = Signal(int, float)     # (音高, 缓冲峰值) —— 峰值让测试能断言"真的发声了"
    #: 一次编辑**真的要改数据**时发一次 ``editStarted(标签)``。
    #: 刻意不是"按下鼠标那一刻"：撤销点拍的是"改动前的工程"，所以它必须压在改动
    #: 之前，而"按下"本身什么都还没改 —— 按下去就发的话，"拖出去又拖回原位"、
    #: "右键点空格子"这种净变化为零的手势会在撤销栈里留下一个按了没反应的格子
    #: （用户连按几次撤销都是"没反应"的一步）。一整笔拖动仍然只发一次。
    editStarted = Signal(str)
    #: 已经压过撤销点的那次编辑，最后**一个音符都没动**（移调全被占位挡住了）。
    #: 页面收到后把那个推测性的空撤销点抹掉（``AppController.pop_undo``）。
    editAborted = Signal()

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.track: Track | None = None
        self.lo_pitch, self.hi_pitch = DEFAULT_LO, DEFAULT_HI
        self.row_h = ROW_H
        self.step_w = STEP_W

        self.default_length = 1
        self.default_velocity = 0.85

        # 选区用 (step, pitch) 键而不是 Note 对象：撤销会整份重建工程，
        # 按对象身份记的选区一次撤销就全失效了，按键记还能自己重新对上。
        self.selected: set[tuple[int, int]] = set()
        self._primary: tuple[int, int] | None = None

        self._mode = ""             # paint/erase/length/velocity/pitch/marquee
        self._drag_note: Note | None = None
        self._drag_start = QPointF(0, 0)
        self._origin_len = 1
        self._origin_vel = 0.85
        self._origin_pitch = 60
        self._moved = False
        self._last_cell: tuple[int, int] | None = None
        self._marquee: QRectF | None = None
        self._marquee_base: set[tuple[int, int]] = set()
        #: 这一笔手势**真的改到数据了**没有。
        #: 不能按"模式"猜（原来那套 `mode in ("paint","erase",...)` 就是把
        #: 拖动改音高漏掉、于是拖完音高一条 edited 都不发的根因）：只有真的
        #: 落到 project 上的改动才算数，松手时也照它决定要不要发 notesChanged。
        self._edit_dirty = False
        #: 拖动手势开始前那个音符的 (音高, 时值, 力度)，用来判断"净变化为零"。
        self._drag_origin: tuple[int, int, float] | None = None
        self._cursor = QPointF(0, 0)
        self._hover: tuple[int, int] | None = None

        self._position = 0.0
        self._playing = False
        self.follow = True

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -- 数据 ------------------------------------------------------------

    def set_project(self, project: Project) -> None:
        self.project = project

    def set_track(self, track: Track | None, lo: int | None = None,
                  hi: int | None = None) -> None:
        """换轨道。音域必须跟着重算 —— 各条轨的音区差一个八度很常见。"""
        self.track = track
        self.selected.clear()
        self._primary = None
        self._cancel_drag()
        if lo is None or hi is None:
            lo, hi = pitch_span(track)
        self.set_pitch_range(lo, hi)

    def set_pitch_range(self, lo: int, hi: int) -> None:
        self.lo_pitch = int(max(0, min(127, lo)))
        self.hi_pitch = int(max(self.lo_pitch + 1, min(127, hi)))
        self._resize_to_content()

    def set_step_width(self, px: float) -> None:
        self.step_w = max(MIN_STEP_W, min(MAX_STEP_W, float(px)))
        self._resize_to_content()

    def set_row_height(self, px: float) -> None:
        self.row_h = max(MIN_ROW_H, min(MAX_ROW_H, float(px)))
        self._resize_to_content()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        self._position = float(seconds)
        self._playing = bool(playing)
        self.update()

    @property
    def playhead_step(self) -> float:
        spt = self.project.seconds_per_step
        return self._position / spt if spt > 0 else 0.0

    def rows(self) -> int:
        return self.hi_pitch - self.lo_pitch + 1

    def _resize_to_content(self) -> None:
        w = int(self.project.total_steps * self.step_w)
        h = int(self.rows() * self.row_h)
        self.setFixedSize(max(1, w), max(1, h))
        self.update()

    # -- 坐标换算（公开，测试与兄弟控件都靠它，避免各算各的）------------

    def x_for_step(self, step: float) -> float:
        return float(step) * self.step_w

    def y_for_pitch(self, pitch: int) -> float:
        return (self.hi_pitch - int(pitch)) * self.row_h

    def step_at_x(self, x: float) -> int:
        s = int(x // self.step_w)
        return max(0, min(self.project.total_steps - 1, s))

    def pitch_at_y(self, y: float) -> int:
        p = self.hi_pitch - int(y // self.row_h)
        return max(self.lo_pitch, min(self.hi_pitch, p))

    def note_rect(self, note: Note) -> QRectF:
        x = self.x_for_step(note.step) + 0.5
        w = max(3.0, max(1, note.length) * self.step_w - 1.0)
        y = self.y_for_pitch(note.pitch) + 0.5
        return QRectF(x, y, w, max(2.0, self.row_h - 1.0))

    # -- 选区 ------------------------------------------------------------

    def selected_notes(self) -> list[Note]:
        if self.track is None:
            return []
        return [n for n in self.track.sorted_notes() if (n.step, n.pitch) in self.selected]

    def primary_note(self) -> Note | None:
        """力度条的默认目标：优先"最后点的那个"，否则选区里最靠前的。"""
        if self.track is None:
            return None
        key = self._primary
        if key is None or key not in self.selected:
            key = sorted(self.selected)[0] if self.selected else None
        if key is None:
            return None
        return self.track.note_at(key[0], key[1])

    def set_primary(self, note: Note | None) -> None:
        if note is None:
            return
        self._primary = (note.step, note.pitch)
        self.selected = {self._primary}
        self.update()

    def toggle_selected(self, note: Note) -> None:
        key = (note.step, note.pitch)
        if key in self.selected:
            self.selected.discard(key)
            if self._primary == key:
                self._primary = None
        else:
            self.selected.add(key)
            self._primary = key
        self.update()

    def select_all(self) -> int:
        if self.track is None:
            return 0
        self.selected = {(n.step, n.pitch) for n in self.track.notes}
        self._primary = sorted(self.selected)[0] if self.selected else None
        self.update()
        return len(self.selected)

    def clear_selection(self) -> None:
        self.selected.clear()
        self._primary = None
        self.update()

    def prune_selection(self) -> None:
        """把选区里已经不存在的音符键丢掉（撤销/量化/删除之后）。"""
        if self.track is None:
            self.selected.clear()
            self._primary = None
            return
        live = {(n.step, n.pitch) for n in self.track.notes}
        self.selected &= live
        if self._primary not in self.selected:
            self._primary = None

    def _rekey(self, old: Note, new_pitch: int) -> None:
        """音符改了音高/起始步之后，选区键要跟着走，否则选区会"漏气"。"""
        old_key = (old.step, old.pitch)
        new_key = (old.step, new_pitch)
        if old_key in self.selected:
            self.selected.discard(old_key)
            self.selected.add(new_key)
        if self._primary == old_key:
            self._primary = new_key

    # -- 编辑动作 --------------------------------------------------------

    def draw_note(self, step: int, pitch: int, *, audition_it: bool = True) -> Note | None:
        """在空格子写一个音符；按题目要求**画的同时试听该音高**。"""
        if self.track is None:
            return None
        step = max(0, min(self.project.total_steps - 1, int(step)))
        pitch = max(0, min(127, int(pitch)))
        # 用 note_covering 而不是 note_at：一个长音符已经盖住这一格时不能再叠一个。
        # 同音高上叠两个音符在钢琴卷帘里是纯粹的数据事故（渲染时会打架）。
        if note_covering(self.track, step, pitch) is not None:
            return None
        note = self.track.add_note(Note(step=step, pitch=pitch,
                                        length=int(self.default_length),
                                        velocity=float(self.default_velocity)))
        if audition_it:
            # **边写边听**：听不到自己写的是什么，写旋律就是盲写
            peak = play_preview(self.track, pitch, note.velocity)
            self.auditioned.emit(int(pitch), peak)
        return note

    def erase_note(self, step: int, pitch: int) -> bool:
        """擦掉**覆盖**这一格的音符。

        不能走 ``Track.remove_note``：它只匹配音符的**起始步**，右键点一个长音符
        的中段会一点反应都没有 —— 而模块顶部写着"擦除也要认得长音符"，同族的
        悬停读数、拖动、点删也都认得它。认得出却删不掉，用户只会以为这个音删不干净。
        """
        if self.track is None:
            return False
        note = note_covering(self.track, step, pitch)
        if note is None:
            return False
        try:
            self.track.notes.remove(note)
        except ValueError:
            return False
        return True

    def delete_selected(self) -> int:
        """删除选中的音符；没有选区时不动（卷帘里不该有"猜用户想删什么"的行为）。"""
        notes = self.selected_notes()
        if self.track is None or not notes:
            self.statusHint.emit("没有选中的音符")
            return 0
        for n in notes:
            try:
                self.track.notes.remove(n)
            except ValueError:
                pass
        self.selected.clear()
        self._primary = None
        self.statusHint.emit(f"已删除 {len(notes)} 个音符")
        self.update()
        return len(notes)

    def transpose(self, delta: int, *, notes: list[Note] | None = None) -> int:
        """整体移调。``notes`` 为 None 时作用于**选区，选区为空则作用于全部**。

        目标位置已经被别的音符占着就跳过它：宁可少移一个音，也不要悄悄
        吃掉用户写的另一个音 —— 那种数据丢失是撤销也救不回直觉的。
        """
        if self.track is None:
            return 0
        delta = int(delta)
        if delta == 0:
            return 0
        if notes is None:
            notes = self.selected_notes() or list(self.track.notes)
        moved = 0
        for n in notes:
            target = max(0, min(127, int(n.pitch) + delta))
            if target == n.pitch:
                continue
            occupant = self.track.note_at(n.step, target)
            if occupant is not None and occupant is not n:
                continue
            self._rekey(n, target)
            n.pitch = target
            moved += 1
        if moved:
            self.set_pitch_range(*pitch_span(self.track))
            self.statusHint.emit(f"{'升' if delta > 0 else '降'} {abs(delta)} 个半音（{moved} 个音符）")
        else:
            self.statusHint.emit("没有可移调的音符")
        self.update()
        return moved

    def quantize(self, grid: int = 16, *, notes: list[Note] | None = None) -> int:
        """吸附到 1/grid 网格。``notes`` 为 None 时作用于选区，空则作用于全部。

        撞车（两个音符被吸到同一格）时保留力度大的那个：量化把两笔并到一起，
        响的那一笔通常才是用户真正想留下的。
        """
        if self.track is None:
            return 0
        unit = quantize_unit(self.project, grid)
        if notes is None:
            pool = self.selected_notes() or list(self.track.notes)
        else:
            pool = list(notes)
        if not pool:
            self.statusHint.emit("没有可量化的音符")
            return 0

        limit = self.project.total_steps
        changed = 0
        kept: dict[tuple[int, int], Note] = {}
        others = [n for n in self.track.notes if n not in pool]
        for n in others:
            kept.setdefault((n.step, n.pitch), n)
        for n in sorted(pool, key=lambda x: (x.step, x.pitch)):
            target = max(0, min(limit - 1, snap_to_unit(n.step, unit)))
            key = (target, n.pitch)
            if key in kept:
                if n.velocity > kept[key].velocity:
                    kept[key] = n
                changed += 1
                continue
            if target != n.step:
                changed += 1
            n.step = target
            kept[key] = n

        self.track.notes = sorted(kept.values(), key=lambda x: (x.step, x.pitch))
        self.prune_selection()
        self.statusHint.emit(
            f"量化到 1/{grid}（{'变更 ' + str(changed) + ' 个音符' if changed else '已在网格上'}）"
        )
        self.update()
        return changed

    # -- 键盘 ------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        mod = event.modifiers()
        ctrl = bool(mod & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mod & Qt.KeyboardModifier.ShiftModifier)

        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            # 和鼠标那套同一个原则：没有选区就什么都删不掉，别压撤销点
            if self.selected:
                self._begin_edit("删除音符")
                if self.delete_selected():
                    self.notesChanged.emit()
            else:
                self.statusHint.emit("没有选中的音符")
        elif ctrl and key == Qt.Key.Key_A:
            self.select_all()
        elif key == Qt.Key.Key_Escape:
            self.clear_selection()
        elif key == Qt.Key.Key_Up:
            self.editStarted.emit("升降八度" if shift else "升降半音")
            if self.transpose(12 if shift else 1):
                self.notesChanged.emit()
            else:
                # 一个音符都没移成（目标音高全被占着）→ 让页面抹掉那个空撤销点
                self.editAborted.emit()
        elif key == Qt.Key.Key_Down:
            self.editStarted.emit("升降八度" if shift else "升降半音")
            if self.transpose(-12 if shift else -1):
                self.notesChanged.emit()
            else:
                self.editAborted.emit()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            # 方向键挪播放头一步：比拖鼠标准，改完立刻能试听
            step = self.playhead_step + (-1 if key == Qt.Key.Key_Left else 1)
            step = max(0, min(self.project.total_steps - 1, step))
            self.seekRequested.emit(self.project.step_time(step))
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # -- 滚轮 ------------------------------------------------------------

    def _scroll_area(self) -> QScrollArea | None:
        p = self.parent()
        while p is not None and not isinstance(p, QScrollArea):
            p = p.parent()
        return p

    def wheelEvent(self, event):
        """Ctrl+滚轮 = 缩放；Shift+滚轮 = 横向滚动；否则交给滚动区纵向滚。

        以光标为锚点缩放：不锚定的话，放大时内容会"跑掉"，
        用户得重新找刚才看的那个音 —— 那是缩放最惹人烦的地方。
        """
        area = self._scroll_area()
        if area is None:
            return
        mod = event.modifiers()
        dy = event.angleDelta().y()
        if mod & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if dy > 0 else 1 / 1.15
            pos = event.position()
            if mod & Qt.KeyboardModifier.ShiftModifier:
                pitch = self.pitch_at_y(pos.y())
                self.set_row_height(self.row_h * factor)
                area.verticalScrollBar().setValue(int(self.y_for_pitch(pitch)))
            else:
                step_at = (pos.x()) / max(1e-6, self.step_w)
                self.set_step_width(self.step_w * factor)
                anchor = step_at * self.step_w
                area.horizontalScrollBar().setValue(int(anchor - pos.x()))
            event.accept()
            return
        if mod & Qt.KeyboardModifier.ShiftModifier:
            bar = area.horizontalScrollBar()
            bar.setValue(bar.value() - dy)
            event.accept()
            return
        super().wheelEvent(event)

    # -- 鼠标 ------------------------------------------------------------

    def _cancel_drag(self) -> None:
        self._mode = ""
        self._drag_note = None
        self._moved = False
        self._last_cell = None
        self._marquee = None
        self._drag_origin = None
        self._edit_dirty = False

    def _begin_edit(self, label: str) -> None:
        """在**这一笔手势的第一次真实改动之前**补发一次 ``editStarted``。

        撤销点拍的是"当前工程"，所以必须在改数据之前发。原来是"按下鼠标就发"，
        于是"点一下又拖回原位""右键点空格子"这类净变化为零的手势也会在撤销栈里
        留下一个空步骤 —— 用户连按几次撤销，看到的是界面一动不动的几步，比少压
        一次更让人困惑。``_edit_dirty`` 挡住后续重复：一整笔拖动仍然只占一步。
        """
        if not self._edit_dirty:
            self.editStarted.emit(label)

    def _commit_drag(self) -> None:
        """整笔拖动结束时才提交撤销点，且只在这笔手势真的改了数据时才提交。

        两个坑都在这里解决：
        1) **净变化为零**（拖出去又拖回原音高）不该留撤销点；
        2) 撤销点又必须压在改动**之前**，所以真改了的时候先把数据退回按下时的
           样子、让页面压点、再改回去。中间那一步不重绘也不通知任何人，用户看不见。
        """
        note, origin = self._drag_note, self._drag_origin
        if note is None or origin is None:
            return                  # paint/erase 在按下那一刻就提交过了
        if not self._edit_dirty:
            return
        now = (int(note.pitch), int(note.length), float(note.velocity))
        if now == origin:
            self._edit_dirty = False    # 这一笔等于什么都没做
            return
        label = {"pitch": "移动音高", "length": "调整时值",
                 "velocity": "调整力度"}.get(self._mode, "编辑音符")
        self._apply_note_state(note, origin)
        self.editStarted.emit(label)            # 快照 = 改动前的样子
        self._apply_note_state(note, now)

    def _apply_note_state(self, note: Note, state: tuple[int, int, float]) -> None:
        """把音符恢复到某个 (音高, 时值, 力度) 状态。选区键跟着走，否则选区会漏气。"""
        pitch, length, velocity = state
        if int(pitch) != int(note.pitch):
            self._rekey(note, int(pitch))
            note.pitch = int(pitch)
        note.length = max(1, int(length))
        note.velocity = float(velocity)

    def mousePressEvent(self, event):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = event.position()
        x, y = pos.x(), pos.y()
        button = event.button()
        mod = event.modifiers()
        self._cursor = pos
        if self.track is None:
            return

        # Alt + 拖动 = 框选（按住 Ctrl 是"加选"）。放在最前面，因为它对
        # "从音符上起手"和"从空格起手"应该是一样的行为。
        if mod & Qt.KeyboardModifier.AltModifier:
            self._mode = "marquee"
            self._drag_start = pos
            self._marquee = QRectF(pos, pos)
            self._marquee_base = set(self.selected) if (mod & Qt.KeyboardModifier.ControlModifier) else set()
            self.update()
            return

        step = self.step_at_x(x)
        pitch = self.pitch_at_y(y)
        hit = note_covering(self.track, step, pitch)

        if button == Qt.MouseButton.RightButton:
            self._mode = "erase"
            # 有东西可擦才压撤销点：右键点在空格子上什么都没改，
            # 不该在撤销栈里占一格（用户连按撤销会看到几步"没反应"）
            if note_covering(self.track, step, pitch) is not None:
                self._begin_edit("擦除音符")
            if self.erase_note(step, pitch):
                self._edit_dirty = True
            self._last_cell = (step, pitch)
            self.update()
            return
        if button != Qt.MouseButton.LeftButton:
            return

        if hit is not None and hit.pitch == pitch:
            if mod & Qt.KeyboardModifier.ControlModifier:
                # Ctrl + 点 = 加/减选。没有这一步，批量移调就没法只用鼠标操作。
                self.toggle_selected(hit)
                self.statusHint.emit(f"已选 {len(self.selected)} 个音符")
                return
            rect = self.note_rect(hit)
            # 这三种模式**按下时都不改数据**（改数据发生在拖动中，或者松手时的
            # "点删"），所以这里不压撤销点 —— 压了就可能在撤销栈里留下一个
            # "按下去毫无反应"的格子。改成在第一次真实改动那一刻补发（见 _begin_edit）。
            if x >= rect.right() - NOTE_EDGE:
                self._mode = "length"
                self._origin_len = int(hit.length)
                self.statusHint.emit("左右拖动调整时值")
            elif mod & Qt.KeyboardModifier.ShiftModifier:
                self._mode = "velocity"
                self._origin_vel = float(hit.velocity)
                self.statusHint.emit("上下拖动调节力度")
            else:
                # 中间：先当作"可能要改音高"。松手时如果没动过，就是一次点击 → 删除。
                self._mode = "pitch"
                self._origin_pitch = int(hit.pitch)
                self.statusHint.emit("上下拖动改音高 · 只点一下是删除")
            self._drag_note = hit
            self._drag_start = pos
            self._drag_origin = (int(hit.pitch), int(hit.length), float(hit.velocity))
            self._moved = False
            self._primary = (hit.step, hit.pitch)
            self.selected = {self._primary}
            self.update()
            return

        # 空格子：左键画（并试听），右键已经在上面处理掉了
        self._mode = "paint"
        if note_covering(self.track, step, pitch) is None:
            self._begin_edit("绘制音符")        # 撤销点必须压在写入之前
            if self.draw_note(step, pitch) is not None:
                self._edit_dirty = True
        self._last_cell = (step, pitch)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        x, y = pos.x(), pos.y()
        self._cursor = pos
        self._hover = (self.step_at_x(x), self.pitch_at_y(y))

        if self._mode == "marquee":
            self._marquee = QRectF(self._drag_start, pos).normalized()
            self.update()
            return

        if not self._moved and (pos - self._drag_start).manhattanLength() > DRAG_SLOP:
            self._moved = True

        if self._mode == "pitch" and self._drag_note is not None and self._moved:
            # 按"行差"移动而不是"点击落点"，这样抓住音符的哪个位置都不会跳。
            # 目标被钳在当前网格的音域里（而不是 0..127）：拖出可视范围之外的
            # 音符会立刻"消失"，用户既看不到也拖不回来 —— 要挪得更远应该用
            # 工具栏的升降八度，那条路径会连带把音域一起重算。
            rows = int(round((self._drag_start.y() - y) / max(1.0, self.row_h)))
            target = max(self.lo_pitch, min(self.hi_pitch, self._origin_pitch + rows))
            if target != self._drag_note.pitch:
                # **已知边界（本轮刻意不动）**：这里只比对目标音高的**起始步**
                # （Track.note_at），所以把长音符拖到与另一条长音符中段重叠的位置
                # 不会被挡住。改用 note_covering 会把"目标格被长音符盖住也算占用"
                # 一并引入，拖动的落点语义跟着变（影响面比本条缺陷大得多），留待
                # 专门一轮处理；写在这里是为了让它是个"已知项"而不是没这回事。
                occupant = self.track.note_at(self._drag_note.step, target)
                if occupant is None or occupant is self._drag_note:
                    self._rekey(self._drag_note, target)
                    self._drag_note.pitch = target
                    self._edit_dirty = True
                    self.statusHint.emit(f"音高 {dsp.pitch_name(target)}")
                    self.update()
            return

        if self._mode == "velocity" and self._drag_note is not None and self._moved:
            dy = self._drag_start.y() - y
            value = max(0.05, min(1.0, self._origin_vel + dy / VEL_DRAG_SPAN))
            if abs(value - float(self._drag_note.velocity)) > 1e-9:
                self._drag_note.velocity = value
                self._edit_dirty = True
            self.statusHint.emit(f"力度 {round(self._drag_note.velocity * 100)}%")
            self.update()
            return

        if self._mode == "length" and self._drag_note is not None:
            delta = int(round((x - self._drag_start.x()) / max(1e-6, self.step_w)))
            limit = self.project.total_steps - self._drag_note.step
            value = max(1, min(max(1, limit), self._origin_len + delta))
            if value != int(self._drag_note.length):
                self._drag_note.length = value
                self._edit_dirty = True
            self.statusHint.emit(f"时值 {self._drag_note.length} 步")
            self.update()
            return

        if self._mode in ("paint", "erase"):
            step = self.step_at_x(x)
            pitch = self.pitch_at_y(y)
            cell = (step, pitch)
            if cell != self._last_cell:
                if self._mode == "erase":
                    # 右键是按在空格子上开始的（当时没压点），拖到音符上才第一次
                    # 真的改动 —— 撤销点在这里补，且只补一次
                    if note_covering(self.track, step, pitch) is not None:
                        self._begin_edit("擦除音符")
                    if self.erase_note(step, pitch):
                        self._edit_dirty = True
                elif note_covering(self.track, step, pitch) is None:
                    # 拖过已有音符时**不删也不覆盖** —— 一笔划过不该毁掉已写好的东西
                    self._begin_edit("绘制音符")
                    if self.draw_note(step, pitch) is not None:
                        self._edit_dirty = True
                self._last_cell = cell
        self.update()

    def mouseReleaseEvent(self, event):
        if self._mode == "marquee" and self._marquee is not None:
            self._apply_marquee(self._marquee)
        elif self._mode == "pitch" and self._drag_note is not None and not self._moved:
            # 点一下已有音符 = 删除（钢琴卷帘里最顺手的"划掉重画"）
            note = self._drag_note
            if note_covering(self.track, note.step, note.pitch) is note:
                pitch = int(note.pitch)
                self._begin_edit("删除音符")    # 撤销点必须压在删除之前
                self.erase_note(note.step, pitch)
                self._edit_dirty = True
                self.prune_selection()
                self.statusHint.emit(f"已删除 {dsp.pitch_name(pitch)}")
        else:
            # 改音高/时值/力度：整笔拖动到这里才提交（净变化为零就什么都不做）
            self._commit_drag()

        if self._mode in ("paint", "erase", "length", "velocity", "pitch"):
            self.prune_selection()
        # 发不发 notesChanged 只看数据真的动过没有 —— 原来按模式枚举，
        # "拖中间改音高"这一路（_moved 为真）落到最后是 changed = False，
        # 于是音高改了却谁也不通知，编排者不会重渲染音频。
        changed = self._edit_dirty
        self._cancel_drag()
        self.update()
        if changed:
            self.notesChanged.emit()

    def leaveEvent(self, event):
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def _apply_marquee(self, rect: QRectF) -> None:
        if self.track is None:
            return
        s0, s1 = self.step_at_x(rect.left()), self.step_at_x(rect.right())
        if s0 > s1:
            s0, s1 = s1, s0
        p_hi, p_lo = self.pitch_at_y(rect.top()), self.pitch_at_y(rect.bottom())
        if p_lo > p_hi:
            p_lo, p_hi = p_hi, p_lo
        keys = set(self._marquee_base)
        for n in self.track.notes:
            if s0 <= n.step <= s1 and p_lo <= n.pitch <= p_hi:
                keys.add((n.step, n.pitch))
        self.selected = keys
        self._primary = sorted(keys)[0] if keys else None
        self.statusHint.emit(f"选中 {len(keys)} 个音符（Delete 删除 · Shift+↑/↓ 升降八度）")
        self.update()

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), GRID_BG)

        self._paint_rows(painter)
        self._paint_columns(painter)
        self._paint_notes(painter)
        self._paint_marquee(painter)
        self._paint_hover(painter)
        self._paint_playhead(painter)
        self._paint_readout(painter)
        painter.end()

    def _paint_rows(self, painter: QPainter) -> None:
        """行底色：黑键行压暗一档。

        没有这一档，上下相邻的 C 和 C# 就是两条一模一样的横线，
        写和弦时会把音放到错半音上 —— 这是钢琴卷帘最要命的可读性问题。
        另外每个 C 的上沿给一道稍亮的线，八度位置一眼可数。
        """
        w = self.width()
        for p in range(self.lo_pitch, self.hi_pitch + 1):
            y = self.y_for_pitch(p)
            row = QRectF(0.0, y, w, self.row_h)
            if is_black_key(p):
                painter.fillRect(row, QColor(0, 0, 0, 46))
            else:
                painter.fillRect(row, QColor(255, 255, 255, 8))
            painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
            painter.drawLine(0, int(y), int(w), int(y))
            if p % 12 == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
                painter.drawLine(0, int(y), int(w), int(y))

    def _paint_columns(self, painter: QPainter) -> None:
        """步 / 拍 / 小节三种明度的竖线（明度依次递增）。"""
        h = self.height()
        spb = self.project.steps_per_bar
        spbeat = self.project.steps_per_beat
        for s in range(self.project.total_steps + 1):
            if s % spb == 0:
                pen = QPen(LINE_BAR, 1)
            elif s % spbeat == 0:
                pen = QPen(LINE_BEAT, 1)
            else:
                pen = QPen(LINE_STEP, 1)
            painter.setPen(pen)
            x = int(self.x_for_step(s))
            painter.drawLine(x, 0, x, int(h))

    def _paint_notes(self, painter: QPainter) -> None:
        if self.track is None:
            return
        color = QColor(self.track.color)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for note in self.track.notes:
            r = self.note_rect(note)
            if r.right() < 0 or r.left() > self.width():
                continue
            if r.bottom() < 0 or r.top() > self.height():
                continue
            vel = max(0.0, min(1.0, float(note.velocity)))
            c = QColor(color)
            # 颜色深浅 = 力度：这是卷帘里唯一能"一眼看出轻重"的编码方式
            c.setAlphaF(0.30 + 0.70 * vel)
            if (note.step, note.pitch) in self.selected:
                c = c.lighter(118)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            rad = min(2.6, max(1.0, r.height() / 2.6))
            painter.drawRoundedRect(r, rad, rad)
            # 亮顶边把力度差异再拉开一档（不改变色相的前提下）
            if r.height() > 3.0:
                hl = QColor(255, 255, 255, int(24 + 96 * vel))
                painter.setBrush(hl)
                painter.drawRect(QRectF(r.x() + 0.6, r.y() + 0.6, max(0.0, r.width() - 1.2), 1.0))
            if (note.step, note.pitch) in self.selected:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(255, 255, 255, 210), 1.2))
                painter.drawRoundedRect(r, rad, rad)

    def _paint_hover(self, painter: QPainter) -> None:
        """光标所在格的高亮：**无条件**跟着光标走，不区分空格子还是有音符。

        这里原来用 ``Track.note_at`` 判断"这一格是不是空的"，结果两头不讨好：
        长音符中段（真的落不了笔）闪出一格高亮、看着像个空格子，它的起始格反倒
        什么都没有 —— 同一件事两套判定。现在索性去掉判定。

        为什么不改成"能不能落笔"（``note_covering``）：悬停高亮的语义是"光标在
        哪一格"，不是"这一格能不能画"。音序器页的 ``StepGrid._paint_hover`` 就是
        无条件画的（sequencer.py:890 起），同一个软件里两个网格用两套悬停语义，
        用户只会觉得其中一个坏了。
        """
        if self._hover is None:
            return
        step, pitch = self._hover
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 20 if not is_black_key(pitch) else 30))
        painter.drawRect(QRectF(self.x_for_step(step), self.y_for_pitch(pitch),
                                self.step_w, self.row_h))

    def _paint_marquee(self, painter: QPainter) -> None:
        if self._marquee is None:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(120, 190, 255, 38))
        painter.setPen(QPen(QColor(170, 215, 255, 190), 1.0, Qt.PenStyle.DashLine))
        painter.drawRect(self._marquee)

    def _paint_playhead(self, painter: QPainter) -> None:
        px = self.x_for_step(self.playhead_step)
        if px < 0 or px > self.width():
            return
        g = QLinearGradient(px, 0, px, self.height())
        g.setColorAt(0.0, theme.white(0.95))
        g.setColorAt(1.0, theme.white(0.30))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(g, 1.6))
        painter.drawLine(int(px), 0, int(px), self.height())

    # -- 读数（跟随光标的即时反馈）---------------------------------------

    def readout(self) -> str:
        if self._hover is None or self.track is None:
            return ""
        step, pitch = self._hover
        text = f"{dsp.pitch_name(pitch)} · 步 {step + 1}"
        note = note_covering(self.track, step, pitch)
        if note is not None:
            text += f" · 力度 {round(note.velocity * 100)}% · 时值 {note.length}"
        return text

    def _paint_readout(self, painter: QPainter) -> None:
        """跟随光标的读数。

        和音序器同一个理由：**不用 tooltip**。tooltip 要悬停半秒才弹、还会挡住
        视线，而"这一格是什么音高 / 这个音符多重"必须在移动鼠标的过程中一直看得见。
        """
        text = self.readout()
        if not text:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = type_scale.caption1.font()
        painter.setFont(f)
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text) + 16.0
        h = 20.0
        x = min(self._cursor.x() + 14.0, self.width() - w - 4.0)
        y = self._cursor.y() - h - 8.0
        if y < 2.0:
            y = self._cursor.y() + 14.0
        r = QRectF(max(2.0, x), y, w, h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(16, 17, 23, 235))
        painter.drawRoundedRect(r, 5.0, 5.0)
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(r, 5.0, 5.0)
        painter.setPen(QPen(labels.label))
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


# ==========================================================================
# 底部：力度条
# ==========================================================================


class VelocityLane(QWidget):
    """力度条。

    力度如果只能靠 Shift+上下拖一个个盲调，那它就等于不存在。
    把它画成一排柱子之后，"这一段整体偏轻"这种判断才做得出来，
    而那正是编曲时最需要看见的东西。

    拖动目标的选择顺序：选中音符 → 光标下那根柱子。
    """

    notesChanged = Signal()
    editStarted = Signal(str)
    statusHint = Signal(str)

    def __init__(self, grid: RollGrid, parent: QWidget | None = None):
        super().__init__(parent)
        self.grid = grid
        self.setFixedHeight(VEL_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self._offset = 0.0
        self._drag: Note | None = None
        #: 这一次拖动是否已经压过撤销点 —— 压过就不再压，一整笔只占一步
        self._dirty = False

    def set_offset(self, offset: float) -> None:
        self._offset = float(offset)
        self.update()

    # -- 命中 ------------------------------------------------------------

    def _bars(self) -> list[tuple[Note, float, float]]:
        """返回 [(音符, 屏幕 x, 宽度)]，只含落在视口里的。"""
        if self.grid.track is None:
            return []
        out = []
        for n in self.grid.track.sorted_notes():
            x = self.grid.x_for_step(n.step) - self._offset
            w = max(4.0, max(1, n.length) * self.grid.step_w - 2.0)
            if x + w < 0 or x > self.width():
                continue
            out.append((n, x, w))
        return out

    def note_under(self, x: float) -> Note | None:
        """光标下那根柱子；落在缝里时取最近的一根（不用对准也能拖）。"""
        best: Note | None = None
        best_d = 12.0
        for n, bx, bw in self._bars():
            if bx - 4.0 <= x <= bx + bw + 4.0:
                return n
            d = min(abs(x - bx), abs(x - (bx + bw)))
            if d < best_d:
                best, best_d = n, d
        return best

    def target_note(self) -> Note | None:
        note = self.grid.primary_note()
        return note

    # -- 交互 ------------------------------------------------------------

    def _velocity_at(self, y: float) -> float:
        span = max(1.0, self.height() - 6.0)
        return max(0.05, min(1.0, 1.0 - (y - 3.0) / span))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        note = self.note_under(pos.x())
        if note is None:
            self.statusHint.emit("力度条上没有音符（先画一个）")
            return
        # 点哪根柱子就把哪根变成"当前音符" —— 不然用户不知道自己正在改哪一个
        self.grid.set_primary(note)
        self._drag = note
        self._dirty = False
        self._set_velocity(note, self._velocity_at(pos.y()))

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        self._set_velocity(self._drag, self._velocity_at(event.position().y()))

    def _set_velocity(self, note: Note, value: float) -> bool:
        """写力度，并且**改动一发生就通知**。

        原来 ``notesChanged`` 只在 mouseRelease 里发：拖动力度时鼠标离开控件、
        或者窗口失焦，release 就永远不来 —— 力度已经写进工程了却谁也不通知，
        编排者不会重渲染，听到的还是旧力度，「边调边听」直接断掉。
        通知跟着数据走，而不是跟着松手走。
        """
        value = max(0.05, min(1.0, float(value)))
        if abs(value - float(note.velocity)) < 1e-9:
            return False                    # 没变就不通知，免得一路空转重渲染
        if not self._dirty:
            self._dirty = True
            self.editStarted.emit("调整力度")   # 撤销点必须压在改动之前
        note.velocity = value
        self.statusHint.emit(f"{dsp.pitch_name(note.pitch)} 力度 {round(value * 100)}%")
        self.grid.update()
        self.update()
        self.notesChanged.emit()
        return True

    def mouseReleaseEvent(self, event):
        # 通知在改动那一刻已经发过了（见 _set_velocity），这里只收尾
        self._drag = None
        self._dirty = False

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), KEY_BG)

        target = self.target_note()
        track_color = QColor(self.grid.track.color) if self.grid.track is not None else accents.blue
        top, bottom = 6.0, self.height() - 8.0

        # 参考线：50% / 100% —— 没有基准线，柱子高低就只是个装饰
        painter.setPen(QPen(QColor(255, 255, 255, 16), 1))
        for frac in (0.5, 1.0):
            y = bottom - (bottom - top) * frac
            painter.drawLine(0, int(y), self.width(), int(y))

        if self.grid.track is None:
            painter.setFont(type_scale.caption1.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "没有可编辑的旋律轨")
            painter.end()
            return

        for note, x, w in self._bars():
            v = max(0.0, min(1.0, float(note.velocity)))
            h = max(2.0, (bottom - top) * v)
            r = QRectF(x, bottom - h, max(3.0, w), h)
            c = QColor(track_color)
            c.setAlphaF(0.35 + 0.55 * v)
            if note is target:
                c = QColor(track_color).lighter(135)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawRoundedRect(r, 1.6, 1.6)
            if note is target:
                painter.setBrush(theme.white(0.95))
                painter.drawRect(QRectF(r.x(), r.y() - 0.5, r.width(), 1.6))

        # 播放头延续到这里：三条视图的播放头在同一根竖线上，眼睛才不用来回找
        px = self.grid.x_for_step(self.grid.playhead_step) - self._offset
        if 0 <= px <= self.width():
            painter.setPen(QPen(theme.white(0.55), 1.2))
            painter.drawLine(int(px), 0, int(px), self.height())

        if target is not None:
            text = (f"{dsp.pitch_name(target.pitch)} · 力度 {round(target.velocity * 100)}%"
                    f" · 时值 {target.length} 步")
            painter.setFont(type_scale.caption2.font())
            fm = QFontMetricsF(painter.font())
            w = fm.horizontalAdvance(text) + 14
            box = QRectF(6.0, 2.0, w, 16.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(16, 17, 23, 225))
            painter.drawRoundedRect(box, 4.0, 4.0)
            painter.setPen(QPen(labels.secondary))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

        painter.setPen(QPen(QColor(255, 255, 255, 22), 1))
        painter.drawLine(0, 0, self.width(), 0)
        painter.end()


# ==========================================================================
# 工具条上的横向芯片条
# ==========================================================================


class _ChipStrip(QScrollArea):
    """横向芯片条。

    刻意**不显示滚动条**：工具条只有 42px 高，一条 9px 的滚动条会把芯片
    挤变形。轨道多到放不下时用滚轮横滚（也可以按 `[` / `]` 换轨）。
    """

    def wheelEvent(self, event):
        bar = self.horizontalScrollBar()
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar.setValue(bar.value() - delta)
        event.accept()


# ==========================================================================
# 页面
# ==========================================================================


class PianoRollPage(QWidget):
    """钢琴卷帘页面：轨道选择条 + 键盘/卷帘/力度条 + 提示条。"""

    #: 契约规定的名字：用户改了工程内容，需要重渲染
    edited = Signal()
    #: 兼容音序器页的命名（编排者若照抄那边的接线也不会断）
    notesEdited = Signal()
    #: 请求把播放头移到某个时间（秒）
    seekRequested = Signal(float)
    #: 试听了一个音：(音高, 缓冲峰值)。峰值暴露给外部是为了让"真的发声了"可验证。
    auditioned = Signal(int, float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self._controller = None
        #: ``_commit`` 刚刚压下去的撤销标签：这一笔最后没改动时按标签抹回去用
        self._last_undo_label = ""
        self._track_id: str | None = None
        self._chips: dict[str, GlassChip] = {}
        self._chip_order: list[str] = []
        self.last_audition: tuple[int, float] | None = None

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)

        # ---- 工具条 -------------------------------------------------------
        bar = GlassPanel(self, material=theme.black(0.20), radius=radii.control)
        bar.setFixedHeight(42)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(5)
        self._bar = bar

        self.track_strip = _ChipStrip(bar)
        self.track_strip.setFixedHeight(30)
        self.track_strip.setWidgetResizable(True)
        self.track_strip.setFrameShape(QScrollArea.Shape.NoFrame)
        self.track_strip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.track_strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.track_strip.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.track_strip.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.track_strip.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self._chip_host = QWidget()
        self._chip_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._chip_row = QHBoxLayout(self._chip_host)
        self._chip_row.setContentsMargins(0, 0, 0, 0)
        self._chip_row.setSpacing(4)
        self._chip_row.addStretch(1)
        self.track_strip.setWidget(self._chip_host)
        self.track_strip.viewport().setStyleSheet("background: transparent;")
        bl.addWidget(self.track_strip, 1)

        def mk(icon, tip, slot, size=28, isize=16):
            b = GlassImageButton(icon, bar, size=size, icon_size=isize, tooltip=tip)
            b.clicked.connect(slot)
            bl.addWidget(b)
            return b

        bl.addSpacing(6)
        self.btn_quantize = mk("target", "量化到 1/16（作用于选中，无选中则全部）", self._quantize)
        self.btn_select_all = mk("check", "全部选中 (Ctrl+A)", self._select_all)
        self.btn_delete = mk("delete", "删除选中的音符 (Delete)", self._delete_selected)
        bl.addSpacing(6)
        self.btn_oct_up = mk("arrow_up", "整体升八度 (Shift+↑)", lambda: self._transpose(12))
        self.btn_oct_down = mk("arrow_down", "整体降八度 (Shift+↓)", lambda: self._transpose(-12))
        bl.addSpacing(6)
        self.btn_zoom_out = mk("search", "缩小网格 (Ctrl+滚轮)", lambda: self._zoom(1 / 1.2), isize=15)
        self.btn_zoom_in = mk("zoom_in", "放大网格 (Ctrl+滚轮)", lambda: self._zoom(1.2), isize=17)
        self.btn_fit = mk("ruler", "铺满宽度并恢复默认行高", self._fit_width)
        bl.addSpacing(6)
        self.btn_undo = mk("undo", "撤销 (Ctrl+Z)", self._undo)
        self.btn_redo = mk("redo", "重做 (Ctrl+Y)", self._redo)

        # ---- 主体 ---------------------------------------------------------
        self._body = QWidget(self)
        self._body.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        body_l = QVBoxLayout(self._body)
        body_l.setContentsMargins(0, 0, 0, 0)
        body_l.setSpacing(spacing.xs)

        self.grid = RollGrid(project, self._body)
        self.keyboard = PianoKeyboard(self._body)
        self.ruler = StepRuler(self._body)
        self.velocity = VelocityLane(self.grid, self._body)

        center = QHBoxLayout()
        center.setSpacing(0)
        left_col = QVBoxLayout()
        left_col.setSpacing(0)
        left_col.addSpacing(RULER_H)
        left_col.addWidget(self.keyboard, 1)

        self.scroll = QScrollArea(self._body)
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
        # 视口必须单独清底：QScrollArea 的 viewport 用 QPalette::Base 上色（默认是白），
        # 只给 QScrollArea 设 transparent 管不到它。内容是短于视口时（音域窄、行高低）
        # 下面会露出一大块白 —— 这在深色软件里极其刺眼。
        self.scroll.viewport().setStyleSheet("background: transparent;")
        right_col = QVBoxLayout()
        right_col.setSpacing(0)
        right_col.addWidget(self.ruler)
        right_col.addWidget(self.scroll, 1)

        center.addLayout(left_col)
        center.addLayout(right_col, 1)
        body_l.addLayout(center, 1)

        # 力度条整条压在键盘下方，只把左侧让出一个键盘宽度，于是它的 x
        # 与网格列**天然对齐**（两者都用同一个滚动偏移）——不用任何手工校准。
        vel_row = QHBoxLayout()
        vel_row.setSpacing(0)
        vel_row.addSpacing(KEY_W)
        vel_row.addWidget(self.velocity, 1)
        body_l.addLayout(vel_row)

        # ---- 空状态 -------------------------------------------------------
        self._empty_host = QWidget(self)
        self._empty_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        eh = QHBoxLayout(self._empty_host)
        eh.addStretch(1)
        self._empty = GlassCard(self._empty_host, radius=radii.card)
        self._empty.setFixedSize(420, 190)
        ev = QVBoxLayout(self._empty)
        ev.setContentsMargins(spacing.lg, spacing.lg, spacing.lg, spacing.lg)
        ev.setSpacing(spacing.xs)
        ev.addStretch(1)
        from PySide6.QtWidgets import QLabel

        icon = QLabel(self._empty)
        icon.setPixmap(icons.pixmap("piano", 56, dpr=self.devicePixelRatioF()))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(icon)
        title = make_label("这里还没有旋律轨", type_scale.title3, labels.secondary, self._empty)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(title)
        sub = make_label("去「总览」页点「＋ 合成器轨道」，回来就能在卷帘里写旋律了。",
                         type_scale.caption1, labels.quaternary, self._empty)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        ev.addWidget(sub)
        ev.addStretch(1)
        eh.addWidget(self._empty)
        eh.addStretch(1)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._body)          # 0
        self._stack.addWidget(self._empty_host)    # 1
        # 顺序不能乱：工具条必须先入布局，否则它会以默认几何尺寸浮在
        # 页面左上角、压住小节尺 —— 而 grab() 照样"成功"，只有真的看图才发现。
        root.addWidget(bar)
        root.addWidget(self._stack, 1)

        # ---- 提示条 -------------------------------------------------------
        hint = GlassCard(self, radius=radii.control)
        hint.setFixedHeight(30)
        hl = QHBoxLayout(hint)
        hl.setContentsMargins(12, 0, 12, 0)
        self._hint = QLabel("左键画 / 点音符删除 · 拖中间改音高 · 拖右缘改时值 · "
                            "Shift+上下拖改力度 · 右键擦除 · Ctrl+点选 · Alt+拖框选", hint)
        self._hint.setFont(type_scale.caption1.font())
        c = labels.tertiary
        self._hint.setStyleSheet(f"color: rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f});"
                                 "background: transparent;")
        hl.addWidget(self._hint)
        hl.addStretch(1)
        self._count = QLabel("", hint)
        self._count.setFont(type_scale.caption1.font())
        c2 = labels.secondary
        self._count.setStyleSheet(f"color: rgba({c2.red()},{c2.green()},{c2.blue()},{c2.alphaF():.3f});"
                                  "background: transparent;")
        hl.addWidget(self._count)
        root.addWidget(hint)

        # ---- 接线 ---------------------------------------------------------
        self.grid.notesChanged.connect(self._on_notes_changed)
        self.grid.seekRequested.connect(self.seekRequested)
        self.grid.statusHint.connect(self._set_hint)
        self.grid.editStarted.connect(self._commit)
        self.grid.editAborted.connect(self._drop_undo)
        self.grid.auditioned.connect(self._on_auditioned)
        self.velocity.notesChanged.connect(self._on_notes_changed)
        self.velocity.editStarted.connect(self._commit)
        self.velocity.statusHint.connect(self._set_hint)
        self.keyboard.notePressed.connect(self._audition_pitch)
        self.ruler.seekRequested.connect(self._seek_to_step)
        self.scroll.horizontalScrollBar().valueChanged.connect(self._on_hscroll)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_vscroll)

        self.rebuild()

    # -- 契约接口 --------------------------------------------------------

    def bind(self, controller) -> None:
        """接上 AppController。只用到 push_undo / undo / redo —— 别的都不需要。

        ``undoStateChanged`` 用 hasattr 兜底：编排者也可能传一个只提供
        push_undo 的轻量对象（测试里就是这样），没必要为此拒接。
        """
        self._controller = controller
        sig = getattr(controller, "undoStateChanged", None)
        if sig is not None:
            try:
                sig.connect(self._on_undo_state)
            except Exception:
                pass
        self._on_undo_state(getattr(controller, "can_undo", False),
                            getattr(controller, "can_redo", False))

    def set_project(self, project: Project) -> None:
        """整个工程被换掉（打开新工程 / 撤销）。"""
        self.project = project
        self._track_id = None
        self.grid.set_project(project)
        self.rebuild()

    def rebuild(self) -> None:
        """工程**结构**变了（增删轨道、改轨道类型）。"""
        tracks = synth_tracks_of(self.project)
        self._sync_chips(tracks)
        track = self._resolve_track()
        if track is None:
            # 没有旋律轨：切到空状态，并把网格置空 —— 但**不能**让它崩，
            # 编排者可能在任何时刻调用这个页面。
            self._stack.setCurrentIndex(1)
            self.grid.set_track(None, DEFAULT_LO, DEFAULT_HI)
            self.grid.set_project(self.project)
        else:
            self._stack.setCurrentIndex(0)
            self._track_id = track.id
            self.grid.set_project(self.project)
            self.grid.set_track(track, *pitch_span(track))
        self._sync_chips_checked()
        self._update_count()
        self._refresh_views()

    def refresh_notes(self) -> None:
        """只是音符/参数变了，重画即可。

        这里**必须按 id 重新解析轨道**：撤销是把工程的 ``__dict__`` 就地换成
        反序列化出来的新对象，我之前抓住的 Track 引用会指向一个已经不在工程里的
        孤儿 —— 继续在它上面画，界面看起来在动，渲染出来的却是旧数据。
        """
        if self.project is not self.grid.project:
            self.grid.set_project(self.project)
        track = self._resolve_track()
        if track is not self.grid.track:
            self.rebuild()
            return
        if track is not None:
            notes = track.notes
            if notes:
                lo, hi = pitch_span(track)
                # 只扩不缩：编辑过程中若音域缩了，行高映射会突然改变，
                # 正在拖的音符会跳走。缩窄交给换轨/重建去做。
                if lo < self.grid.lo_pitch or hi > self.grid.hi_pitch:
                    self.grid.set_pitch_range(min(lo, self.grid.lo_pitch),
                                              max(hi, self.grid.hi_pitch))
        self.grid.prune_selection()
        self._update_count()
        self._refresh_views()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        """播放位置更新；``playing`` 为真时做自动跟随。"""
        self.grid.set_position(seconds, playing)
        self.ruler.set_playhead(self.grid.playhead_step)
        self.velocity.update()
        if playing and self.grid.follow:
            self._follow(seconds)

    # -- 轨道解析 --------------------------------------------------------

    def _resolve_track(self) -> Track | None:
        tracks = synth_tracks_of(self.project)
        if not tracks:
            return None
        if self._track_id:
            t = self.project.track_by_id(self._track_id)
            if t is not None and t.kind == "synth":
                return t
        self._track_id = tracks[0].id
        return tracks[0]

    def current_track(self) -> Track | None:
        return self.grid.track

    def is_empty_state(self) -> bool:
        return self._stack.currentIndex() == 1

    def select_track(self, track: Track | None) -> None:
        """切换要编辑的轨道；音域按新轨道的音符重算。

        只接受 ``kind == "synth"`` 的轨道，和本模块另外四条路径
        （``_resolve_track`` / ``rebuild`` / ``cycle_track`` / 芯片条）保持一致。
        这是一个 public 入口 —— 编排者很容易顺手把"列表里当前选中的轨道"传进来，
        而那可能是鼓轨。不挡的话网格会切到鼓轨上，接下来画的音符就**写进了鼓轨的
        notes**（鼓机页不认这种数据，用户也看不出来它去哪了），要等下一次 refresh
        才自愈，脏音符却留在鼓轨里了。
        """
        if track is None or track.kind != "synth":
            if track is not None:
                self._set_hint(f"「{track.name}」是鼓轨，请用鼓机页编辑")
            return
        self._track_id = track.id
        self.grid.set_project(self.project)
        self.grid.set_track(track, *pitch_span(track))
        self._sync_chips_checked()
        self._update_count()
        self.ruler.set_view(self.grid.step_w, self.scroll.horizontalScrollBar().value(),
                            self.project.total_steps, self.project.steps_per_bar,
                            self.project.steps_per_beat)
        self._refresh_views()

    def cycle_track(self, delta: int) -> None:
        tracks = synth_tracks_of(self.project)
        if len(tracks) < 2:
            return
        cur = self.grid.track
        idx = tracks.index(cur) if cur in tracks else 0
        self.select_track(tracks[(idx + delta) % len(tracks)])

    # -- 芯片条 ----------------------------------------------------------

    def _sync_chips(self, tracks: list[Track]) -> None:
        ids = [t.id for t in tracks]
        if ids == self._chip_order:
            return
        for chip in self._chips.values():
            self._chip_row.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()
        self._chip_order = ids
        for i, track in enumerate(tracks):
            chip = GlassChip(track.name, self._chip_host,
                             accent=QColor(track.color), checkable=True)
            chip.setToolTip(f"{track.name} · {len(track.notes)} 个音符")
            chip.clicked.connect(lambda t=track: self.select_track(t))
            self._chip_row.insertWidget(i, chip)
            self._chips[track.id] = chip

    def _sync_chips_checked(self) -> None:
        for tid, chip in self._chips.items():
            chip.setChecked(tid == self._track_id)

    # -- 视图同步 --------------------------------------------------------

    def _refresh_views(self) -> None:
        """把所有兄弟视图的视图参数对齐到网格。

        三块视图（尺子/网格/力度条）共用同一个横向偏移 = 滚动条的值，
        所以不存在"三个偏移量各算各的、慢慢就对不上"的问题。
        """
        hbar = self.scroll.horizontalScrollBar()
        vbar = self.scroll.verticalScrollBar()
        self.ruler.set_view(self.grid.step_w, hbar.value(), self.project.total_steps,
                            self.project.steps_per_bar, self.project.steps_per_beat)
        self.ruler.set_playhead(self.grid.playhead_step)
        self.velocity.set_offset(hbar.value())
        inset = max(0, self.scroll.height() - self.scroll.viewport().height())
        self.keyboard.set_view(self.grid.lo_pitch, self.grid.hi_pitch, self.grid.row_h,
                               vbar.value(), inset)
        self.grid.update()
        self.velocity.update()

    def _on_hscroll(self, value: float) -> None:
        self.ruler.set_view(self.grid.step_w, value, self.project.total_steps,
                            self.project.steps_per_bar, self.project.steps_per_beat)
        self.velocity.set_offset(value)

    def _on_vscroll(self, value: float) -> None:
        inset = max(0, self.scroll.height() - self.scroll.viewport().height())
        self.keyboard.set_view(self.grid.lo_pitch, self.grid.hi_pitch, self.grid.row_h,
                               value, inset)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_views()

    # -- 与控制器对接 ----------------------------------------------------

    def _commit(self, label: str) -> None:
        """在改动**之前**压撤销点。

        网格/力度条在"这一笔真的要改数据"的那一刻发一次 ``editStarted``
        （不是按下鼠标那一刻，见 ``RollGrid.editStarted``），一整笔拖动因此
        只占一步撤销，而净变化为零的手势一步都不占。

        标签顺手记下来：万一这一笔最后什么都没改（``editAborted``），页面要
        带标签把它抹掉，见 :meth:`_drop_undo`。
        """
        self._last_undo_label = str(label)
        ctrl = getattr(self, "_controller", None)
        if ctrl is not None:
            try:
                ctrl.push_undo(label)
            except Exception:
                pass

    def _drop_undo(self) -> None:
        """抹掉**刚刚**压下的那个撤销点（这一笔最后什么都没改）。

        ``pop_undo`` 必须带标签调用：万一栈顶不是这一笔（中间被别的操作插了一手），
        宁可留一个空条目，也绝不能删掉别人的有效撤销点 —— 那是真的会让用户丢数据的。
        标签就是 ``_commit`` 刚压下去的那个，所以一定匹配。
        """
        label, self._last_undo_label = self._last_undo_label, ""
        ctrl = getattr(self, "_controller", None)
        pop = getattr(ctrl, "pop_undo", None)
        if pop is None or not label:
            # 轻量控制器（只提供 push_undo 的那种）没有 pop_undo：不抹也没关系，
            # 最坏就是一个空条目，不会误删
            return
        try:
            pop(label)
        except Exception:
            pass

    def _undo(self) -> None:
        ctrl = getattr(self, "_controller", None)
        if ctrl is None:
            self._set_hint("没有接上控制器，撤销不可用")
            return
        ctrl.undo()
        # 撤销会整份重建工程对象，必须重建而不是 refresh
        self.rebuild()

    def _redo(self) -> None:
        ctrl = getattr(self, "_controller", None)
        if ctrl is None:
            return
        ctrl.redo()
        self.rebuild()

    def _on_undo_state(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_undo.set_dimmed(not can_undo)
        self.btn_redo.set_dimmed(not can_redo)

    # -- 工具条动作 ------------------------------------------------------

    def _select_all(self) -> None:
        n = self.grid.select_all()
        self._set_hint(f"已选中 {n} 个音符" if n else "这条轨道上还没有音符")
        self.velocity.update()

    def _delete_selected(self) -> None:
        if not self.grid.selected:
            self._set_hint("没有选中的音符")
            return
        self._commit("删除音符")
        if self.grid.delete_selected():
            self._on_notes_changed()

    def _transpose(self, delta: int) -> None:
        label = "升八度" if delta > 0 else "降八度"
        self._commit(label)
        if self.grid.transpose(delta):
            self._on_notes_changed()
        else:
            self._drop_undo()      # 一个音符都没动（全被占位挡住）→ 别留空撤销点

    def _quantize(self) -> None:
        self._commit("量化到 1/16")
        if self.grid.quantize(16):
            self._on_notes_changed()
        else:
            # 已经在网格上时量化是恒等变换：什么都不改就不该占一步撤销
            self._drop_undo()

    def _zoom(self, factor: float) -> None:
        self.grid.set_step_width(self.grid.step_w * factor)
        self._refresh_views()

    def _fit_width(self) -> None:
        vw = max(200, self.scroll.viewport().width())
        self.grid.set_row_height(ROW_H)
        self.grid.set_step_width(vw / max(1, self.project.total_steps))
        self.scroll.horizontalScrollBar().setValue(0)
        self._refresh_views()

    # -- 试听 ------------------------------------------------------------

    def _audition_pitch(self, pitch: int) -> None:
        """点键盘试听。**只发声、不动播放头**。

        这是刻意的：试音是为了确认音高，如果顺手把播放头挪走，
        正在播放的曲子就被打断了 —— 只读动作不该有副作用。
        """
        track = self.grid.track
        peak = play_preview(track, pitch, 0.85)
        self.last_audition = (int(pitch), peak)
        self.auditioned.emit(int(pitch), peak)
        self._set_hint(f"试听 {dsp.pitch_name(pitch)}（播放头未移动）")

    def _on_auditioned(self, pitch: int, peak: float) -> None:
        self.last_audition = (int(pitch), float(peak))
        self.auditioned.emit(int(pitch), float(peak))

    # -- 其它 ------------------------------------------------------------

    def _seek_to_step(self, step: int) -> None:
        self.seekRequested.emit(self.project.step_time(max(0, int(step))))

    def _set_hint(self, text: str) -> None:
        self._hint.setText(text)

    def _on_notes_changed(self) -> None:
        self._update_count()
        self.grid.update()
        self.velocity.update()
        self.edited.emit()
        self.notesEdited.emit()

    def _update_count(self) -> None:
        track = self.grid.track
        if track is None:
            self._count.setText("无旋律轨")
            return
        self._count.setText(
            f"{track.name} · {len(track.notes)} 个音符 · "
            f"{dsp.pitch_name(self.grid.lo_pitch)}–{dsp.pitch_name(self.grid.hi_pitch)}"
        )

    def _follow(self, seconds: float) -> None:
        """让播放头留在视口内（横向 + 纵向）。

        横向留出 15% 的余量再滚：卡在边缘才滚会让画面持续抽动。
        纵向只在"当前发声的音高跑到视野外"时才滚 —— 旋律上下跳的时候
        视图跟着乱窜比不跟随更烦人。
        """
        spt = self.project.seconds_per_step
        if spt <= 0:
            return
        hbar = self.scroll.horizontalScrollBar()
        x = self.grid.x_for_step(seconds / spt)
        width = self.scroll.viewport().width()
        margin = max(60.0, width * 0.15)
        if x < hbar.value() + margin or x > hbar.value() + width - margin:
            hbar.setValue(max(0, int(x - width * 0.35)))

        track = self.grid.track
        if track is None or not track.notes:
            return
        step = seconds / spt
        sounding = [n for n in track.notes if n.step <= step < n.step + max(1, n.length)]
        if not sounding:
            return
        pitch = min(sounding, key=lambda n: abs(n.step - step)).pitch
        vbar = self.scroll.verticalScrollBar()
        view_h = max(60, self.scroll.viewport().height())
        top = self.grid.y_for_pitch(pitch)
        if top < vbar.value() or top > vbar.value() + view_h - self.grid.row_h:
            vbar.setValue(max(0, int(top - view_h * 0.4)))

    def keyPressEvent(self, event):
        """页面级快捷键：换轨（`[` / `]`）和撤销重做。"""
        mod = event.modifiers()
        if mod & Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Z:
            self._undo()
        elif mod & Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Y:
            self._redo()
        elif event.key() == Qt.Key.Key_BracketLeft:
            self.cycle_track(-1)
        elif event.key() == Qt.Key.Key_BracketRight:
            self.cycle_track(1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()


__all__ = [
    "PianoRollPage", "RollGrid", "PianoKeyboard", "StepRuler", "VelocityLane",
    "is_black_key", "pitch_span", "note_covering", "quantize_unit", "snap_to_unit",
    "render_preview", "play_preview", "synth_tracks_of",
]
