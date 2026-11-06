"""总览页 —— 打开软件第一眼看到的东西。

信息层级
--------
1. **工程头**：只放最关键的几个数（速度、小节、时长、音符数），大字号、负字距。
2. **轨道列表**：每条轨道一行，左边是身份（颜色 + 图标 + 名字），
   中间是内容一览（迷你音符图），右边是混音控制（音量/声像/静音/独奏）。
3. **母带卡**：总线电平、限幅阈值、混响与延迟的返回量。

迷你音符图不是缩略图而是**按真实音符位置压扁画的**，所以一眼就能看出
这条轨道是密的还是疏的、哪个小节是空的 —— 这是编曲时最常需要的信息。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import effects as fx
from app.audio.project import DRUM_NAMES, Project, Track, pitch_to_drum
from app.theme import accents, labels, materials, radii, spacing, type_scale
from app.widgets.controls import GlassKnob, GlassSlider
from app.widgets.glass import (
    GlassCard, GlassChip, GlassImageButton, GlassPanel, GlassStyle, paint_glass,
)


# ==========================================================================
# 迷你音符预览
# ==========================================================================


class MiniRoll(QWidget):
    """把一条轨道的音符按时间轴压扁画出来。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._track: Track | None = None
        self._total_steps = 128
        self._steps_per_bar = 16
        self._color = accents.blue

    def set_track(self, track: Track, total_steps: int, steps_per_bar: int, color: QColor) -> None:
        self._track = track
        self._total_steps = max(1, total_steps)
        self._steps_per_bar = max(1, steps_per_bar)
        self._color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())

        paint_glass(painter, rect, GlassStyle(
            material=theme.black(0.20), radius=radii.chip, specular=0.0,
            border=0.5, inner_gradient=0.0, grain=0.4,
        ))
        inner = rect.adjusted(3, 4, -3, -4)

        # 小节线 —— 极淡，只作为参考网格
        bar_w = inner.width() * self._steps_per_bar / self._total_steps
        painter.setPen(QPen(theme.white(0.055), 1.0))
        x = inner.left() + bar_w
        while x < inner.right() - 0.5:
            painter.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
            x += bar_w

        if self._track is None or not self._track.notes:
            painter.setFont(type_scale.caption2.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "空")
            painter.end()
            return

        is_drum = self._track.kind == "drum"
        if is_drum:
            pieces = sorted({pitch_to_drum(n.pitch) for n in self._track.notes})
            lane_of = {p: i for i, p in enumerate(pieces)}
            lanes = max(1, len(pieces))
        else:
            pitches = [n.pitch for n in self._track.notes]
            lo, hi = min(pitches), max(pitches)
            lane_of = None
            lanes = max(3, hi - lo + 1)
            pitch_lo = lo

        lane_h = max(1.4, inner.height() / lanes)
        step_w = inner.width() / self._total_steps

        for note in self._track.notes:
            if is_drum:
                lane = lane_of.get(pitch_to_drum(note.pitch), 0)
            else:
                lane = lanes - 1 - (note.pitch - pitch_lo)
            y = inner.top() + lane * lane_h
            x0 = inner.left() + note.step * step_w
            w = max(1.2, note.length * step_w - 0.6)
            h = max(1.4, lane_h - 1.0)

            c = QColor(self._color)
            c.setAlphaF(0.35 + 0.62 * float(np.clip(note.velocity, 0, 1)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawRoundedRect(QRectF(x0, y, w, h), min(1.6, h / 2.0), min(1.6, h / 2.0))

        painter.end()


# ==========================================================================
# 轨道行
# ==========================================================================


class TrackStrip(GlassCard):
    """一条轨道的行。"""

    changed = Signal()
    selectRequested = Signal(object)
    deleteRequested = Signal(object)

    HEIGHT = 76

    def __init__(self, track: Track, project: Project, index: int,
                 parent: QWidget | None = None):
        super().__init__(parent, radius=radii.card, liquid=True,
                         material=theme.black(0.30))
        self.track = track
        self.project = project
        self.index = index
        self._color = QColor(track.color)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_accent(self._color)

        # ---- 左侧：身份 -----------------------------------------------
        self.btn_icon = GlassImageButton(
            track.icon, self, size=36, icon_size=24, style="tinted",
            accent=self._color, tooltip=f"{track.name}（点击试听）",
        )
        self.btn_icon.move(12, (self.HEIGHT - 36) // 2)

        # ---- 中间：迷你音符图 -----------------------------------------
        self.mini = MiniRoll(self)
        self.mini.set_track(track, project.total_steps, project.steps_per_bar, self._color)

        # ---- 右侧：混音控制 -------------------------------------------
        self.vol = GlassSlider(self, 0.0, 1.5, track.volume, accent=self._color)
        self.vol.valueChanged.connect(self._on_volume)

        self.pan = GlassKnob(self, -1.0, 1.0, track.pan, accent=accents.teal, size=32)
        self.pan.valueChanged.connect(self._on_pan)

        self.btn_mute = GlassImageButton("speaker_mute", self, size=26, icon_size=18,
                                         style="plain", tooltip="静音")
        self.btn_solo = GlassImageButton("headphones", self, size=26, icon_size=18,
                                         style="plain", tooltip="独奏")
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.btn_solo.clicked.connect(self._toggle_solo)
        self._sync_mute_solo()

        self.btn_delete = GlassImageButton("delete", self, size=26, icon_size=16,
                                           style="plain", tooltip="删除轨道")
        self.btn_delete.clicked.connect(lambda: self.deleteRequested.emit(self.track))

        self._layout_children()

    # -- 布局（手写定位，因为要在定高行里做四段式排布）-------------------

    #: 左侧身份区宽度（色条 + 图标 + 名称列）
    ID_W = 206
    #: 右侧混音控制区宽度
    CTRL_W = 244

    def _layout_children(self) -> None:
        h = self.HEIGHT
        w = self.width()

        # 迷你音符图：夹在身份区与控制区之间
        mini_x = self.ID_W + spacing.xs
        mini_w = max(60, w - mini_x - self.CTRL_W - spacing.sm)
        self.mini.setGeometry(mini_x, (h - 34) // 2, mini_w, 34)

        rx = w - self.CTRL_W - spacing.xs
        btn = 26
        self.btn_mute.move(rx, 15)
        self.btn_solo.move(rx + btn + 4, 15)
        self.pan.move(rx + 64, (h - 34) // 2)
        self.vol.setGeometry(rx + 104, 37, max(60, w - 46 - (rx + 104)), 22)
        self.btn_delete.move(w - btn - 10, 15)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_children()

    # -- 状态同步 --------------------------------------------------------

    def _sync_mute_solo(self) -> None:
        """未激活时把图标压暗 —— 否则静音/独奏的"开着"和"关着"长得一样，
        用户根本看不出当前状态。"""
        self.btn_mute.set_style_kind("destructive" if self.track.mute else "plain")
        self.btn_mute.set_dimmed(not self.track.mute)
        self.btn_solo.set_accent(accents.yellow)
        self.btn_solo.set_style_kind("tinted" if self.track.solo else "plain")
        self.btn_solo.set_dimmed(not self.track.solo)

    def _on_volume(self, v: float) -> None:
        self.track.volume = float(v)
        self.changed.emit()

    def _on_pan(self, v: float) -> None:
        self.track.pan = float(v)
        self.changed.emit()

    def _toggle_mute(self) -> None:
        self.track.mute = not self.track.mute
        self._sync_mute_solo()
        self.changed.emit()

    def _toggle_solo(self) -> None:
        self.track.solo = not self.track.solo
        self._sync_mute_solo()
        self.changed.emit()

    def refresh(self) -> None:
        self.vol.set_value(self.track.volume)
        self.pan.set_value(self.track.pan)
        self._sync_mute_solo()
        self.mini.set_track(self.track, self.project.total_steps,
                            self.project.steps_per_bar, self._color)
        self.update()

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 左侧色条：轨道身份的第一识别信号（比文字快得多）
        bar = QRectF(4.0, 12.0, 3.0, self.height() - 24.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(bar, 1.5, 1.5)

        # ---- 名称列（独立占位，绝不与迷你音符图重叠）------------------
        col_x = 58.0
        col_w = self.ID_W - col_x - 8.0

        f = type_scale.subhead.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        fm = QFontMetricsF(f)
        painter.setPen(QPen(labels.label))

        kind_text = "鼓机" if self.track.kind == "drum" else _wave_label(self.track)
        kind_font = type_scale.caption2.font()
        kind_w = QFontMetricsF(kind_font).horizontalAdvance(kind_text) + 10

        # 名称过长时省略，给类型徽章留位
        name_w = max(30.0, col_w - kind_w)
        elided = fm.elidedText(self.track.name, Qt.TextElideMode.ElideRight, name_w)
        painter.drawText(QRectF(col_x, 13, name_w, 17),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        # 类型徽章：小圆角底 + 轨道色文字
        badge = QRectF(col_x + name_w + 2, 15, kind_w, 14)
        tint = QColor(self._color)
        tint.setAlphaF(0.20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tint)
        painter.drawRoundedRect(badge, 4, 4)
        painter.setFont(kind_font)
        painter.setPen(QPen(self._color.lighter(145)))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, kind_text)

        # 第二行：统计
        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(QRectF(col_x, 33, col_w, 15),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         f"{len(self.track.notes)} 音符"
                         + (f" · {len(self.track.effects)} 效果" if self.track.effects else ""))

        # ---- 控制区小标签 --------------------------------------------
        rx = self.width() - self.CTRL_W - spacing.xs
        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(labels.quaternary))
        painter.drawText(QRectF(rx + 104, 21, 80, 14),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "音量")
        painter.drawText(QRectF(rx + 62, 12, 40, 14),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "声像")
        painter.end()


def _wave_label(track: Track) -> str:
    return {
        "sine": "正弦", "triangle": "三角", "saw": "锯齿",
        "square": "方波", "organ": "风琴", "noise": "噪声",
    }.get(track.synth.wave, track.synth.wave)


# ==========================================================================
# 工程头
# ==========================================================================


class ProjectHeader(GlassPanel):
    """工程概览卡。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, material=theme.black(0.16), radius=radii.panel, liquid=True)
        self.setFixedHeight(78)
        self._name = ""
        self._values: list[tuple[str, str]] = []

    def set_project(self, project: Project) -> None:
        self._name = project.name
        notes = sum(len(t.notes) for t in project.tracks)
        dur = project.duration_seconds(extra_tail=0.0)
        self._values = [
            ("速度", f"{project.bpm:g}"),
            ("小节", f"{project.bars}"),
            ("轨道", f"{len(project.tracks)}"),
            ("音符", f"{notes}"),
            ("时长", f"{int(dur // 60)}:{dur % 60:04.1f}"),
        ]
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # 乐谱图标
        pm = icons.pixmap("score", 38, dpr=self.devicePixelRatioF())
        painter.drawPixmap(18, (self.height() - 38) // 2, pm)

        x = 18 + 38 + 14
        # 工程名：大字号 + 负字距（排版规则：字越大字距越紧）
        f = type_scale.large_title.font()
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        fm = QFontMetricsF(f)
        painter.drawText(QRectF(x, 12, max(120, self.width() - x - 400), 30),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._name)

        # 数值行（只放速度与小节，避免与右侧大号数值重复）
        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(QRectF(x, 42, 400, 16),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         " · ".join(f"{k} {v}" for k, v in self._values[:2]))

        # 右侧：大号数值组。列数按实际条目数算 —— 写死列数会在中间留下大空档
        vals = self._values[2:]
        if not vals:
            painter.end()
            return
        rw = 320.0
        rx = self.width() - rw - 20
        cw = rw / len(vals)
        for i, (k, v) in enumerate(vals):
            cx = rx + i * cw
            fv = type_scale.title2.font()
            painter.setFont(fv)
            painter.setPen(QPen(labels.label))
            painter.drawText(QRectF(cx, 18, cw - 10, 22),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, v)
            painter.setFont(type_scale.caption2.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(QRectF(cx, 40, cw - 10, 14),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, k)
        painter.end()


# ==========================================================================
# 母带卡
# ==========================================================================


class MasterCard(GlassCard):
    """总线控制。"""

    changed = Signal()

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent, radius=radii.card, liquid=True,
                         material=theme.black(0.26))
        self.project = project
        self.setFixedHeight(84)
        self.set_accent(accents.orange)

        m = project.master
        self.vol = GlassSlider(self, 0.0, 1.5, m.volume, accent=accents.orange, height=24)
        self.reverb = GlassSlider(self, 0.0, 2.0, float(m.reverb.params.get("size", 0.6)),
                                  accent=accents.purple, height=24)
        self.delay = GlassSlider(self, 0.0, 0.9, float(m.delay.params.get("feedback", 0.35)),
                                 accent=accents.teal, height=24)
        self.limiter = GlassSlider(self, -12.0, 0.0, m.limiter_threshold,
                                   accent=accents.red, height=24)

        self.vol.valueChanged.connect(self._on_vol)
        self.reverb.valueChanged.connect(self._on_reverb)
        self.delay.valueChanged.connect(self._on_delay)
        self.limiter.valueChanged.connect(self._on_limiter)

    def _on_vol(self, v):
        self.project.master.volume = float(v)
        self.changed.emit()

    def _on_reverb(self, v):
        self.project.master.reverb.params["size"] = float(v)
        self.changed.emit()

    def _on_delay(self, v):
        self.project.master.delay.params["feedback"] = float(v)
        self.changed.emit()

    def _on_limiter(self, v):
        self.project.master.limiter_threshold = float(v)
        self.changed.emit()

    def _layout_children(self) -> None:
        w = self.width()
        col = max(120, (w - 150) / 4.0)
        x0 = 146
        for i, (slider, _) in enumerate(((self.vol, 0), (self.reverb, 1),
                                         (self.delay, 2), (self.limiter, 3))):
            x = x0 + i * col
            slider.setGeometry(int(x), 44, int(col - 22), 24)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_children()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        pm = icons.pixmap("control_knobs", 34, dpr=self.devicePixelRatioF())
        painter.drawPixmap(16, (self.height() - 34) // 2, pm)

        f = type_scale.subhead.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        painter.drawText(QRectF(58, 14, 90, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "母带")

        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(QRectF(58, 32, 90, 14),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "总线")

        w = self.width()
        col = max(120, (w - 150) / 4.0)
        x0 = 146
        labels_row = (
            ("总线音量", f"{self.project.master.volume:.2f}"),
            ("混响空间", f"{float(self.project.master.reverb.params.get('size', 0.6)):.2f}"),
            ("延迟反馈", f"{float(self.project.master.delay.params.get('feedback', 0.35)):.2f}"),
            ("限幅阈值", f"{self.project.master.limiter_threshold:+.1f} dB"),
        )
        for i, (name, value) in enumerate(labels_row):
            x = x0 + i * col
            painter.setFont(type_scale.caption1.font())
            painter.setPen(QPen(labels.secondary))
            painter.drawText(QRectF(x, 20, col - 22, 14),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
            painter.setFont(type_scale.caption2.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(QRectF(x, 20, col - 22, 14),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, value)
        painter.end()


# ==========================================================================
# 页面
# ==========================================================================


class OverviewPage(QWidget):
    """总览页。

    .. note:: 本页写在 :doc:`docs/MODULE_CONTRACT` 之前，信号名与契约不一致。
       这里保留了原有的 `projectEdited`（改动它要牵动一堆连接），另外**补上**
       契约要求的 `edited` / `seekRequested`，并在内部把前者转发给后者 ——
       于是集成层可以一视同仁地处理全部 7 个页面，不需要为总览页开特例。
    """

    projectEdited = Signal()
    trackSelected = Signal(object)
    trackDeleted = Signal(object)
    addTrackRequested = Signal(str)

    #: 契约要求：用户改了工程内容，需要重渲染
    edited = Signal()
    #: 契约要求：请求把播放头移到某个时间。总览页没有播放头，此信号只为接口一致而存在。
    seekRequested = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._strips: list[TrackStrip] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)

        self.header = ProjectHeader(self)
        root.addWidget(self.header)

        # 工具行
        tools = QHBoxLayout()
        tools.setSpacing(spacing.xs)
        self._tools = tools
        tools.addStretch(1)
        self.btn_add_synth = GlassChip("＋ 合成器轨道", self, checkable=False, accent=accents.blue)
        self.btn_add_drum = GlassChip("＋ 鼓组轨道", self, checkable=False, accent=accents.orange)
        self.btn_add_synth.clicked.connect(lambda: self.addTrackRequested.emit("synth"))
        self.btn_add_drum.clicked.connect(lambda: self.addTrackRequested.emit("drum"))
        tools.addWidget(self.btn_add_synth)
        tools.addWidget(self.btn_add_drum)
        root.addLayout(tools)

        # 滚动区
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                                  "QScrollBar:vertical{width:8px;background:transparent;}"
                                  "QScrollBar::handle:vertical{background:rgba(255,255,255,40);"
                                  "border-radius:4px;min-height:30px;}"
                                  "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")

        self.list_host = QWidget()
        self.list_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(spacing.xs)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        root.addWidget(self.scroll, 1)

        self.master = MasterCard(project, self)
        self.master.changed.connect(self.projectEdited)
        root.addWidget(self.master)

        # 把历史信号名转发到契约要求的名字上
        self.projectEdited.connect(self.edited)

        self.rebuild()

    # -- 契约接口 --------------------------------------------------------

    def bind(self, controller) -> None:
        """接上控制器。总览页不需要控制器做别的事，留着是为了接口统一。"""
        self._controller = controller

    def set_project(self, project: Project) -> None:
        """整个工程被换掉（打开新工程 / 撤销）时调用。

        **两个地方都要换**：页面自己持有一份引用，母带卡也持有一份。
        早先只换了页面这一份，母带卡会继续写进旧工程 —— 这正是
        `MainWindow._open_project` 里那两行手工修补的由来。
        """
        self.project = project
        self.master.project = project
        self.rebuild()

    def refresh_notes(self) -> None:
        """只是音符/参数变了，刷新数值即可，不必重建列表。"""
        self.refresh_values()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        """总览页没有播放头，因此是空操作 —— 但接口必须在，否则集成层要为它开特例。"""
        return

    # -- 重建 ------------------------------------------------------------

    def rebuild(self) -> None:
        # 清掉旧行
        for strip in self._strips:
            self.list_layout.removeWidget(strip)
            strip.setParent(None)
            strip.deleteLater()
        self._strips.clear()

        for i, track in enumerate(self.project.tracks):
            strip = TrackStrip(track, self.project, i, self.list_host)
            strip.changed.connect(self.projectEdited)
            strip.deleteRequested.connect(self.trackDeleted)
            strip.btn_icon.clicked.connect(lambda t=track: self.trackSelected.emit(t))
            self.list_layout.insertWidget(i, strip)
            self._strips.append(strip)

        self.header.set_project(self.project)
        self.master.update()

    def refresh_values(self) -> None:
        for strip in self._strips:
            strip.refresh()
        self.header.set_project(self.project)
        self.master.update()


__all__ = ["OverviewPage", "TrackStrip", "ProjectHeader", "MasterCard", "MiniRoll"]
