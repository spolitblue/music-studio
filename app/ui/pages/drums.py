"""鼓机 —— 单条鼓轨的步进网格 + 逐件音色捏造。

和音序器的分工（为什么要有两个页面）
------------------------------------
音序器是"全曲一览、快速搭骨架"：所有轨道铺在一屏里，鼓轨被压成 12 条细行，
每行只有 21px 高 —— 看得见结构，但**捏不了音色**，也没有地方放参数。

鼓机反过来：**只盯一条鼓轨**，但把横向空间全给它（每步 16px，一小节 16 步 = 256px，
一眼能数清第几拍第几个十六分），并且把"这件鼓长什么样"的六个参数
（level / tune / decay / tone / snap / pan）摊在下方面板上。

市面上两种鼓机各有取舍，这里都取了长：
* 硬件式的**固定鼓件灯格**（TR-808）：一眼懂，但鼓件固定、无法换套鼓
  → 这里鼓件行由该轨 `kit` 的 `enabled` 决定，可以逐件开关。
* 软件式的**参数面板**：可调性高，但脱离网格
  → 这里参数面板与网格同页，选中哪一行就调哪一行。

交互（一张表看懂）
-----------------
==========================  ============================================
左键点空格                  点亮，**并立刻试听该鼓件**
左键点已亮的格子            熄灭（不发声 —— 安静本身就是反馈）
左键按住拖动                连续刷音符（经过已亮的格不会擦掉它）
右键                       擦除
点小节尺                    把播放头挪到那一步
点行表头的鼓件名            选中这件鼓（下方面板切过去）**并试听一次**
点行表头右端的小开关        静音 / 启用这件鼓（该行会消失，对应 DrumVoice.enabled）
Ctrl + 滚轮                 缩放网格
空格 / Delete / ← →         走带交给全局；Delete 清空当前鼓件，← → 挪播放头一步
==========================  ============================================

"边调边听"是本页面的核心：任何参数变化都会立刻发声。听不到衰减变化的鼓机
等于没做 —— 所以试听不是"加分项"，而是主流程的一部分。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import drums as drum_engine
from app.audio.player import audition
from app.audio.project import (
    DEFAULT_KIT, DRUM_NAMES, KIT_ORDER, Note, Project, Track, drum_to_pitch,
    pitch_to_drum,
)
from app.audio import soundpacks
from app.theme import accents, labels, radii, spacing, type_scale
from app.widgets.controls import GlassKnob
from app.widgets.glass import (
    GlassButton, GlassCard, GlassChip, GlassImageButton, GlassPanel, make_label,
)

# --------------------------------------------------------------------------
# 尺寸常量
# --------------------------------------------------------------------------

HEADER_W = 158.0            # 左侧鼓件表头宽度（色条 + 图标 + 名字 + 计数 + 开关）
RULER_H = 26.0              # 顶部小节尺高度
ROW_H = 26.0                # 每件鼓一行。比音序器的 21px 高 —— 这里一格就是"一个音"，
                            # 要够大才点得准
SWITCH_W = 30.0             # 行表头右端的启用开关（iOS 小药丸）
SWITCH_H = 16.0

MIN_STEP_W = 7.0
MAX_STEP_W = 34.0
DEFAULT_STEP_W = 16.0

#: 一件鼓都没启用时网格中间的那句话。
#: 抽成常量是为了让测试能断言"空网格真的会说话"，而不是只验一句不可见的注释。
EMPTY_ROWS_HINT = "这条轨的鼓件都被静音了 —— 点表头右端的小开关重新启用一件"

#: 试听时用的力度（点格子时用该鼓件的默认力度，这个值只在没有音符上下文时兜底）
AUDITION_VELOCITY = 0.85

#: 连续拖动旋钮时两次试听之间的最小间隔（秒）。
#: 一次拖动会发几十个 valueChanged；每个都渲染 + 打断上一发会糊成一串噪音，
#: 而且 render_drum 本身要几毫秒。70ms（≈14 次/秒）已经密到听不出"跳"，
#: 又让引擎喘得过气。**最后一个值一定会响**（补一次尾音），所以不会漏听。
AUDITION_MIN_INTERVAL = 0.07

#: 连续型参数（旋钮）改动在撤销栈里的标签。
#: 单独抽成常量是因为"要不要按手势合并撤销点"是**按标签**判断的：
#: 旋钮是连续量（一次拖动几十个值，只能占一步），开关是离散动作（点几下就是几步）。
PARAM_EDIT_LABEL = "调整鼓音色"

#: 两次旋钮改动间隔超过这个秒数才算"新手势"，见 DrumPage._on_param_edit。
PARAM_GESTURE_GAP = 0.6

#: 手工点音符时，每种鼓件给一个合理的默认力度。
#: 全都用 0.85 会让整条鼓轨没有动态 —— 鼓的"人味"大半来自力度差。
PIECE_VELOCITY: dict[str, float] = {
    "kick": 1.0, "snare": 0.92, "clap": 0.80,
    "hat_closed": 0.60, "hat_pedal": 0.50, "hat_open": 0.56,
    "tom_low": 0.80, "tom_mid": 0.78, "tom_high": 0.76,
    "rim": 0.60, "crash": 0.55, "ride": 0.48, "cowbell": 0.58,
}


# --------------------------------------------------------------------------
# 鼓型
# --------------------------------------------------------------------------
#
# 鼓型用**一小节 16 个位置**描述（也就是 1/16 音符网格），落盘时再按
# ``steps_per_bar`` 映射到真实步号 —— 工程的拍号若不是 4/4，鼓型照样能铺。
#
# 为什么不把鼓型写成绝对步号？因为那样它就和 4/4 死绑了；而鼓型是**节奏型**，
# 描述的是"一小节里的第几拍"，与总步数无关。

PATTERNS: tuple[tuple[str, str, dict[str, tuple[int, ...]]], ...] = (
    ("四拍", "底鼓踩满四拍 · 拍手补反拍", {
        "kick": (0, 4, 8, 12),
        "clap": (4, 12),
        "hat_closed": tuple(range(0, 16, 2)),
    }),
    ("反拍", "军鼓落在 2 / 4 拍", {
        "kick": (0, 10),
        "snare": (4, 12),
        "hat_closed": tuple(range(0, 16, 2)),
    }),
    ("十六分", "踩镲十六分推进", {
        "kick": (0, 8),
        "snare": (4, 12),
        "hat_closed": tuple(range(0, 16)),
    }),
    ("碎拍", "Breakbeat 切分 + 边击点缀", {
        "kick": (0, 3, 6, 10),
        "snare": (4, 12),
        "rim": (7, 14),
        "hat_closed": tuple(range(0, 16, 2)),
    }),
    ("加花", "小节末尾过门（嗵鼓下行）", {
        "kick": (0,),
        "crash": (0,),
        "tom_high": (8, 10),
        "tom_low": (12, 13),
        "snare": (14,),
    }),
)

PATTERN_NAMES: tuple[str, ...] = tuple(p[0] for p in PATTERNS)


def pattern_velocity(piece: str, pos: int) -> float:
    """鼓型里某个位置该用多大力度 —— 重拍重、弱拍轻，否则鼓型就是打点机。"""
    if piece == "kick":
        return 1.0 if pos % 8 == 0 else 0.88
    if piece in ("snare", "clap"):
        return 0.94 if pos % 8 == 4 else 0.82
    if piece.startswith("hat"):
        return 0.62 if pos % 4 == 0 else 0.44
    return 0.72


def piece_icon(piece: str) -> str:
    """挑一个贴切的 Twemoji 图标。鼓件之间要有区分度，否则一列图标长得全一样。"""
    if piece in ("kick", "snare", "clap", "rim"):
        return "drums"
    if piece.startswith("hat"):
        return "bell"
    if piece in ("crash", "ride"):
        return "sparkles"
    if piece.startswith("tom"):
        return "party"
    return "bell"


def piece_velocity(piece: str) -> float:
    return PIECE_VELOCITY.get(piece, AUDITION_VELOCITY)


# --------------------------------------------------------------------------
# 试听
# --------------------------------------------------------------------------


def render_piece_result(track: Track, piece: str, velocity: float = AUDITION_VELOCITY):
    """渲染单个鼓件，并把**失败原因**一起带出来：返回 ``(缓冲, 原因)``。

    **不走整轨渲染**：那要几秒。这里只合成这一件，几毫秒出结果 ——
    边点边听的前提就是它够快。

    为什么不让异常冒出去：试听是"边调边听"的主流程，一件鼓渲染不出来不该把
    整个页面拖崩。但**也绝不能静默** —— 用户点了不响、界面上一个字都没有，
    和软件坏了没有区别。所以原因必须随返回值一路走到界面上（成功时为空串）。
    """
    if track is None:
        return None, "还没有选中鼓轨"
    try:
        voice = track.kit.get(piece)
    except Exception as exc:                     # kit 都没建起来
        return None, f"读不到套鼓：{type(exc).__name__}"
    if voice is None:
        return None, "这件鼓不在套鼓里"
    try:
        buf = drum_engine.render_drum(piece, voice, velocity)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if buf is None or getattr(buf, "size", 0) == 0:
        return None, "声音引擎返回了空缓冲"
    return buf, ""


def render_piece(track: Track, piece: str, velocity: float = AUDITION_VELOCITY):
    """只要缓冲、不要原因的老接口：失败返回 None。"""
    return render_piece_result(track, piece, velocity)[0]


def play_piece_result(track: Track, piece: str,
                      velocity: float = AUDITION_VELOCITY) -> tuple[bool, str]:
    """渲染并立刻发声。返回 ``(是否响了, 失败原因)``。

    保留 bool 版 ``play_piece`` 是为了不破坏已有调用方；但界面上必须知道
    "为什么没响"（渲染不出来 / 播放器没起来是两回事），所以细节走这个函数。
    """
    buf, why = render_piece_result(track, piece, velocity)
    if buf is None:
        return False, why
    try:
        p = audition()
        if p is None:
            return False, "试听器没起来（音频设备可能被占用）"
        if not p.play_note(buf):
            return False, "播放器拒绝了这次试听"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def play_piece(track: Track, piece: str, velocity: float = AUDITION_VELOCITY) -> bool:
    """渲染并立刻发声。返回是否真的送进了播放器。"""
    return play_piece_result(track, piece, velocity)[0]


def audition_failure(piece: str, reason: str) -> str:
    """拼一句给用户看的失败说明。

    带上鼓件名：一条"没出声"在 12 件鼓的页面上等于没说，用户接下来该动哪件
    鼓、哪个旋钮，全靠这句话里的名字。
    """
    return f"「{DRUM_NAMES.get(piece, piece)}」没能发声 —— {reason}"


# ==========================================================================
# 步进网格
# ==========================================================================


class DrumGrid(QWidget):
    """鼓机网格本体：画鼓件行 × 步，并处理全部鼠标交互。"""

    notesChanged = Signal()
    #: 一次编辑**即将开始**时发一次（按下鼠标那一刻）。
    #: 拖动全程只发一次，于是"拖一整笔"在撤销栈里只占一步。
    editStarted = Signal(str)
    #: 步宽变了（Ctrl+滚轮与页面的放大/缩小按钮都会走到 set_step_width）。
    #:
    #: 为什么要发这个信号：步宽的**唯一真值源是 grid.step_w**（绘制用的就是它），
    #: 页面的 _zoom_level 只是它的倍数读数。以前滚轮直接改 step_w 却不通知，
    #: 页面还拿着旧的 _zoom_level 去算"再放大 25%"，于是点放大反而变小。
    #: 与其让滚轮去反查页面，不如让唯一会改步宽的那个函数广播一声。
    stepWidthChanged = Signal(float)
    #: 请求把播放头挪到某秒（点小节尺时发）
    seekRequested = Signal(float)
    statusHint = Signal(str)
    #: 试听失败（渲染不出来 / 播放器没起来）。
    #:
    #: 单独一条信号而不是复用 statusHint：这不是"提示"，是"这里坏了"。
    #: 提示条那一行会被紧接着的普通提示挤掉（点行首就是"选中「底鼓」"），
    #: 而状态栏那一行不会 —— 失败必须留得住。
    auditionFailed = Signal(str)
    #: 用户点了某件鼓（表头名字）—— 下方面板应该切过去
    pieceSelected = Signal(str)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.track: Track | None = None
        self.rows: list[str] = []            # 当前显示的鼓件（只含 enabled 的）
        self.step_w = DEFAULT_STEP_W
        self.selected_row = 0
        self.follow = True

        self._position = 0.0
        self._playing = False
        self._hover: tuple[int, int] | None = None
        self._cursor = QPointF(0.0, 0.0)
        self._mode = ""                      # paint | erase
        self._last_cell: tuple[int, int] | None = None
        #: 这一笔（一次按下到松开）是否已经压过撤销点。
        #: 擦除是"懒压"的：右键按在空格子上不该进撤销栈，但按住右键横拖时
        #: 起手那格可能是空的、后面几格有音符 —— 撤销点必须跟着第一次真改动走。
        self._stroke_pushed = False
        #: 试听次数。测试与自检用 —— 没有它就没法验证"点一下真的响了"
        self.audition_count = 0

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # -- 数据 ------------------------------------------------------------

    def set_project(self, project: Project) -> None:
        self.project = project
        self.track = None
        self.rebuild()

    def set_track(self, track: Track | None) -> None:
        self.track = track
        if track is not None:
            track.ensure_kit()
        self.rows = []
        self.selected_row = 0
        self._hover = None
        self.rebuild()

    def rebuild(self) -> None:
        """按 kit 的 enabled 重建鼓件行，并保持"当前选中还是那一件"。"""
        keep = self.selected_piece
        if self.track is not None:
            self.track.ensure_kit()
        rows = [p for p in KIT_ORDER
                if self.track is not None
                and self.track.kit.get(p) is not None
                and self.track.kit[p].enabled]
        self.rows = rows
        if keep in rows:
            self.selected_row = rows.index(keep)
        else:
            self.selected_row = max(0, min(self.selected_row, len(rows) - 1))
        self._resize_to_content()

    @property
    def selected_piece(self) -> str | None:
        if not self.rows:
            return None
        return self.rows[max(0, min(self.selected_row, len(self.rows) - 1))]

    def select_piece(self, piece: str) -> None:
        if piece in self.rows:
            self.selected_row = self.rows.index(piece)
            self.update()

    def piece_voice(self, piece: str):
        if self.track is None:
            return None
        return self.track.kit.get(piece)

    def note_count(self, piece: str | None = None) -> int:
        """该件（或整轨）在全曲里的音符数。表头上显示它，一眼看出这件用没用上。"""
        if self.track is None:
            return 0
        if piece is None:
            return len(self.track.notes)
        pitch = drum_to_pitch(piece)
        return sum(1 for n in self.track.notes if n.pitch == pitch)

    def set_position(self, seconds: float, playing: bool = False) -> None:
        self._position = float(seconds)
        self._playing = bool(playing)
        self.update()

    def set_step_width(self, px: float) -> None:
        # 钳制后的**实际**值才广播出去：页面要同步的是"现在到底多宽"，
        # 不是"有人请求了多宽"
        self.step_w = max(MIN_STEP_W, min(MAX_STEP_W, float(px)))
        self._resize_to_content()
        self.stepWidthChanged.emit(self.step_w)

    def _content_size(self) -> tuple[int, int]:
        steps = self.project.total_steps
        w = int(HEADER_W + steps * self.step_w + 10)
        h = int(RULER_H + max(1, len(self.rows)) * ROW_H + 8)
        return max(w, 260), max(h, 120)

    def _resize_to_content(self) -> None:
        w, h = self._content_size()
        self.setFixedSize(w, h)
        self.update()

    # -- 坐标换算 --------------------------------------------------------

    def _row_top(self, row: int) -> float:
        return RULER_H + row * ROW_H

    def _row_at_y(self, y: float) -> int | None:
        if y < RULER_H:
            return None
        idx = int((y - RULER_H) // ROW_H)
        if 0 <= idx < len(self.rows):
            return idx
        return None

    def _step_at_x(self, x: float) -> int:
        s = int((x - HEADER_W) // self.step_w)
        return max(0, min(self.project.total_steps - 1, s))

    def _x_for_step(self, step: float) -> float:
        return HEADER_W + step * self.step_w

    def _cell_rect(self, row: int, step: int) -> QRectF:
        top = self._row_top(row)
        return QRectF(self._x_for_step(step) + 1.5, top + 3.5,
                      max(2.0, self.step_w - 3.0), ROW_H - 7.0)

    def _switch_rect(self, row: int) -> QRectF:
        """行表头右端的小开关的矩形。

        开关放在**表头末端**而不是网格末尾：网格有 128 步，真"行末尾"在
        2000px 开外，用户得先横向滚到底才能静音一件鼓 —— 那种开关没人会用。
        """
        return QRectF(HEADER_W - SWITCH_W - 8.0,
                      self._row_top(row) + (ROW_H - SWITCH_H) / 2.0,
                      SWITCH_W, SWITCH_H)

    def bar_of_step(self, step: float) -> int:
        spb = max(1, self.project.steps_per_bar)
        return max(0, min(self.project.bars - 1, int(step // spb)))

    def current_bar(self) -> int:
        """播放头所在小节（铺鼓型的目标小节）。"""
        spt = self.project.seconds_per_step
        step = self._position / spt if spt > 0 else 0.0
        return self.bar_of_step(step)

    def current_step(self) -> int:
        spt = self.project.seconds_per_step
        step = self._position / spt if spt > 0 else 0.0
        return max(0, min(self.project.total_steps - 1, int(step)))

    # -- 编辑动作 --------------------------------------------------------

    def toggle_enabled(self, row: int) -> None:
        """静音 / 启用一件鼓（写进 DrumVoice.enabled，不是只改界面）。"""
        if self.track is None or not (0 <= row < len(self.rows)):
            return
        piece = self.rows[row]
        voice = self.track.kit.get(piece)
        if voice is None:
            return
        turn_on = not voice.enabled
        self.editStarted.emit("启用鼓件" if turn_on else "静音鼓件")
        voice.enabled = turn_on
        self.rebuild()
        self.notesChanged.emit()
        self.statusHint.emit(
            f"「{DRUM_NAMES.get(piece, piece)}」已{'启用' if turn_on else '静音'}"
        )
        self.update()

    def set_note(self, row: int, step: int, on: bool, audition_on: bool = True) -> bool:
        """把某一格点亮/熄灭。返回**是否发生了改动**。

        ``on=True`` 且该格已亮时不重复加音符（拖过时靠这个幂等）。
        """
        if self.track is None or not (0 <= row < len(self.rows)):
            return False
        piece = self.rows[row]
        pitch = drum_to_pitch(piece)
        step = max(0, min(self.project.total_steps - 1, int(step)))
        existing = self.track.note_at(step, pitch)
        if on:
            if existing is not None:
                return False
            vel = piece_velocity(piece)
            self.track.add_note(Note(step=step, pitch=pitch, length=1, velocity=vel))
            if audition_on:
                self._audition(piece, vel)
            return True
        if existing is None:
            return False
        self.track.remove_note(step, pitch)
        return True

    def clear_piece(self, piece: str | None = None) -> int:
        """清空一件鼓（或整轨）的全部音符。返回删掉几个。"""
        if self.track is None:
            return 0
        if piece is None:
            removed = len(self.track.notes)
            self.track.notes = []
        else:
            pitch = drum_to_pitch(piece)
            removed = sum(1 for n in self.track.notes if n.pitch == pitch)
            self.track.notes = [n for n in self.track.notes if n.pitch != pitch]
        self.update()
        return removed

    def _audition(self, piece: str, velocity: float) -> str:
        """试听一件鼓。返回失败说明（成功时为空串），供调用方决定要不要再说一遍。"""
        self.audition_count += 1
        played, why = play_piece_result(self.track, piece, velocity)
        if played:
            return ""
        # 点了不响必须有回音：这是本页面唯一"坏了却看不出来"的地方
        text = audition_failure(piece, why)
        self.statusHint.emit(text)
        self.auditionFailed.emit(text)
        return text

    # -- 键盘 ------------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            piece = self.selected_piece
            if piece is not None:
                name = DRUM_NAMES.get(piece, piece)
                if self.note_count(piece) == 0:
                    # 本来就没音符 = 什么都没发生。压撤销点会让撤销栈里多出
                    # 一步"撤销了但界面纹丝不动"，但也不能一声不吭 ——
                    # 用户会以为 Delete 键坏了
                    self.statusHint.emit(f"「{name}」本来就没有鼓点")
                else:
                    self.editStarted.emit("清空鼓件")
                    n = self.clear_piece(piece)
                    self.notesChanged.emit()
                    self.statusHint.emit(f"已清空「{name}」（{n} 个音符）")
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = self.current_step() + (-1 if key == Qt.Key.Key_Left else 1)
            step = max(0, min(self.project.total_steps - 1, step))
            self.seekRequested.emit(self.project.step_time(step))
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            if self.rows:
                delta = -1 if key == Qt.Key.Key_Up else 1
                self.select_piece(self.rows[max(0, min(len(self.rows) - 1,
                                                        self.selected_row + delta))])
                self.pieceSelected.emit(self.selected_piece or "")
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # -- 滚轮 ------------------------------------------------------------

    def wheelEvent(self, event):
        """横向滚轴交给外层滚动区，Ctrl 时缩放。与音序器保持一致的手感。"""
        parent = self.parent()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parent()
        if parent is None:
            return
        dy = event.angleDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if dy > 0 else 1 / 1.15
            new_w = max(MIN_STEP_W, min(MAX_STEP_W, self.step_w * factor))
            anchor_x = event.position().x()
            step_at = (anchor_x - HEADER_W) / max(1e-6, self.step_w)
            self.set_step_width(new_w)
            new_anchor = self._x_for_step(step_at)
            bar = parent.horizontalScrollBar()
            bar.setValue(bar.value() + int(new_anchor - anchor_x))
        else:
            bar = parent.horizontalScrollBar()
            bar.setValue(bar.value() - dy)
        event.accept()

    # -- 鼠标 ------------------------------------------------------------

    def mousePressEvent(self, event):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = event.position()
        x, y = pos.x(), pos.y()
        button = event.button()

        # 小节尺 = 定位播放头
        if y < RULER_H:
            if button == Qt.MouseButton.LeftButton:
                self.seekRequested.emit(self.project.step_time(self._step_at_x(x)))
            return

        row = self._row_at_y(y)
        if row is None:
            return

        # 表头 = 选中 / 开关
        if x < HEADER_W:
            if self._switch_rect(row).adjusted(-3, -3, 3, 3).contains(pos):
                self.toggle_enabled(row)
                return
            self.selected_row = row
            piece = self.rows[row]
            # 点名字也试听一次 —— 表头在这里就是"鼓垫"，选音色和听音色应该是一件事
            failed = self._audition(piece, piece_velocity(piece))
            self.pieceSelected.emit(piece)
            self.statusHint.emit(f"选中「{DRUM_NAMES.get(piece, piece)}」")
            if failed:
                # 上面那句"选中"会立刻盖掉失败提示，所以失败要**再说一遍**：
                # 表头是本页面最常用的试听入口，这里静默等于白修
                self.statusHint.emit(failed)
            self.update()
            return

        self.selected_row = row
        self.pieceSelected.emit(self.rows[row])
        step = self._step_at_x(x)
        self._stroke_pushed = False

        if button == Qt.MouseButton.RightButton:
            self._mode = "erase"
            # 空操作不压点：撤销点在 _erase_at 里、等真的擦掉东西才压
            self._erase_at(row, step)
        else:
            self._mode = "paint"
            # 左键按下一定是"切换"，必然有改动，所以这里可以立刻压点
            self.editStarted.emit("编辑鼓点")
            self._stroke_pushed = True
            # 第一格是"切换"语义（点亮的可以点灭），之后拖过的格子只加不删
            self.set_note(row, step, not self._is_on(row, step))
        self._last_cell = (row, step)
        self.update()

    def _is_on(self, row: int, step: int) -> bool:
        if self.track is None or not (0 <= row < len(self.rows)):
            return False
        return self.track.note_at(step, drum_to_pitch(self.rows[row])) is not None

    def _erase_at(self, row: int, step: int) -> bool:
        """擦掉一格，并在**真的擦掉东西**时懒压一次撤销点。返回是否擦掉了。

        为什么撤销点不跟"按下鼠标"绑定：右键划空格子是极常见的随手动作
        （尤其想确认某格是空的），每划一下就进一次撤销栈，用户按撤销时会
        看到"撤销了但界面纹丝不动"—— 比不压点更让人怀疑软件坏了。
        """
        if not self._is_on(row, step):
            return False
        if not self._stroke_pushed:
            self.editStarted.emit("擦除鼓点")
            self._stroke_pushed = True
        return self.set_note(row, step, False)

    def mouseMoveEvent(self, event):
        pos = event.position()
        self._cursor = pos
        row = self._row_at_y(pos.y())
        self._hover = None if row is None else (row, self._step_at_x(pos.x()))

        if self._mode in ("paint", "erase"):
            if row is not None and pos.x() >= HEADER_W:
                step = self._step_at_x(pos.x())
                cell = (row, step)
                if cell != self._last_cell:
                    if self._mode == "erase":
                        self._erase_at(row, step)
                    else:
                        # 拖动只加不删：拖过已有音符时不应该把它擦掉
                        self.set_note(row, step, True)
                    self._last_cell = cell
        self.update()

    def mouseReleaseEvent(self, event):
        if self._mode:
            self.notesChanged.emit()
        self._mode = ""
        self._last_cell = None
        self._stroke_pushed = False
        self.update()

    def leaveEvent(self, event):
        self._hover = None
        self.update()
        super().leaveEvent(event)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(12, 13, 18, 214))

        steps = self.project.total_steps
        spb = max(1, self.project.steps_per_bar)
        grid_right = self._x_for_step(steps)
        rows_bottom = self._row_top(max(1, len(self.rows))) if self.rows else RULER_H + ROW_H

        self._paint_row_bands(painter, grid_right)
        self._paint_grid_lines(painter, steps, spb, grid_right, rows_bottom)
        self._paint_loop(painter, rows_bottom)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_cells(painter)
        self._paint_hover(painter)

        if not self.rows:
            self._paint_empty_rows(painter, grid_right, rows_bottom)

        self._paint_playhead(painter, grid_right, rows_bottom)

        # 表头与尺子最后画，压在网格之上
        painter.fillRect(QRectF(0, 0, HEADER_W, rows_bottom), QColor(10, 11, 15, 244))
        painter.fillRect(QRectF(0, 0, self.width(), RULER_H), QColor(10, 11, 15, 250))
        self._paint_header(painter)
        self._paint_ruler(painter, spb)
        self._paint_readout(painter)

        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.drawLine(int(HEADER_W), 0, int(HEADER_W), int(rows_bottom))
        painter.end()

    def _row_color(self, row: int) -> QColor:
        palette = accents.track_palette()
        return QColor(palette[row % len(palette)])

    def _paint_row_bands(self, painter: QPainter, grid_right: float) -> None:
        for i in range(len(self.rows)):
            top = self._row_top(i)
            if i == self.selected_row:
                c = QColor(self._row_color(i))
                c.setAlphaF(0.11)
                painter.fillRect(QRectF(HEADER_W, top, grid_right - HEADER_W, ROW_H), c)
            elif i % 2 == 1:
                painter.fillRect(QRectF(HEADER_W, top, grid_right - HEADER_W, ROW_H),
                                 QColor(255, 255, 255, 7))

    def _paint_grid_lines(self, painter: QPainter, steps: int, spb: int,
                          grid_right: float, rows_bottom: float) -> None:
        spbeat = max(1, self.project.steps_per_beat)
        draw_fine = self.step_w >= 12.0
        for s in range(steps + 1):
            if s % spb == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
            elif s % spbeat == 0:
                painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
            elif draw_fine:
                painter.setPen(QPen(QColor(255, 255, 255, 9), 1))
            else:
                continue
            x = int(self._x_for_step(s))
            painter.drawLine(x, int(RULER_H), x, int(rows_bottom))
        painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
        for i in range(len(self.rows) + 1):
            y = int(self._row_top(i))
            painter.drawLine(int(HEADER_W), y, int(grid_right), y)

    def _paint_loop(self, painter: QPainter, rows_bottom: float) -> None:
        lo, hi = self.project.loop_range
        if hi > lo:
            painter.fillRect(
                QRectF(self._x_for_step(lo), RULER_H,
                       (hi - lo) * self.step_w, rows_bottom - RULER_H),
                QColor(255, 255, 255, 9),
            )

    def _paint_cells(self, painter: QPainter) -> None:
        """画所有已点亮的格子。

        先建一次 ``(step, pitch) -> Note`` 的索引再画 —— 12 行 × 128 步 = 1536 格，
        每格都去 ``note_at`` 线性扫一遍音符列表的话，绘制会慢到拖影。
        """
        if self.track is None or not self.rows:
            return
        index = {(n.step, n.pitch): n for n in self.track.notes}
        left = HEADER_W
        right = self.width()
        for i, piece in enumerate(self.rows):
            pitch = drum_to_pitch(piece)
            base = QColor(self._row_color(i))
            for step in range(self.project.total_steps):
                x = self._x_for_step(step)
                if x + self.step_w < left or x > right:
                    continue
                note = index.get((step, pitch))
                if note is None:
                    continue
                r = self._cell_rect(i, step)
                c = QColor(base)
                c.setAlphaF(0.34 + 0.66 * max(0.0, min(1.0, float(note.velocity))))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(c)
                radius = min(3.0, r.height() / 2.0)
                painter.drawRoundedRect(r, radius, radius)
                # 重击加一道亮顶边：不换颜色的前提下把力度差再拉开一档
                if note.velocity > 0.85 and r.height() > 6.0:
                    painter.setBrush(QColor(255, 255, 255, 105))
                    painter.drawRoundedRect(
                        QRectF(r.x() + 1.0, r.y() + 0.8, r.width() - 2.0, 1.4), 0.7, 0.7)

    def _paint_hover(self, painter: QPainter) -> None:
        if self._hover is None or self._mode:
            return
        row, step = self._hover
        if not (0 <= row < len(self.rows)) or self._cursor.x() < HEADER_W:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        r = self._cell_rect(row, step)
        painter.drawRoundedRect(r.adjusted(-1.0, -2.0, 1.0, 2.0), 3.0, 3.0)

    def _paint_empty_rows(self, painter: QPainter, grid_right: float,
                          rows_bottom: float) -> None:
        """一件鼓都没启用时，网格是空的 —— 必须说清为什么空，否则像坏了。"""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(type_scale.callout.font())
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(QRectF(HEADER_W, RULER_H, max(120.0, grid_right - HEADER_W - 20),
                                max(40.0, rows_bottom - RULER_H)),
                         Qt.AlignmentFlag.AlignCenter, EMPTY_ROWS_HINT)

    def _paint_playhead(self, painter: QPainter, grid_right: float,
                        rows_bottom: float) -> None:
        spt = self.project.seconds_per_step
        step_f = self._position / spt if spt > 0 else 0.0
        px = self._x_for_step(step_f)
        if not (HEADER_W - self.step_w * 2 <= px <= grid_right):
            return
        g = QLinearGradient(px, RULER_H, px, rows_bottom)
        g.setColorAt(0.0, theme.white(0.95))
        g.setColorAt(1.0, theme.white(0.32))
        painter.setPen(QPen(g, 1.6))
        painter.drawLine(QPointF(px, RULER_H), QPointF(px, rows_bottom))
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
                             f"{bar + 1} 小节")
        painter.setPen(QPen(QColor(255, 255, 255, 22), 1))
        painter.drawLine(0, int(RULER_H - 1), self.width(), int(RULER_H - 1))

    def _paint_header(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if not self.rows:
            return
        for i, piece in enumerate(self.rows):
            top = self._row_top(i)
            color = self._row_color(i)
            selected = i == self.selected_row

            if selected:
                bar = QColor(color)
                bar.setAlphaF(0.95)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(bar)
                painter.drawRoundedRect(QRectF(3.0, top + 4.0, 3.0, ROW_H - 8.0), 1.5, 1.5)

            pm = icons.pixmap(piece_icon(piece), 14, opacity=0.8,
                              dpr=self.devicePixelRatioF())
            painter.drawPixmap(11, int(top + (ROW_H - 14) / 2), pm)

            f = type_scale.caption1.font()
            f.setWeight(QFont.Weight.DemiBold if selected else QFont.Weight.Normal)
            painter.setFont(f)
            fm = QFontMetricsF(f)
            count = self.note_count(piece)
            name_w = HEADER_W - 34.0 - SWITCH_W - 10.0
            painter.setPen(QPen(labels.label if selected else labels.secondary))
            painter.drawText(
                QRectF(30, top, name_w, ROW_H),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                fm.elidedText(DRUM_NAMES.get(piece, piece), Qt.TextElideMode.ElideRight,
                              name_w),
            )
            # 该件在全曲里用了多少个 —— "这件没用上"是编曲时最常要问的问题
            painter.setFont(type_scale.caption2.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(QRectF(HEADER_W - SWITCH_W - 44, top, 36, ROW_H),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             f"{count}")

            self._paint_switch(painter, self._switch_rect(i),
                               bool(self.track.kit[piece].enabled), color)

    def _paint_switch(self, painter: QPainter, r: QRectF, on: bool, color: QColor) -> None:
        """iOS 小药丸开关。开着=实心强调色 + 旋钮靠右；关着=空槽 + 旋钮靠左。"""
        painter.setPen(Qt.PenStyle.NoPen)
        if on:
            painter.setBrush(QColor(color))
        else:
            painter.setBrush(QColor(255, 255, 255, 34))
        painter.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)
        knob_r = r.height() - 3.0
        cx = r.right() - 1.5 - knob_r / 2.0 if on else r.left() + 1.5 + knob_r / 2.0
        painter.setBrush(QColor(250, 250, 252) if on else QColor(255, 255, 255, 150))
        painter.drawEllipse(QPointF(cx, r.center().y()), knob_r / 2.0, knob_r / 2.0)

    def _paint_readout(self, painter: QPainter) -> None:
        """跟随光标的读数：鼓件 · 小节 · 步 · 力度。

        不用 tooltip 是刻意的：tooltip 要悬停半秒才弹，还会挡住网格；
        铺鼓点时需要的是一眼扫到的即时反馈。
        """
        if self._hover is None or self.track is None or not self.rows:
            return
        row, step = self._hover
        if not (0 <= row < len(self.rows)) or self._cursor.x() < HEADER_W:
            return
        piece = self.rows[row]
        note = self.track.note_at(step, drum_to_pitch(piece))
        spb = max(1, self.project.steps_per_bar)
        bar = step // spb + 1
        beat = step % spb // max(1, self.project.steps_per_beat) + 1
        text = f"{DRUM_NAMES.get(piece, piece)} · 第 {bar} 小节 第 {beat} 拍 · 第 {step + 1} 步"
        if note is not None:
            text += f" · 力度 {round(note.velocity * 100)}%"

        f = type_scale.caption1.font()
        painter.setFont(f)
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text) + 16
        h = 20.0
        x = min(self._cursor.x() + 14, self.width() - w - 4)
        y = self._cursor.y() - h - 8
        if y < RULER_H:
            y = self._cursor.y() + 14
        box = QRectF(max(HEADER_W + 4, x), y, w, h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(16, 17, 23, 236))
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QPen(labels.label))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)


# ==========================================================================
# 参数面板
# ==========================================================================


class _KnobColumn(QWidget):
    """一个旋钮 + 名字 + 数值。数值常驻显示 —— 旋钮只能看出"大概"，看不出 0.35 秒。"""

    def __init__(self, title: str, knob: GlassKnob, parent: QWidget | None = None):
        super().__init__(parent)
        self.knob = knob
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(knob)
        row.addStretch(1)
        v.addLayout(row)
        self.name = make_label(title, type_scale.caption2, labels.tertiary, self)
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.name)
        self.value = make_label("—", type_scale.mono, labels.secondary, self)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.value)
        self.setFixedWidth(72)


def format_param(name: str, value: float) -> str:
    if name == "tune":
        return f"{value:+.1f} 半音"
    if name == "decay":
        return f"{int(round(value * 1000))} ms"
    if name == "pan":
        if abs(value) < 0.02:
            return "居中"
        return f"{'左' if value < 0 else '右'} {abs(value):.0%}"
    return f"{value:.2f}"


class DrumParamsPanel(GlassCard):
    """选中鼓件的六个参数（对应 DrumVoice 的六个字段）。

    **参数一变就写入 DrumVoice 并发试听请求** —— 面板不持有"草稿值"，
    界面永远直接反映工程数据。这样撤销/重做之后重绑一次就完全同步，
    不需要在两处维护同一份状态。

    而正因为"写"发生在这里，撤销点也必须由这里发起：``editStarted`` 在
    ``setattr`` **之前**发出去。曾经把压点交给页面的 ``changed`` 信号去做，
    结果是快照里已经存进了新值 —— 撤销把新值又恢复一遍，用户看到的是
    "按了撤销但什么都没变"。
    """

    #: 参数改动（需要重渲染整曲）
    changed = Signal()
    #: 参数**即将**被写进 DrumVoice（撤销点压在这一刻，见模块内 PARAM_EDIT_LABEL）
    editStarted = Signal(str)
    #: 请求试听当前鼓件
    auditionRequested = Signal()
    #: 启用开关被点
    enabledChanged = Signal()

    #: (字段名, 显示名, 最小, 最大, 是否双极)
    PARAMS: tuple[tuple[str, str, float, float, bool], ...] = (
        ("level", "音量", 0.0, 1.0, False),
        ("tune", "调音", -12.0, 12.0, True),
        ("decay", "衰减", 0.01, 2.0, False),
        ("tone", "音色", 0.0, 1.0, False),
        ("snap", "冲击", 0.0, 1.0, False),
        ("pan", "声像", -1.0, 1.0, True),
    )

    HEIGHT = 108
    #: 左侧身份区（图标 + 名字）宽度，布局从此处开始
    ID_W = 152

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, radius=radii.card, liquid=False,
                         material=theme.black(0.28))
        self.piece: str | None = None
        self.voice = None
        self._color = QColor(accents.orange)
        self._values: dict[str, float] = {}
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_accent(self._color)

        row = QHBoxLayout(self)
        row.setContentsMargins(int(self.ID_W), 10, 14, 10)
        row.setSpacing(6)

        self.btn_enable = GlassImageButton("speaker_high", self, size=32, icon_size=20,
                                           style="plain", tooltip="启用 / 静音这件鼓")
        self.btn_enable.clicked.connect(self._toggle_enabled)
        row.addWidget(self.btn_enable)

        self.btn_audition = GlassButton("试听", self, style="tinted", compact=True,
                                        accent=accents.orange)
        self.btn_audition.setFixedWidth(52)
        self.btn_audition.clicked.connect(self.auditionRequested)
        row.addWidget(self.btn_audition)

        row.addSpacing(4)
        self.columns: list[_KnobColumn] = []
        self._column_of: dict[str, _KnobColumn] = {}
        for name, title, lo, hi, bipolar in self.PARAMS:
            knob = GlassKnob(self, lo, hi, (lo + hi) / 2.0, accent=self._color,
                             size=36, center_detent=bipolar)
            col = _KnobColumn(title, knob, self)
            knob.valueChanged.connect(lambda v, n=name: self._on_knob(n, float(v)))
            self.columns.append(col)
            self._column_of[name] = col
            row.addWidget(col)
        row.addStretch(1)

        self._hint = make_label("", type_scale.caption2, labels.quaternary, self)
        row.addWidget(self._hint)

    # -- 绑定 ------------------------------------------------------------

    def set_voice(self, piece: str | None, voice, track_color: QColor | None = None) -> None:
        """把面板绑到某件鼓的 DrumVoice 上，并把旋钮刷成当前值。"""
        self.piece = piece
        self.voice = voice
        if track_color is not None:
            self._color = QColor(track_color)
            self.set_accent(self._color)
            self.btn_audition.set_accent(self._color)
            for col in self.columns:
                col.knob.set_accent(self._color)
        if voice is None:
            self._values = {}
            for col in self.columns:
                col.value.setText("—")
            self._hint.setText("选一件鼓")
            self.update()
            return
        self._values = {}
        for (name, _title, lo, hi, _bi), col in zip(self.PARAMS, self.columns):
            v = float(getattr(voice, name, (lo + hi) / 2.0))
            self._values[name] = v
            col.knob.set_value(v, notify=False)
            col.value.setText(format_param(name, v))
        self.btn_enable.set_icon("speaker_high" if voice.enabled else "speaker_mute")
        self.btn_enable.set_dimmed(not voice.enabled)
        self.btn_audition.setEnabled(voice.enabled)
        self._hint.setText("调音色" if voice.enabled else "已静音")
        self.update()

    def refresh_enabled(self) -> None:
        if self.voice is not None:
            self.btn_enable.set_icon("speaker_high" if self.voice.enabled else "speaker_mute")
            self.btn_enable.set_dimmed(not self.voice.enabled)
            self.btn_audition.setEnabled(self.voice.enabled)

    # -- 编辑 ------------------------------------------------------------

    def _on_knob(self, name: str, value: float) -> None:
        if self.voice is None:
            return
        col = self._column_of.get(name)
        if col is None:
            return
        # 顺序不能反：先请页面压撤销点（此时 voice 还是旧值），再改 voice。
        # 反过来的话快照里已经是新值，这一步就再也撤不回去了。
        self.editStarted.emit(PARAM_EDIT_LABEL)
        setattr(self.voice, name, float(value))
        self._values[name] = float(value)
        col.value.setText(format_param(name, float(value)))
        self.changed.emit()
        self.auditionRequested.emit()

    def _toggle_enabled(self) -> None:
        if self.voice is None:
            return
        # 同上：标签按"要做的动作"取（翻转前判断），压点也必须在翻转前
        turn_on = not self.voice.enabled
        self.editStarted.emit("启用鼓件" if turn_on else "静音鼓件")
        self.voice.enabled = turn_on
        self.refresh_enabled()
        self._hint.setText("调音色" if self.voice.enabled else "已静音")
        self.enabledChanged.emit()

    def values(self) -> dict[str, float]:
        return dict(self._values)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        if self.piece is None:
            painter.setFont(type_scale.callout.font())
            painter.setPen(QPen(labels.tertiary))
            painter.drawText(QRectF(16, 0, self.width() - 32, self.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             "点网格左侧的鼓件名，选一件鼓来调音色")
            painter.end()
            return

        pm = icons.pixmap(piece_icon(self.piece), 34, dpr=self.devicePixelRatioF())
        painter.drawPixmap(16, int((self.height() - 34) / 2), pm)

        f = type_scale.subhead.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        painter.drawText(QRectF(58, 18, self.ID_W - 70, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         DRUM_NAMES.get(self.piece, self.piece))

        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(QRectF(58, 38, self.ID_W - 70, 15),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         "鼓件音色")

        # 左侧一条强调色，和网格里那一行同色 —— 面板与网格是同一件东西的两面
        painter.setPen(Qt.PenStyle.NoPen)
        bar = QColor(self._color)
        bar.setAlphaF(0.9)
        painter.setBrush(bar)
        painter.drawRoundedRect(QRectF(4.0, 14.0, 3.0, self.height() - 28.0), 1.5, 1.5)
        painter.end()


# ==========================================================================
# 页面
# ==========================================================================


class DrumPage(QWidget):
    """鼓机页面：轨道选择 + 鼓型 + 步进网格 + 参数面板。"""

    #: 用户改了工程内容，需要重渲染。**改动前必须先 controller.push_undo(标签)**
    edited = Signal()
    #: 请求把播放头移到某个时间（秒）
    seekRequested = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.track: Track | None = None
        self._controller = None
        self._zoom_level = 1.0
        self._last_audition = 0.0
        #: 试听请求次数（含被节流合并的）—— 用于自检与测试
        self.audition_count = 0
        self._suspend_sync = False

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)

        # ---- 顶栏：轨道选择 / 撤销 / 缩放 --------------------------------
        bar = GlassPanel(self, material=theme.black(0.20), radius=radii.control)
        bar.setFixedHeight(46)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 6, 12, 6)
        bl.setSpacing(5)

        ico = QLabel(bar)
        ico.setPixmap(icons.pixmap("drums", 26, dpr=self.devicePixelRatioF()))
        ico.setFixedSize(28, 28)
        bl.addWidget(ico)

        title = QLabel("鼓机", bar)
        tf = type_scale.title3.font()
        title.setFont(tf)
        title.setStyleSheet(self._css(labels.label))
        bl.addWidget(title)
        bl.addSpacing(8)

        self.track_host = QWidget(bar)
        self.track_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.track_row = QHBoxLayout(self.track_host)
        self.track_row.setContentsMargins(0, 0, 0, 0)
        self.track_row.setSpacing(5)
        bl.addWidget(self.track_host)
        bl.addStretch(1)

        def mk(icon_name, tip, slot, size=28, isize=16):
            b = GlassImageButton(icon_name, bar, size=size, icon_size=isize, tooltip=tip)
            b.clicked.connect(slot)
            bl.addWidget(b)
            return b

        self.btn_undo = mk("undo", "撤销 (Ctrl+Z)", self._undo)
        self.btn_redo = mk("redo", "重做 (Ctrl+Y)", self._redo)
        bl.addSpacing(6)
        self.btn_zoom_out = mk("search", "缩小网格 (Ctrl+滚轮)", lambda: self._zoom(0.8), isize=15)
        self.btn_zoom_in = mk("zoom_in", "放大网格 (Ctrl+滚轮)", lambda: self._zoom(1.25), isize=17)
        bl.addSpacing(6)
        self.btn_home = mk("arrow_back", "回到开头", lambda: self.seekRequested.emit(0.0))
        self.bar = bar

        # ---- 鼓型行 -------------------------------------------------------
        pattern_bar = GlassCard(self, radius=radii.control)
        pattern_bar.setFixedHeight(38)
        pl = QHBoxLayout(pattern_bar)
        pl.setContentsMargins(12, 4, 12, 4)
        pl.setSpacing(6)
        plabel = QLabel("鼓型", pattern_bar)
        plabel.setFont(type_scale.caption1.font())
        plabel.setStyleSheet(self._css(labels.tertiary))
        pl.addWidget(plabel)

        self.pattern_chips: list[GlassChip] = []
        for i, (name, desc, _steps) in enumerate(PATTERNS):
            chip = GlassChip(name, pattern_bar, accent=accents.track_palette()[i % 8],
                             checkable=False)
            chip.setToolTip(f"{desc} · 铺到播放头所在小节")
            chip.clicked.connect(lambda n=name: self.apply_pattern(n))
            pl.addWidget(chip)
            self.pattern_chips.append(chip)

        pl.addSpacing(8)
        self.btn_clear_bar = GlassChip("清空本小节", pattern_bar, accent=accents.red,
                                       checkable=False)
        self.btn_clear_bar.setToolTip("删掉当前小节里这条鼓轨的全部鼓点")
        self.btn_clear_bar.clicked.connect(self.clear_current_bar)
        pl.addWidget(self.btn_clear_bar)

        pl.addStretch(1)
        self.pattern_hint = QLabel("", pattern_bar)
        self.pattern_hint.setFont(type_scale.caption2.font())
        self.pattern_hint.setStyleSheet(self._css(labels.quaternary))
        pl.addWidget(self.pattern_hint)
        self.pattern_bar = pattern_bar

        # ---- 音色包行 -----------------------------------------------------
        # 放在鼓型行的正下方：两者都是"一次点击换一整套东西"，
        # 一个是节奏型、一个是音色，性质相同就该挨着。
        pack_bar = GlassCard(self, radius=radii.control)
        pack_bar.setFixedHeight(38)
        kl = QHBoxLayout(pack_bar)
        kl.setContentsMargins(12, 4, 12, 4)
        kl.setSpacing(6)
        klabel = QLabel("音色包", pack_bar)
        klabel.setFont(type_scale.caption1.font())
        klabel.setStyleSheet(self._css(labels.tertiary))
        kl.addWidget(klabel)

        self.pack_chips: list[GlassChip] = []
        for pack in soundpacks.SOUND_PACKS:
            chip = GlassChip(pack.name, pack_bar,
                             accent=getattr(accents, pack.accent, accents.orange),
                             checkable=False)
            chip.setToolTip(soundpacks.describe(pack) + f" · {pack.tip}")
            chip.clicked.connect(lambda k=pack.key: self.apply_sound_pack(k))
            kl.addWidget(chip)
            self.pack_chips.append(chip)

        kl.addStretch(1)
        self.pack_hint = QLabel("套上 808 / 牛铃，新鼓件会作为新行出现在网格里",
                                pack_bar)
        self.pack_hint.setFont(type_scale.caption2.font())
        self.pack_hint.setStyleSheet(self._css(labels.quaternary))
        kl.addWidget(self.pack_hint)
        self.pack_bar = pack_bar

        # ---- 主体：网格 / 空状态 ------------------------------------------
        self.body = QStackedWidget(self)
        self.body.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.grid = DrumGrid(project, self)
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

        self.params = DrumParamsPanel(self)

        editor = QWidget(self)
        editor.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        ev = QVBoxLayout(editor)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(spacing.sm)
        ev.addWidget(self.scroll, 1)
        ev.addWidget(self.params)
        self.editor = editor

        self.empty = self._build_empty_state()
        self.body.addWidget(self.empty)     # 0
        self.body.addWidget(editor)         # 1

        # ---- 底部提示条 ---------------------------------------------------
        hint = GlassCard(self, radius=radii.control)
        hint.setFixedHeight(30)
        hl = QHBoxLayout(hint)
        hl.setContentsMargins(12, 0, 12, 0)
        self._hint = QLabel(
            "左键点亮（会发声）· 按住拖动连刷 · 右键擦除 · "
            "点行首鼓件名选中并试听 · 点行尾小开关静音", hint)
        self._hint.setFont(type_scale.caption1.font())
        self._hint.setStyleSheet(self._css(labels.tertiary))
        hl.addWidget(self._hint)
        hl.addStretch(1)
        self._count = QLabel("", hint)
        self._count.setFont(type_scale.caption1.font())
        self._count.setStyleSheet(self._css(labels.secondary))
        hl.addWidget(self._count)
        self.hint_bar = hint

        root.addWidget(bar)
        root.addWidget(pattern_bar)
        root.addWidget(pack_bar)
        root.addWidget(self.body, 1)
        root.addWidget(hint)

        # ---- 试听节流器 ---------------------------------------------------
        self._audition_timer = QTimer(self)
        self._audition_timer.setSingleShot(True)
        self._audition_timer.setInterval(int(AUDITION_MIN_INTERVAL * 1000) + 5)
        self._audition_timer.timeout.connect(self._play_audition)

        # ---- 接线 ---------------------------------------------------------
        self.grid.editStarted.connect(self._commit)
        self.grid.notesChanged.connect(self._on_notes_changed)
        # 步宽的唯一真值源在 grid 里；_zoom_level 只是跟着它走的倍数读数
        self.grid.stepWidthChanged.connect(self._on_step_width_changed)
        self.grid.pieceSelected.connect(self._on_piece_selected)
        self.grid.seekRequested.connect(self.seekRequested)
        self.grid.statusHint.connect(self._set_hint)
        self.grid.auditionFailed.connect(self._on_audition_failed)
        self.params.changed.connect(self._on_params_changed)
        # 撤销点由面板在**写 voice 之前**请求（见 _on_param_edit）——
        # 不能挂在 changed 上，那个信号发出来时改动已经落地了
        self.params.editStarted.connect(self._on_param_edit)
        self.params.auditionRequested.connect(self._on_param_audition)
        self.params.enabledChanged.connect(self._on_param_enabled)
        self._sync_label_colors()
        self.rebuild()

    # -- 小工具 ----------------------------------------------------------

    @staticmethod
    def _css(color: QColor) -> str:
        return (f"color: rgba({color.red()},{color.green()},{color.blue()},"
                f"{color.alphaF():.3f});background:transparent;")

    def _sync_label_colors(self) -> None:
        self.pattern_hint.setStyleSheet(self._css(labels.quaternary))

    def _set_hint(self, text: str) -> None:
        self._hint.setText(text)

    def _on_audition_failed(self, text: str) -> None:
        """网格那边试听失败 —— 再往状态栏送一次。

        提示条只有一行，点行首时会被"选中「底鼓」"立刻盖掉；状态栏停 5 秒，
        用户至少有一次机会看到"不是没做，是坏了"。
        """
        self._status(text, 5000)

    def _build_empty_state(self) -> QWidget:
        """没有鼓轨时的空状态。

        绝不是"空白一片"：要讲清**为什么没有**、以及**下一步点哪里**，
        并且顺手给一个能直接用的按钮 —— 空状态是引导，不是墓碑。
        """
        host = QWidget(self)
        host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        card = GlassCard(host, radius=radii.card, liquid=False, material=theme.black(0.30))
        card.setFixedSize(430, 190)
        card.set_accent(accents.orange)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(22, 18, 22, 18)
        inner.setSpacing(spacing.xs)

        ico = QLabel(card)
        ico.setPixmap(icons.pixmap("drums", 46, opacity=0.95,
                                   dpr=self.devicePixelRatioF()))
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(ico)

        t = QLabel("工程里还没有鼓轨", card)
        tf = type_scale.title3.font()
        t.setFont(tf)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(self._css(labels.label))
        inner.addWidget(t)

        d = QLabel("鼓机编辑的是某一条鼓轨（kind = drum）。\n"
                   "可以现在新建一条，也可以到「总览」页点「＋ 鼓组轨道」。", card)
        d.setFont(type_scale.caption1.font())
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d.setWordWrap(True)
        d.setStyleSheet(self._css(labels.tertiary))
        inner.addWidget(d)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_new_track = GlassButton("新建鼓组轨道", card, style="filled",
                                        accent=accents.orange)
        self.btn_new_track.clicked.connect(self.create_drum_track)
        row.addWidget(self.btn_new_track)
        row.addStretch(1)
        inner.addLayout(row)

        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        self.empty_card = card
        return host

    # -- 与控制器对接 ----------------------------------------------------

    def bind(self, controller) -> None:
        self._controller = controller
        # 控制器不一定实现了撤销状态信号（测试台就是个鸭子类型的替身），
        # 所以这里必须容错，不能让页面依赖一个可选能力。
        if hasattr(controller, "undoStateChanged"):
            try:
                controller.undoStateChanged.connect(self._on_undo_state)
            except Exception:
                pass
        self.rebuild()

    def _on_undo_state(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_undo.set_dimmed(not can_undo)
        self.btn_redo.set_dimmed(not can_redo)

    def _commit(self, label: str) -> None:
        """在改动**之前**压撤销点。"""
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.push_undo(label)
            except Exception:
                pass

    def _status(self, text: str, ms: int = 3000) -> None:
        ctrl = self._controller
        if ctrl is not None and hasattr(ctrl, "statusMessage"):
            try:
                ctrl.statusMessage.emit(text, ms)
            except Exception:
                pass

    def _undo(self) -> None:
        ctrl = self._controller
        if ctrl is None:
            return
        try:
            ctrl.undo()
        except Exception:
            return
        self.rebuild()
        self.edited.emit()

    def _redo(self) -> None:
        ctrl = self._controller
        if ctrl is None:
            return
        try:
            ctrl.redo()
        except Exception:
            return
        self.rebuild()
        self.edited.emit()

    # -- 工程 / 轨道 -----------------------------------------------------

    def drum_tracks(self) -> list[Track]:
        return [t for t in self.project.tracks if t.kind == "drum"]

    @property
    def has_drum_track(self) -> bool:
        return bool(self.drum_tracks())

    def set_project(self, project: Project) -> None:
        self.project = project
        self.grid.set_project(project)
        self.track = None
        self.rebuild()

    def set_track(self, track: Track | None) -> None:
        """切换当前编辑的鼓轨。"""
        if track is not None and track.kind != "drum":
            track = None
        self.track = track
        self.grid.set_track(track)
        self._sync_chips()
        self._sync_params()
        self._update_counts()

    def rebuild(self) -> None:
        """结构变化（增删轨道 / 撤销）后重建：轨道芯片、网格、参数面板。

        当前轨道按 **id** 重新找，而不是按对象身份或相等性：
        撤销是"就地替换内容"，Track 对象会被整体换掉（新实例），
        而 dataclass 的 ``==`` 又是按值比较 —— 两种做法都会让页面继续
        指向一个已经不在工程里的旧轨道，表现为"撤销后网格不跟着变"。
        """
        tracks = self.drum_tracks()
        cur_id = self.track.id if self.track is not None else None
        match = next((t for t in tracks if t.id == cur_id), None)
        self.track = match if match is not None else (tracks[0] if tracks else None)
        self._rebuild_chips(tracks)
        self.grid.set_track(self.track)
        self._sync_chips()
        self._sync_params()
        self.body.setCurrentIndex(1 if self.track is not None else 0)
        self._update_counts()

    def refresh_notes(self) -> None:
        """只是音符/参数变了 —— 重画即可，不重建控件。"""
        self.grid.rebuild()
        self._sync_params(keep_values=True)
        self._update_counts()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        self.grid.set_position(seconds, playing)
        if playing and self.grid.follow:
            self._follow(float(seconds))

    def _follow(self, seconds: float) -> None:
        """让播放头留在视口内。

        留出左右各 15% 的余量再滚：卡在边缘才滚会让画面持续抽动，
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

    # -- 轨道芯片 --------------------------------------------------------

    def _rebuild_chips(self, tracks: list[Track]) -> None:
        while self.track_row.count():
            item = self.track_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.track_chips = []
        if not tracks:
            lbl = QLabel("没有鼓轨", self.track_host)
            lbl.setFont(type_scale.caption1.font())
            lbl.setStyleSheet(self._css(labels.quaternary))
            self.track_row.addWidget(lbl)
            return
        for i, t in enumerate(tracks):
            chip = GlassChip(t.name, self.track_host, accent=QColor(t.color),
                             checkable=True, selected=False)
            chip.setToolTip(f"{t.name} · {len(t.notes)} 个鼓点")
            chip.clicked.connect(lambda _=False, tr=t: self.set_track(tr))
            self.track_row.addWidget(chip)
            self.track_chips.append(chip)

    def _sync_chips(self) -> None:
        tracks = self.drum_tracks()
        for chip, t in zip(getattr(self, "track_chips", []), tracks):
            chip.setChecked(t is self.track)

    # -- 参数面板 --------------------------------------------------------

    def _sync_params(self, keep_values: bool = False) -> None:
        piece = self.grid.selected_piece
        voice = self.grid.piece_voice(piece) if piece else None
        color = QColor(self.track.color) if self.track is not None else accents.orange
        self.params.set_voice(piece, voice, color)
        if keep_values and voice is not None and piece is not None:
            for (name, _t, _lo, _hi, _b), col in zip(DrumParamsPanel.PARAMS,
                                                     self.params.columns):
                v = float(getattr(voice, name, 0.0))
                col.knob.set_value(v, notify=False)
                col.value.setText(format_param(name, v))

    def _on_piece_selected(self, piece: str) -> None:
        self._sync_params()

    def _on_param_edit(self, label: str) -> None:
        """面板**即将**写 DrumVoice 时调到这里 —— 撤销点就压在这一刻。

        一次拖动旋钮会发几十个 valueChanged，所以只有连续型参数（旋钮）才按
        "手势"合并：距上次压点超过 ``PARAM_GESTURE_GAP`` 才算新手势。
        开关是离散动作，点几下就该是几步，不合并 —— 合并了用户会觉得
        "我明明关了两次只撤回来一次"。

        合并的只是**压点次数**，不影响**顺序**：这个槽是同步收到的，
        那时面板还没 setattr，快照里一定是改动前的值。
        """
        if label == PARAM_EDIT_LABEL:
            now = time.monotonic()
            if now - getattr(self, "_last_param_push", 0.0) <= PARAM_GESTURE_GAP:
                return
            self._last_param_push = now
        self._commit(label)

    def _on_params_changed(self) -> None:
        """参数变了：请求重渲染。（撤销点已经由 _on_param_edit 压过了。）"""
        self.edited.emit()
        self._update_counts()

    def _on_param_audition(self) -> None:
        self.audition_count += 1
        self._request_audition()

    def _on_param_enabled(self) -> None:
        voice = self.params.voice
        if voice is None:
            return
        # 撤销点不在这里压：面板已经在翻转 enabled 之前压好了
        piece = self.grid.selected_piece
        self.grid.rebuild()
        self._sync_params()
        self._update_counts()
        self.grid.update()
        self.edited.emit()
        self._status(f"「{DRUM_NAMES.get(piece or '', piece or '')}」"
                     f"{'已启用' if voice.enabled else '已静音'}")

    # -- 试听 ------------------------------------------------------------

    def _request_audition(self) -> None:
        """请求试听当前鼓件。

        节流到 ~14 次/秒：拖动旋钮时每次都渲染 + 打断上一发会糊成一串噪音。
        被节流掉的那次**不会丢** —— 定时器到点会补响最后一次，所以"最后一格
        旋钮的听感"永远是对的。
        """
        now = time.monotonic()
        if now - self._last_audition >= AUDITION_MIN_INTERVAL:
            self._play_audition()
        else:
            self._audition_timer.start()

    def _play_audition(self) -> None:
        piece = self.grid.selected_piece
        if self.track is None or piece is None:
            return
        self._last_audition = time.monotonic()
        played, why = play_piece_result(self.track, piece, piece_velocity(piece))
        if not played:
            # 参数面板上的每一次改动都会走到这里 —— 不报的话，用户会以为是
            # "这个旋钮调了没效果"，而不是"声音引擎出错了"
            text = audition_failure(piece, why)
            self._set_hint(text)
            self._status(text, 5000)

    # -- 鼓型 ------------------------------------------------------------

    def apply_pattern(self, name: str) -> int:
        """把某个鼓型写进**播放头所在小节**。

        返回写进去的音符数（0 表示没写成功）。写了就一定会进 ``track.notes`` ——
        界面上的格子只是工程数据的投影，不做"看起来铺上了"的假实现。

        取舍：只写**启用的**鼓件。被静音的鼓件是用户刻意关掉的，替他打开更粗暴；
        跳过时会在提示条里说清楚跳过了哪几件。
        """
        pattern = next((p for p in PATTERNS if p[0] == name), None)
        if pattern is None or self.track is None:
            self._set_hint("先选一条鼓轨再铺鼓型")
            return 0

        _, _desc, spec = pattern
        spb = max(1, self.project.steps_per_bar)
        bar = self.grid.current_bar()
        base = bar * spb
        self._commit("铺鼓型")

        self.track.ensure_kit()
        touched = set(spec.keys())
        # 只清掉本小节里、这个鼓型会碰的那些鼓件 —— 别的鼓件（比如手写的过门）
        # 保持原样，"铺鼓型"才不会变成"删掉我刚写的东西"。
        self.track.notes = [
            n for n in self.track.notes
            if not (base <= n.step < base + spb and pitch_to_drum(n.pitch) in touched)
        ]

        added = 0
        skipped: list[str] = []
        for piece, positions in spec.items():
            voice = self.track.kit.get(piece)
            if voice is not None and not voice.enabled:
                skipped.append(DRUM_NAMES.get(piece, piece))
                continue
            pitch = drum_to_pitch(piece)
            seen: set[int] = set()
            for pos in positions:
                # 鼓型按"一小节 16 个位置"描述 —— 映射到真实步号
                offset = int(round(pos / 16.0 * spb))
                step = base + min(spb - 1, max(0, offset))
                if step in seen:
                    continue
                seen.add(step)
                if self.track.note_at(step, pitch) is not None:
                    continue
                self.track.add_note(Note(step=step, pitch=pitch, length=1,
                                         velocity=pattern_velocity(piece, pos)))
                added += 1

        msg = f"「{name}」已铺到第 {bar + 1} 小节（{added} 个鼓点）"
        if skipped:
            msg += f" · 跳过静音件：{'、'.join(skipped)}"
        self._set_hint(msg)
        self._status(msg, 4000)
        self.grid.rebuild()
        self._sync_params()
        self._update_counts()
        self.edited.emit()
        return added

    def apply_sound_pack(self, key: str) -> list[str]:
        """把音色包套到当前鼓轨上。返回真的被改动的鼓件名。

        为什么要有个按钮而不是让用户自己拧旋钮：风格是**一整套**东西 ——
        808 底鼓必须配长衰减、牛铃必须落在窄带通的中心，单独调任何一个都不成调。
        套包之后新鼓件会作为新行出现在网格里（``KIT_ORDER`` 里排着它们），
        所以"套上了"这件事是**看得见**的，不是只改了内存里的参数。
        """
        from app.audio import soundpacks

        pack = soundpacks.pack_by_key(key)
        if pack is None:
            self._set_hint(f"没有叫「{key}」的音色包")
            return []
        if self.track is None or self.track.kind != "drum":
            self._set_hint("先选一条鼓轨再套音色包")
            self._status("套音色包需要一个鼓轨", 3000)
            return []

        self._commit(f"音色包·{pack.name}")
        applied = soundpacks.apply_pack(self.track, pack)
        # 套完立刻能听见：鼓件的参数变了，试听循环下一拍就是新音色
        self.grid.rebuild()
        self._sync_params()
        self._update_counts()
        names = "、".join(DRUM_NAMES.get(p, p) for p in applied)
        msg = f"音色包「{pack.name}」已套上（{names}）· {pack.tip or pack.summary}"
        self._set_hint(msg)
        self._status(f"音色包「{pack.name}」已套上", 4000)
        self.edited.emit()
        return applied

    def clear_current_bar(self) -> int:
        """清空当前小节里这条鼓轨的全部鼓点。返回删掉几个。"""
        if self.track is None:
            return 0
        spb = max(1, self.project.steps_per_bar)
        bar = self.grid.current_bar()
        base = bar * spb
        doomed = [n for n in self.track.notes if base <= n.step < base + spb]
        if not doomed:
            # 本来就空的小节：压了撤销点就是"撤销了但什么都没发生"，
            # 所以只说话、不进撤销栈
            self._set_hint(f"第 {bar + 1} 小节本来就没有鼓点")
            return 0
        self._commit("清空小节")
        self.track.notes = [n for n in self.track.notes
                            if not (base <= n.step < base + spb)]
        self._set_hint(f"已清空第 {bar + 1} 小节的 {len(doomed)} 个鼓点")
        self.grid.update()
        self._update_counts()
        self.edited.emit()
        return len(doomed)

    def create_drum_track(self) -> Track:
        """空状态里那个按钮：真的新建一条鼓轨并切过去。"""
        self._commit("新建鼓组轨道")
        palette = accents.track_palette()
        idx = len(self.project.tracks)
        t = Track(name=f"鼓组 {idx + 1}", kind="drum", icon="drums",
                  color=palette[idx % len(palette)].name(), volume=0.9)
        t.kit = drum_engine.default_voices()
        self.project.add_track(t)
        self.rebuild()
        self.set_track(t)
        ctrl = self._controller
        if ctrl is not None and hasattr(ctrl, "touch"):
            try:
                ctrl.touch(structural=True)
            except Exception:
                pass
        self.edited.emit()
        self._status(f"已新建「{t.name}」", 3000)
        return t

    # -- 其它 ------------------------------------------------------------

    def _on_notes_changed(self) -> None:
        self._update_counts()
        self.edited.emit()

    def _update_counts(self) -> None:
        if self.track is None:
            self._count.setText("没有鼓轨")
            self.pattern_hint.setText("")
            return
        total = len(self.track.notes)
        rows = len(self.grid.rows)
        bar = self.grid.current_bar() + 1
        self._count.setText(f"{self.track.name} · {total} 个鼓点 · {rows} 件启用的鼓")
        self.pattern_hint.setText(f"鼓型写入第 {bar} 小节")

    def _zoom(self, factor: float) -> None:
        """按倍数缩放网格。

        基准取 ``_zoom_level``，而它由 ``grid.stepWidthChanged`` 跟着真实步宽走 ——
        所以不管上一次是点按钮还是 Ctrl+滚轮，这里都是从**当前**宽度往上/下算，
        不会再出现"点放大反而变小"。
        """
        self._zoom_level = max(0.4, min(2.2, self._zoom_level * factor))
        self.grid.set_step_width(DEFAULT_STEP_W * self._zoom_level)

    def _on_step_width_changed(self, px: float) -> None:
        """网格步宽变了（滚轮/按钮）—— 把倍数读数重算一遍。

        只做"读数的镜像"，不反向去改按钮值：步宽永远以 grid.step_w 为准。
        """
        self._zoom_level = px / DEFAULT_STEP_W


def add_drum_track(project: Project) -> Track:
    """在不依赖页面的场合新建一条鼓轨（页面内那个按钮走的是实例方法）。"""
    palette = accents.track_palette()
    idx = len(project.tracks)
    t = Track(name=f"鼓组 {idx + 1}", kind="drum", icon="drums",
              color=palette[idx % len(palette)].name(), volume=0.9)
    t.kit = drum_engine.default_voices()
    project.add_track(t)
    return t


__all__ = [
    "DrumPage", "DrumGrid", "DrumParamsPanel", "PATTERNS", "PATTERN_NAMES",
    "render_piece", "play_piece", "pattern_velocity", "format_param",
    "add_drum_track", "piece_icon", "piece_velocity",
    "render_piece_result", "play_piece_result", "audition_failure",
]
