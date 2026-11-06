"""走带条 —— 播放控制 + 波形 + 电平表 + 渲染进度。

波形不是装饰：它是**可点击定位的**。而且左右声道分开画（用两种明度叠在一起），
这样声像处理的效果一眼可见 —— 单声道的轨道看起来是一条，铺底那种宽立体声
则是两条错开。
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen,
)
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from app import icons, theme
from app.audio import render as audio_render
from app.audio.player import PlayerState
from app.theme import accents, labels, materials, radii, spacing, type_scale
from app.widgets.glass import (
    GlassDivider, GlassIconButton, GlassImageButton, GlassPanel, GlassStyle,
    paint_glass, paint_mini_specular,
)


# ==========================================================================
# 波形
# ==========================================================================


class WaveformView(QWidget):
    """音频波形 + 播放头 + 循环区间，支持点击/拖动定位。"""

    seekRequested = Signal(float)   # 秒

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._envelope: np.ndarray | None = None
        self._env_width = 0
        self._duration = 0.0
        self._position = 0.0
        self._loop: tuple[float, float] | None = None
        self._dragging = False
        self._accent = accents.blue

    # -- 数据 ------------------------------------------------------------

    def set_audio(self, stereo: np.ndarray | None, duration: float) -> None:
        self._duration = max(1e-6, float(duration))
        self._envelope = None           # 让下次绘制重新分桶
        self._env_width = 0
        if stereo is not None and stereo.size:
            self._audio = stereo
        else:
            self._audio = None
        self.update()

    def set_position(self, seconds: float) -> None:
        if abs(seconds - self._position) < 0.004:
            return
        self._position = float(seconds)
        self.update()

    def set_loop(self, span: tuple[float, float] | None) -> None:
        self._loop = span
        self.update()

    def set_accent(self, color: QColor) -> None:
        self._accent = color
        self.update()

    # -- 交互 ------------------------------------------------------------

    def _time_at(self, x: float) -> float:
        if self.width() <= 0:
            return 0.0
        return float(np.clip(x / self.width(), 0.0, 1.0)) * self._duration

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.seekRequested.emit(self._time_at(event.position().x()))

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.seekRequested.emit(self._time_at(event.position().x()))

    def mouseReleaseEvent(self, event):
        self._dragging = False

    # -- 绘制 ------------------------------------------------------------

    def _ensure_envelope(self) -> np.ndarray | None:
        audio = getattr(self, "_audio", None)
        if audio is None or audio.size == 0:
            return None
        buckets = max(16, min(2000, self.width()))
        if self._envelope is None or self._env_width != buckets:
            self._envelope = audio_render.waveform_envelope(audio, buckets)
            self._env_width = buckets
        return self._envelope

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect())
        # 背景：比走带条本体略深一点，形成"凹槽"的层次
        paint_glass(painter, rect, GlassStyle(
            material=theme.black(0.18), radius=radii.control,
            specular=0.0, border=0.6, inner_gradient=0.0, grain=0.5,
        ))

        mid_y = rect.center().y()
        half = rect.height() * 0.5 - 6.0

        # 中点线
        painter.setPen(QPen(theme.white(0.10), 1.0))
        painter.drawLine(QPointF(rect.left() + 4, mid_y), QPointF(rect.right() - 4, mid_y))

        # 循环区间高亮
        if self._loop is not None:
            lo, hi = self._loop
            if hi > lo:
                x0 = rect.left() + (lo / self._duration) * rect.width()
                x1 = rect.left() + (hi / self._duration) * rect.width()
                band = QRectF(x0, rect.top() + 2, max(1.0, x1 - x0), rect.height() - 4)
                c = QColor(self._accent)
                c.setAlphaF(0.13)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(c)
                painter.drawRoundedRect(band, 4, 4)

        env = self._ensure_envelope()
        if env is not None and env.shape[0] > 0:
            n = env.shape[0]
            step = rect.width() / n
            # 已播放部分用强调色，未播放用中性色 —— 进度一目了然
            played_x = rect.left() + (self._position / self._duration) * rect.width()

            played = QColor(self._accent)
            played.setAlphaF(0.95)
            upcoming = QColor(theme.tw("neutral_300"))
            upcoming.setAlphaF(0.42)

            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(n):
                x = rect.left() + i * step
                top = mid_y - env[i, 1] * half
                bot = mid_y - env[i, 0] * half
                h = max(1.2, bot - top)
                painter.setBrush(played if x <= played_x else upcoming)
                painter.drawRect(QRectF(x, top, max(1.0, step * 0.85), h))

        # 播放头
        px = rect.left() + (self._position / self._duration) * rect.width()
        if rect.left() <= px <= rect.right():
            g = QLinearGradient(px, rect.top(), px, rect.bottom())
            g.setColorAt(0.0, theme.white(0.15))
            g.setColorAt(0.5, theme.white(0.98))
            g.setColorAt(1.0, theme.white(0.15))
            painter.setPen(QPen(QBrush(g), 1.6))
            painter.drawLine(QPointF(px, rect.top() + 2), QPointF(px, rect.bottom() - 2))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.white(0.98))
            painter.drawEllipse(QPointF(px, rect.top() + 3.0), 2.6, 2.6)

        # 无音频时给个提示
        if env is None:
            painter.setFont(type_scale.footnote.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "渲染后显示波形")

        painter.end()


# ==========================================================================
# 电平表
# ==========================================================================


class LevelMeter(QWidget):
    """横向电平表。峰值瞬时回落、有效值平滑跟随 —— 和硬件表头的观感一致。"""

    def __init__(self, parent: QWidget | None = None, *, width: int = 130):
        super().__init__(parent)
        self.setFixedSize(width, 8)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._peak_db = -120.0
        self._rms_db = -120.0
        self._hold_db = -120.0
        self._hold_frames = 0

    def set_levels(self, peak_db: float, rms_db: float) -> None:
        # 峰值保持：好看且实用（能看出刚才有没有接近过 0dB）
        if peak_db > self._hold_db:
            self._hold_db = peak_db
            self._hold_frames = 30
        elif self._hold_frames > 0:
            self._hold_frames -= 1
        else:
            self._hold_db = max(peak_db, self._hold_db - 0.8)

        self._peak_db = peak_db
        self._rms_db = rms_db
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.black(0.30))
        painter.drawRoundedRect(rect, 3, 3)

        def frac(db: float) -> float:
            # -60dB..0dB 映射到 0..1，低于 -60 一律贴底
            return float(np.clip((db + 60.0) / 60.0, 0.0, 1.0))

        # 有效值段：低饱和的底
        rms_w = rect.width() * frac(self._rms_db)
        if rms_w > 0.5:
            g = QLinearGradient(rect.left(), 0, rect.right(), 0)
            g.setColorAt(0.0, theme.tw("green"))
            g.setColorAt(0.72, theme.tw("yellow"))
            g.setColorAt(0.92, theme.tw("orange"))
            g.setColorAt(1.0, theme.tw("red"))
            painter.setBrush(QBrush(g))
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(), rms_w, rect.height()), 3, 3)

        # 峰值线
        peak_x = rect.left() + rect.width() * frac(self._peak_db)
        if self._peak_db > -59.5:
            painter.setBrush(theme.white(0.85))
            painter.drawRect(QRectF(peak_x - 1.0, rect.y(), 2.0, rect.height()))

        # 保持标记
        if self._hold_db > -59.5:
            hx = rect.left() + rect.width() * frac(self._hold_db)
            painter.setBrush(theme.white(0.95))
            painter.drawRect(QRectF(hx - 0.75, rect.y(), 1.5, rect.height()))
        painter.end()


# ==========================================================================
# 走带条
# ==========================================================================


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:d}:{s:04.1f}"


class TransportBar(GlassPanel):
    """底部走带条。"""

    playToggled = Signal()
    stopRequested = Signal()
    loopToggled = Signal(bool)
    seekRequested = Signal(float)
    exportRequested = Signal()
    renderRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, material=theme.black(0.28), radius=0.0, liquid=True)
        self.setFixedHeight(96)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._state = PlayerState()
        self._bpm = 120.0
        self._steps_per_bar = 16
        self._seconds_per_step = 0.125
        self._loop = False
        self._rendering = False
        self._render_frac = 0.0
        self._render_msg = ""
        self._status = ""
        # 状态消息按设定时长自动消失，不需要外部清理
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self.clear_status)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(spacing.md, spacing.xs, spacing.md, spacing.sm)
        lay.setSpacing(spacing.xs)

        # ---- 第一行：传输控制 -----------------------------------------
        top = QHBoxLayout()
        top.setSpacing(spacing.xs)

        self.btn_stop = GlassImageButton("stop", self, size=32, tooltip="停止")
        self.btn_play = GlassImageButton("play", self, size=38, tooltip="播放 / 暂停",
                                         accent=accents.green, style="tinted")
        self.btn_loop = GlassImageButton("loop", self, size=32, tooltip="循环播放",
                                         accent=accents.blue)
        self.btn_render = GlassImageButton("refresh", self, size=32, tooltip="重新渲染")
        self.btn_export = GlassImageButton("save", self, size=32, tooltip="导出音频 / MIDI",
                                           accent=accents.green)

        self.btn_stop.clicked.connect(self.stopRequested)
        self.btn_play.clicked.connect(self.playToggled)
        self.btn_render.clicked.connect(self.renderRequested)
        self.btn_export.clicked.connect(self.exportRequested)
        self.btn_loop.clicked.connect(lambda: self.loopToggled.emit(not self._loop))

        for b in (self.btn_stop, self.btn_play, self.btn_loop):
            top.addWidget(b)
        top.addSpacing(spacing.sm)

        self.waveform = WaveformView(self)
        self.waveform.seekRequested.connect(self.seekRequested)
        top.addWidget(self.waveform, 1)
        top.addSpacing(spacing.sm)

        self.btn_render.setParent(self)
        self.btn_export.setParent(self)
        top.addWidget(self.btn_render)
        top.addWidget(self.btn_export)

        lay.addLayout(top)

        # ---- 第二行：数值读出 -----------------------------------------
        bottom = QHBoxLayout()
        bottom.setSpacing(spacing.md)
        self._bottom = bottom
        lay.addLayout(bottom)

        self.meter = LevelMeter(self, width=150)
        bottom.addWidget(self.meter)
        bottom.addStretch(1)

        self._readout_font = type_scale.mono.font()

    # -- 数值字体 --------------------------------------------------------

    def _mono(self, size_delta: float = 0.0, weight: int = QFont.Weight.Medium) -> QFont:
        from app.theme import resolve_mono_font

        f = resolve_mono_font()
        f.setPointSizeF(type_scale.mono.size + size_delta)
        f.setWeight(QFont.Weight(weight))
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)
        return f

    # -- 外部设置 --------------------------------------------------------

    def set_project_info(self, bpm: float, steps_per_bar: int, seconds_per_step: float,
                         loop_range: tuple[int, int] | None = None) -> None:
        self._bpm = bpm
        self._steps_per_bar = steps_per_bar
        self._seconds_per_step = seconds_per_step
        self._project_loop = loop_range
        self.waveform.set_loop(
            None if loop_range is None else
            (loop_range[0] * seconds_per_step, loop_range[1] * seconds_per_step)
        )
        self.update()

    def set_audio(self, audio, duration: float) -> None:
        self.waveform.set_audio(audio, duration)

    def set_state(self, state: PlayerState) -> None:
        self._state = state
        self.waveform.set_position(state.position)
        self.meter.set_levels(state.peak_db, state.rms_db)
        self.btn_play.set_icon("pause" if state.playing else "play")
        self.btn_play.setToolTip("暂停" if state.playing else "播放")
        self.update()

    def set_loop(self, enabled: bool) -> None:
        self._loop = enabled
        self.btn_loop.set_style_kind("tinted" if enabled else "plain")
        self.update()

    def set_rendering(self, rendering: bool, frac: float = 0.0, message: str = "") -> None:
        self._rendering = rendering
        self._render_frac = frac
        self._render_msg = message
        self.update()

    def set_status(self, text: str, duration_ms: int = 4000) -> None:
        """显示一条临时状态消息。

        ``duration_ms`` 是必须的：这里也是 :attr:`AppController.statusMessage`
        信号的槽，而那个信号带 (文字, 毫秒) 两个参数 —— 签名对不上时 PySide6
        会抛 TypeError，但异常被 Qt 吞掉，表现为"提示莫名其妙不出现"。
        """
        self._status = text
        if duration_ms > 0:
            self._status_timer.start(int(duration_ms))
        else:
            self._status_timer.stop()
        self.update()

    def clear_status(self) -> None:
        self._status = ""
        self.update()

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        y = self.height() - 22
        x = spacing.md

        # 位置：小节.拍.步 + 时间
        step = self._state.position / max(1e-6, self._seconds_per_step)
        bar = int(step // self._steps_per_bar) + 1
        beat = int((step % self._steps_per_bar) // 4) + 1
        sub = int(step % 4) + 1

        painter.setFont(self._mono(3.0, QFont.Weight.DemiBold))
        painter.setPen(QPen(labels.label))
        pos_text = f"{bar:02d}.{beat}.{sub}"
        painter.drawText(QRectF(x, y, 88, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, pos_text)
        x += 92

        painter.setFont(self._mono(0.0))
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(QRectF(x, y, 120, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         f"{_fmt_time(self._state.position)} / {_fmt_time(self._state.duration)}")
        x += 128

        # BPM
        painter.setPen(QPen(labels.secondary))
        painter.drawText(QRectF(x, y, 96, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         f"{self._bpm:g} BPM")
        x += 104

        # 渲染进度 / 状态消息
        if self._rendering:
            frac = float(np.clip(self._render_frac, 0.0, 1.0))
            bar_rect = QRectF(x, y + 5, 220, 6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.white(0.12))
            painter.drawRoundedRect(bar_rect, 3, 3)
            c = QColor(accents.blue)
            painter.setBrush(c)
            painter.drawRoundedRect(QRectF(bar_rect.x(), bar_rect.y(),
                                           max(4.0, bar_rect.width() * frac), bar_rect.height()), 3, 3)
            painter.setFont(type_scale.footnote.font())
            painter.setPen(QPen(labels.secondary))
            painter.drawText(QRectF(x + 232, y, 300, 18),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             f"渲染中 {frac * 100:.0f}% · {self._render_msg}")
        elif self._status:
            painter.setFont(type_scale.footnote.font())
            painter.setPen(QPen(labels.tertiary))
            painter.drawText(QRectF(x, y, 460, 18),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             self._status)

        painter.end()


__all__ = ["TransportBar", "WaveformView", "LevelMeter"]
