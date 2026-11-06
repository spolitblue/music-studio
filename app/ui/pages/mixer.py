"""混音台 —— 硬件调音台式的竖向通道条。

为什么是竖的
------------
总览页已经有一份"横着排"的混音控制（每轨一行、推子横放）。那个布局适合
**扫一眼比较**各轨参数，但完全无法回答混音时最要紧的问题："哪一轨快爆了、
动态余量还剩多少"。电平表要竖着才有分辨率 —— 这是硬件调音台几十年的结论，
不是审美偏好。所以这一页整条通道条竖排，横向滚动。

通道条自上而下（与硬件一致的操作顺序：先定音色位置，再定送出，最后推电平）::

    图标 + 轨道名
    声像旋钮
    混响发送 / 延迟发送
    竖向推子 + 电平表      ← 占满剩余高度
    静音 / 独奏
    读数

分轨电平怎么来（本模块最需要动脑的地方）
----------------------------------------
只有母带总线能拿到**实时**电平（``PlayerState.peak_db/rms_db`` 来自播放线程），
因为播放时只有一条混好的总线。要显示"每一轨此刻多响"，就必须把每条轨道单独
渲染一遍 —— 那是几百毫秒到一秒的 numpy 运算，塞进 ``paintEvent`` 或每帧定时器
里会直接把界面锁死。

所以方案是**两级**：

1. **静态参考**：工程结构变化（``rebuild``）或用户点"重新测电平"时，对每条轨道
   算一次 ``render.render_track`` 的 RMS/峰值，缓存起来当这条通道条的参考电平。

   这一次性开销**跑在后台线程**（``controller.submit_background``），不在 UI
   线程上。曾经的做法是在 GUI 线程上排队串行跑、两条之间让事件循环喘 16ms ——
   那只是让界面"能先出来、表逐条点亮"，每点亮一条就真僵住 0.2~2.6s
   （实测示范曲 5 轨合计约 5.3s，贵在轨道效果链；``render_track`` 是整块的
   numpy 运算，没法在中间让出）。现在整轮都在后台，UI 线程一次都不会被
   独占超过几十毫秒，结果通过信号回到 UI 线程再落到表上。

   页面**不自己开线程**：线程归属与生命周期由 ``AppController`` 统一收口
   （窗口关闭时要有人在 ``shutdown()`` 里等它收工，页面做不到这件事）。
   页面只负责"要算什么"和"拿到结果怎么画"。

   后台读的是**工程快照**，不是 ``self.project`` —— 用户边拖推子边分析时，
   GUI 线程改的正是那些字段。旧结果的作废靠一个递增的轮次代号（``_round``）。

2. **实时缩放**：推子/声像一动，参考值乘以 ``音量 × 等功率声像增益`` 立刻重画，
   **不重新渲染**。所以拖推子时电平表跟着动，手感是实时的，代价只有几次浮点乘。
   静音（或被别的轨独奏挤掉）时电平表落到表底并压暗 —— 与硬件一致：
   通道都关了，表就不该有读数。

母带条不这么做：它有真正的实时源，直接接 ``controller.transportChanged``；
停止时回落到最近一次整曲渲染的 ``stats.peak_db/rms_db``。

撤销粒度
--------
推子/旋钮在**第一次真正改变数值之前**才压一次撤销点（``editStarted``），
同一笔拖动全程共用 —— 否则拖一次推子会往撤销栈里塞进几十个点，用户按一次
Ctrl+Z 只退一个像素。这与 ``sequencer.py`` 的 ``editStarted`` 是同一套做法。
"""

from __future__ import annotations

import functools
import time

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import dsp, render
from app.audio.project import Project, Track
from app.theme import accents, labels, radii, spacing, type_scale
from app.ui.controller import project_signature
from app.widgets.controls import GlassKnob
from app.widgets.glass import (
    GlassCard, GlassDivider, GlassImageButton, GlassPanel, paint_bloom,
)


# ==========================================================================
# 尺寸常量
# ==========================================================================

FADER_W = 46                # 推子控件宽：槽 + 刻度 + 刻度数字三栏
METER_W = 18
STRIP_W = 108
MASTER_W = 176

HEADER_H = 44               # 通道条头部（自绘：色条 + 图标 + 名字）
PAN_ROW_H = 52              # 声像旋钮 34 + 下面那行"声像"标题 12
SEND_ROW_H = 46
FADER_ROW_MIN_H = 150       # 推子行的最小高度 —— 低于这个值推子就没有分辨率了
TOGGLE_ROW_H = 30
READOUT_H = 18

FADER_MIN = 0.0
FADER_MAX = 1.5
FADER_UNITY = 1.0

METER_FLOOR_DB = -60.0      # 表底。比 -60 更低的都画成"没有"
METER_CEIL_DB = 0.0
ZONE_YELLOW_DB = -12.0      # 进入"注意"区
ZONE_RED_DB = -3.0          # 进入"危险"区

ANALYSIS_GAP_MS = 16        # 兜底路径（没有后台设施时）的排队间隔：两条轨道之间
                            # 让出事件循环这么久。有 controller 时整轮都投后台，
                            # 这个间隔只用来决定"第一个 tick 什么时候起跳"——
                            # 页面构造时控制器可能还没 bind 上来，等一跳让它先接好。
ANALYSIS_MIN_GAP = 2.0      # 秒：两次**自动**重测之间的最小间隔（防抖，见 refresh_notes）

AUDITION_SECONDS = 2.5      # 试听片段长度。再长就变成"听歌"而不是"听一耳朵"了

DOT_SIZE = 20


# ==========================================================================
# 小工具
# ==========================================================================


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _db_text(db: float) -> str:
    """电平数值文本。表底以下一律显示 −∞ —— 那是噪声地板，
    写 -78.3 只会让人以为真有个 -78.3 dB 的信号。"""
    if db <= METER_FLOOR_DB + 1e-9:
        return "−∞"
    return f"{db:+.1f}"


def _audition_excerpt(buf: np.ndarray, seconds: float = AUDITION_SECONDS,
                      sr: int | None = None) -> np.ndarray:
    """从整轨干声里截"最响的那几秒"当试听片段。

    为什么不从头截：鼓轨开头常常是两拍空鼓，铺底要等包络起来才有声音 ——
    从头放会让人以为这条轨道没内容。取能量最高的一段，一按就听到它最有代表性的样子。

    为什么用前缀和，而不是"找最热的 0.25 秒块、再把窗居中"（第一版的做法）：
    块居中只保证那个**块**落在窗里，不保证窗是最响的 —— 最热的块常常本来就靠近
    窗边（第一拍、某个过门），居中之后窗的另一半落回安静的地方。实测（44.1k、
    鼓轨 40 秒）：块居中选出的窗比真正最响的连续 2.5 秒低 1.25dB，其余轨道低
    0.22~0.65dB，而且鼓轨会被推到 0.00s —— 正好是"从头截"那个它想避开的位置。
    长度固定为 n 的连续窗最大值有精确解：对功率做前缀和后作差，argmax 就是答案。
    一次 O(N)、没有启发式参数，比块居中版还便宜（省掉了 reshape 出来的
    (2, frames, hop) 中间数组）。
    """
    sr = int(sr or render.SR)
    # 渲染出来永远是立体声，但本函数是模块级的、测试会单独喂一维缓冲进来
    stereo = buf.reshape(1, -1) if buf.ndim == 1 else buf
    n = min(int(seconds * sr), stereo.shape[1])
    if stereo.shape[1] <= n:
        return buf
    # 功率用 float64 累加：几十万个样本的平方和走 float32 的话，前缀和会把
    # 后几位的差吃掉，而"哪个窗最响"的差距往往就在千分之几。逐声道累加是为了
    # 不再为整轨多开一份 (2, N) 的 float64 副本（长曲子那份是上百 MB）。
    power = np.square(stereo[0], dtype=np.float64)
    for row in stereo[1:]:
        power += np.square(row, dtype=np.float64)
    csum = np.empty(power.shape[0] + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(power, out=csum[1:])
    # win[i] = power[i : i+n] 的和，即"起点为 i 的窗"的能量；取最大者即最响的窗
    win = csum[n:] - csum[:-n]
    start = int(np.argmax(win))
    piece = np.array(stereo[:, start:start + n], dtype=np.float32)
    # 两端各来 10ms 淡入淡出：截断的波形直接播会"啪"一声。
    # 这里没有用 dsp.crossfade_edges —— 它内部按 len(x) 取长度，
    # 而立体的 (2, N) 数组 len 出来是**声道数** 2，它会认为信号太短直接原样返回
    # （第一版就是这么静默失灵的，被测试逮住了）。
    fade = max(1, min(int(0.01 * sr), piece.shape[1] // 8))
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    piece[:, :fade] *= ramp
    piece[:, -fade:] *= ramp[::-1]
    return piece


def _gain_db(volume: float) -> float:
    """线性增益 → dB。0 增益返回表底而不是 -180，免得读数写成 -180 dB。"""
    if volume <= 1e-4:
        return METER_FLOOR_DB
    return max(METER_FLOOR_DB, dsp.gain_to_db(volume))


# ==========================================================================
# 参考电平测量（纯计算，可在任何线程里跑）
# ==========================================================================


def _measure_track(project: Project, track: Track, job=None):
    """渲染一条轨道并算 (峰值 dB, RMS dB, 试听片段, 失败原因)。

    抽成模块级**纯函数**是这套后台方案的关键：它只读传进来的工程、只返回数据，
    不碰页面状态、不碰任何控件 —— 因此后台工作线程能直接用它，测试线程、
    同步兜底路径也能用同一份代码。两边共用同一套口径很重要：电平表的读数
    口径一旦分叉，"分析出的值"和"表上显示的值"就会慢慢不一致。

    失败**不靠抛异常传播**，而是作为第四个返回值交回去：这样"渲染炸了"这条
    信息能跨线程回到 UI 线程，由页面自己决定怎么播报（页面有 ``_failures``
    那套"失败必须可见"的机制，而这个函数根本不知道界面的存在）。

    ``job`` 不为空时，取消检查挂在 ``render_track`` 的**音符回调**上 —— 那是它
    唯一的中断点。注意效果链在全部音符跑完之后才执行，所以"音符很少、效果链
    很贵"的轨道要等效果链结束才能收工（实测最坏 2.6s）。协作式取消本来就是这个
    语义：等在途的那一段跑完（见 HANDOFF 第 ② 条）。
    """
    callback = None
    if job is not None:
        def callback(_done, _total):
            job.raise_if_cancelled()
    try:
        buf = render.render_track(track, project, note_callback=callback)
    except Exception as exc:
        if job is not None and job.cancelled:
            raise               # 取消不是失败：原样抛给作业框架去吞
        return METER_FLOOR_DB, METER_FLOOR_DB, None, exc
    if buf is None or buf.size == 0:
        return METER_FLOOR_DB, METER_FLOOR_DB, None, ValueError("渲染结果是空缓冲")
    peak_db, rms_db = render.meter(buf)
    if not (peak_db == peak_db) or not (rms_db == rms_db):     # NaN 兜底
        return METER_FLOOR_DB, METER_FLOOR_DB, None, ValueError("电平读数算出了 NaN")
    return (max(METER_FLOOR_DB, float(peak_db)),
            max(METER_FLOOR_DB, float(rms_db)),
            _audition_excerpt(buf), None)


def _analysis_job(snapshot: dict, tid: str, job):
    """后台线程里跑的活：从工程快照还原一份**只属于本线程**的工程，测一条轨道。

    为什么每条件业都还原一份，而不是几条线共用一份 clone 出来的工程：
    ``render_track`` 会顺手改工程里的东西（``ensure_kit`` 往 ``track.kit`` 里补
    鼓件），几条线程共读一个可变对象就是在赌"它只读" —— 赌输的症状是随机的
    错音或错电平，而且只在特定时序下出现。每线一份副本是把这类 bug 从
    "偶发"变成"不可能"。

    还原的成本花在工作线程上，与界面无关。传进来的是 ``to_dict()`` 的结果
    （纯 dict / list / 标量），在 UI 线程只做一次 —— 在 UI 线程做深拷贝正是
    我们要避免的事。

    ``job`` 是必需的第三个参数：作业框架靠"必填位置参数恰好一个"来判断要不要
    递句柄，所以此处**不能**给它默认值（给了就永远收不到句柄，取消会失灵）。
    直接调用时传 ``None`` 即可（表示不需要取消）。
    """
    project = Project.from_dict(snapshot)
    track = project.track_by_id(tid)
    if track is None:
        return (METER_FLOOR_DB, METER_FLOOR_DB, None,
                KeyError(f"快照里没有这条轨道：{tid}"))
    return _measure_track(project, track, job)


# ==========================================================================
# 竖向推子（Qt 没有现成的，自己画）
# ==========================================================================


class VerticalFader(QWidget):
    """竖向推子。

    Qt 只有横向 ``QSlider``，而"把横向控件旋转绘制"要连着把鼠标坐标一起反变换，
    一不留神就出偏移量对不上的怪 bug。推子的几何其实极简单（一条槽 + 一个手柄），
    自己画反而更短、更可控，也才能把刻度线、刻度数字、读数一起画进去。

    交互取舍
    --------
    * **点哪跳哪（绝对定位）**：与工程里既有的 ``GlassSlider`` 一致。
    * **Shift = 精细拖动**：改为相对位移、1/4 灵敏度。混音时 0.01 的差别人耳能听出来，
      但 200px 行程下 1px 就是 0.0075 —— 不放大根本调不准。
    * **双击 = 回到基准**（音量回 1.0，限幅回 -1.0 dB）。
    """

    valueChanged = Signal(float)
    #: 一次编辑**即将开始**时发一次（就在第一次真正改值之前）。
    #: 拖动全程只发一次，撤销栈里"拖一整笔"因此只占一步。
    editStarted = Signal(str)
    editFinished = Signal()

    PAD_TOP = 6.0
    CAPTION_H = 14.0
    PAD_BOTTOM = 4.0
    GROOVE_W = 7.0
    GROOVE_X = 13.0         # 槽中心。不放在正中：右半边要留给刻度线与数字
    HANDLE_W = 20.0
    HANDLE_H = 13.0
    HANDLE_H_HOVER = 15.0
    FINE_PIXELS = 140.0     # 走完全行程所需像素（与 GlassKnob 保持一致的手感）
    FINE_SCALE = 0.25       # Shift 精细模式的灵敏度

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        minimum: float = FADER_MIN,
        maximum: float = FADER_MAX,
        value: float = FADER_UNITY,
        accent: QColor | None = None,
        caption: str = "",
        unity: float | None = FADER_UNITY,
        reset_value: float | None = FADER_UNITY,
        edit_label: str = "调整电平",
        ticks: tuple[float, ...] | None = None,
    ):
        super().__init__(parent)
        self._min = float(minimum)
        self._max = float(maximum)
        self._value = _clamp(float(value), self._min, self._max)
        self._accent = accent or accents.blue
        self._caption = caption
        self._unity = unity
        self._reset_value = reset_value
        self._edit_label = edit_label
        # 刻度默认五等分 —— 均匀刻度对线性增益最诚实（画成 dB 刻度反而要用户换算）
        self._ticks = tuple(ticks) if ticks else tuple(
            self._min + (self._max - self._min) * i / 4.0 for i in range(5)
        )

        self._dragging = False
        self._hover = False
        self._edit_open = False      # 本笔编辑是否已经压过撤销点
        self._press_y = 0.0
        self._press_value = self._value
        self._fine = False

        self.setFixedWidth(FADER_W)
        self.setMinimumHeight(FADER_ROW_MIN_H)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    # -- 值 --------------------------------------------------------------

    def value(self) -> float:
        return self._value

    def set_value(self, v: float, notify: bool = False) -> None:
        nv = _clamp(float(v), self._min, self._max)
        if abs(nv - self._value) < 1e-9:
            return
        self._value = nv
        self.update()
        if notify:
            self.valueChanged.emit(nv)

    def set_accent(self, color: QColor) -> None:
        self._accent = color
        self.update()

    def _frac(self) -> float:
        span = self._max - self._min
        return 0.0 if span <= 0 else (self._value - self._min) / span

    # -- 坐标换算（绘制与交互共用同一套，测试也直接用它算按压位置）--------

    def _track_rect(self) -> QRectF:
        r = QRectF(self.rect())
        x = min(self.GROOVE_X, max(self.GROOVE_W / 2.0 + 1.0, r.width() - 20.0))
        h = max(24.0, r.height() - self.PAD_TOP - self.CAPTION_H - self.PAD_BOTTOM)
        return QRectF(x - self.GROOVE_W / 2.0, r.top() + self.PAD_TOP, self.GROOVE_W, h)

    def _value_at(self, y: float) -> float:
        r = self._track_rect()
        if r.height() <= 0:
            return self._min
        f = _clamp((r.bottom() - y) / r.height(), 0.0, 1.0)
        return self._min + f * (self._max - self._min)

    def _y_for_value(self, v: float) -> float:
        r = self._track_rect()
        span = self._max - self._min
        f = 0.0 if span <= 0 else _clamp((v - self._min) / span, 0.0, 1.0)
        return r.bottom() - f * r.height()

    # -- 交互 ------------------------------------------------------------

    def _apply(self, nv: float) -> None:
        """改值。**撤销点在改值之前压**，否则撤销回去的是"改完的状态"。"""
        nv = _clamp(float(nv), self._min, self._max)
        if abs(nv - self._value) < 1e-9:
            return
        if not self._edit_open:
            self._edit_open = True
            self.editStarted.emit(self._edit_label)
        self.set_value(nv, notify=True)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = True
        self._edit_open = False
        self._press_y = event.position().y()
        self._press_value = self._value
        self._fine = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if not self._fine:
            # 绝对定位：按下即到位。Shift 时保持不动，只做相对微调
            self._apply(self._value_at(event.position().y()))
        self.update()

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        span = self._max - self._min
        if self._fine:
            dy = self._press_y - event.position().y()      # 往上拖 = 变响
            self._apply(self._press_value + dy / self.FINE_PIXELS * span * self.FINE_SCALE)
        else:
            self._apply(self._value_at(event.position().y()))

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self._edit_open = False
        self.editFinished.emit()
        self.update()

    def mouseDoubleClickEvent(self, event):
        target = self._reset_value
        if target is None:
            target = (self._min + self._max) / 2.0
        self._edit_open = False
        self._apply(target)

    def wheelEvent(self, event):
        fine = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        step = (self._max - self._min) * (0.005 if fine else 0.02)
        self._edit_open = False        # 滚一格 = 一步撤销，不该被上一笔的状态吃掉
        self._apply(self._value + (step if event.angleDelta().y() > 0 else -step))

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._track_rect()
        hy = self._y_for_value(self._value)
        accent = QColor(self._accent)

        # ---- 1. 槽：先挖一个凹槽出来 -----------------------------------
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.black(0.42))
        painter.drawRoundedRect(track, 3.5, 3.5)
        painter.setPen(QPen(theme.white(0.05), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), 3.0, 3.0)

        # ---- 2. 已填充部分：从手柄往下到槽底 ---------------------------
        fill = QRectF(track.x(), hy, track.width(), max(0.0, track.bottom() - hy))
        if fill.height() > 0.5:
            g = QLinearGradient(0.0, hy, 0.0, track.bottom())
            g.setColorAt(0.0, accent.lighter(118))
            g.setColorAt(1.0, QColor(accent).darker(112))
            painter.save()
            painter.setClipRect(fill)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(g))
            painter.drawRoundedRect(track, 3.5, 3.5)
            painter.restore()

        # ---- 3. 刻度参考线 --------------------------------------------
        self._paint_ticks(painter, track)

        # ---- 4. 手柄（推子帽）-----------------------------------------
        hh = self.HANDLE_H_HOVER if (self._hover or self._dragging) else self.HANDLE_H
        hw = self.HANDLE_W + (1.0 if (self._hover or self._dragging) else 0.0)
        cap = QRectF(track.center().x() - hw / 2.0, hy - hh / 2.0, hw, hh)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.black(0.45))                      # 落影：让帽子"浮"在槽上
        painter.drawRoundedRect(cap.translated(0.0, 1.4), 3.0, 3.0)

        cg = QLinearGradient(0.0, cap.top(), 0.0, cap.bottom())
        cg.setColorAt(0.0, QColor(252, 253, 255))
        cg.setColorAt(0.5, QColor(232, 235, 242))
        cg.setColorAt(1.0, QColor(196, 201, 212))
        painter.setBrush(QBrush(cg))
        painter.setPen(QPen(QColor(0, 0, 0, 120), 1.0))
        painter.drawRoundedRect(cap, 3.0, 3.0)
        # 帽面上的握把线用轨道色 —— 一眼看出这是哪条通道的推子
        painter.setPen(QPen(accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(cap.left() + 2.5, cap.center().y()),
                         QPointF(cap.right() - 2.5, cap.center().y()))

        # ---- 5. 底部标签（硬件推子下面印的就是这个）--------------------
        # 只印名字、不印数值：数值由通道条底部的读数行负责，两边都写就是重复，
        # 而且 46px 宽塞不下"音量 1.25"这种字符串
        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(labels.secondary if self._hover else labels.tertiary))
        cap_rect = QRectF(0.0, self.height() - self.CAPTION_H, self.width(), self.CAPTION_H)
        text = self._caption or f"{self._value:.2f}"
        painter.drawText(cap_rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

    def _paint_ticks(self, painter: QPainter, track: QRectF) -> None:
        painter.setFont(type_scale.caption2.font())
        fm = QFontMetricsF(type_scale.caption2.font())
        half = fm.height() / 2.0
        for tick in self._ticks:
            y = self._y_for_value(tick)
            is_unity = self._unity is not None and abs(tick - self._unity) < 1e-6
            x0 = track.right() + 2.0
            x1 = x0 + (7.0 if is_unity else 4.0)
            color = theme.white(0.42) if is_unity else theme.white(0.16)
            painter.setPen(QPen(color, 1.0))
            painter.drawLine(QPointF(x0, y), QPointF(x1, y))
            # 只给基准刻度和两端写数字：全写会糊成一片，反而看不清
            if is_unity or tick in (self._min, self._max):
                painter.setPen(QPen(labels.quaternary if not is_unity
                                    else labels.secondary))
                # 数字往里收，别和推子下面那行标题撞上 —— 最底下的刻度正好在槽末端，
                # 不钳制就会和"音量"压在一起（第一版就是这样糊成一团）
                ly = _clamp(y, track.top() + half, track.bottom() - half)
                rect = QRectF(x1 + 1.5, ly - half,
                              max(8.0, self.width() - x1 - 2.0), fm.height())
                painter.drawText(rect, Qt.AlignmentFlag.AlignLeft |
                                 Qt.AlignmentFlag.AlignVCenter, f"{tick:g}")


# ==========================================================================
# 旋钮（给 GlassKnob 补"编辑开始"信号）
# ==========================================================================


class MixerKnob(GlassKnob):
    """带撤销点信号的旋钮。

    ``GlassKnob`` 只在 ``valueChanged`` 里往外说话，而撤销点必须压在改动**之前**，
    所以只能在这里补一个 ``editStarted``。补的方式是"预测"：拖动时先按同一套公式
    算出即将变成的值，确认会变才压栈，然后仍然交给 ``GlassKnob`` 去真正改值 ——
    改值逻辑一份都不重复，只有这一个判断是重复的。
    """

    editStarted = Signal(str)

    def __init__(self, parent: QWidget | None = None, minimum: float = -1.0,
                 maximum: float = 1.0, value: float = 0.0, *,
                 accent: QColor | None = None, size: int = 32,
                 center_detent: bool = True, reset_value: float | None = None,
                 edit_label: str = "调整参数"):
        super().__init__(parent, minimum, maximum,
                         _clamp(float(value), float(minimum), float(maximum)),
                         accent=accent, size=size, center_detent=center_detent)
        self._reset_value = reset_value
        self._edit_label = edit_label
        self._edit_open = False

    def mousePressEvent(self, event):
        self._edit_open = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and not self._edit_open:
            dy = self._drag_start_y - event.globalPosition().y()
            span = self._max - self._min
            nv = _clamp(self._drag_start_value + dy / 140.0 * span, self._min, self._max)
            if abs(nv - self._value) > 1e-9:
                self._edit_open = True
                self.editStarted.emit(self._edit_label)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._edit_open = False

    def mouseDoubleClickEvent(self, event):
        if self._reset_value is not None:
            if abs(self._value - self._reset_value) > 1e-9:
                self.editStarted.emit(self._edit_label)
                self.set_value(self._reset_value, notify=True)
            return
        # 没给基准值就走 GlassKnob 的居中逻辑（双极参数回中）
        self.editStarted.emit(self._edit_label)
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        step = (self._max - self._min) * 0.05
        nv = _clamp(self._value + (step if event.angleDelta().y() > 0 else -step),
                    self._min, self._max)
        if abs(nv - self._value) > 1e-9:
            self.editStarted.emit(self._edit_label)
            self.set_value(nv, notify=True)


# ==========================================================================
# 静音 / 独奏小圆点
# ==========================================================================


class StateDot(QWidget):
    """静音/独奏开关。

    没有复用 ``GlassOrb``：它的"开/关"只差图标透明度一档，在一屏十几条通道条上
    根本分不出来。这里改用**实心强调色 + 外圈泛光** vs **空心描边**，
    隔着半米也能一眼看出哪个通道被静音了。工程里的既有结论也是这个 ——
    "未激活时必须压暗，否则用户看不出当前状态"。
    """

    toggled = Signal(bool)

    def __init__(self, letter: str, accent: QColor, parent: QWidget | None = None,
                 *, size: int = DOT_SIZE, tooltip: str = ""):
        super().__init__(parent)
        self._letter = letter
        self._accent = QColor(accent)
        self._on = False
        self._hover = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)

    def is_on(self) -> bool:
        return self._on

    def set_on(self, value: bool, notify: bool = False) -> None:
        value = bool(value)
        if value == self._on:
            return
        self._on = value
        self.update()
        if notify:
            self.toggled.emit(value)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_on(not self._on, notify=True)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        accent = QColor(self._accent)

        if self._on:
            # 开着：泛光 + 实心 + 深色字（亮底上只有深色字读得清）
            paint_bloom(painter, r, r.width() / 2.0, accent, strength=0.85, spread=4.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(r)
            painter.setPen(QPen(accent.lighter(175), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(r)
            text_color = theme.black(0.88)
        else:
            # 关着：空心 + 极淡底。与"开着"的差异是**形状级**的，不是明度级
            fill = QColor(255, 255, 255, 30 if self._hover else 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawEllipse(r)
            painter.setPen(QPen(theme.white(0.30 if self._hover else 0.20), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(r)
            text_color = labels.tertiary if self._hover else labels.quaternary

        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(text_color))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._letter)
        painter.end()


# ==========================================================================
# 电平表
# ==========================================================================


class VerticalMeter(QWidget):
    """竖向电平表（分段 LED 风格）。

    两种数据源：
    * :meth:`set_static` —— 分轨参考电平（工程分析得来），直接显示、不回落；
    * :meth:`push` —— 母带实时电平，**快起慢落**：电平表如果瞬时跟随，
      数字抖动会让人看不清峰值；真实表头靠机械惯性回落，这里用定时器模拟。

    回落定时器在数值追上目标后**自己停掉**，不做无休止的 50ms 重绘 ——
    一条静止的表最不该做的事就是每秒烧掉 20 次重绘。
    """

    #: 每个 tick（50ms）回落多少 dB → 约 30dB/s，接近硬件表头
    DECAY_DB = 1.5
    HOLD_DECAY_DB = 0.35        # 峰值保持线的基准回落速度
    HOLD_CATCHUP = 0.06         # 与当前电平差得越远落得越快（见 _tick）
    TICK_MS = 50

    def __init__(self, parent: QWidget | None = None, *, width: int = METER_W):
        super().__init__(parent)
        self._peak = METER_FLOOR_DB
        self._rms = METER_FLOOR_DB
        self._hold = METER_FLOOR_DB
        self._target_peak = METER_FLOOR_DB
        self._target_rms = METER_FLOOR_DB
        self._marker: float | None = None
        self._dimmed = False
        self._realtime = False
        self._live = False

        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

        self.setFixedWidth(width)
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    # -- 数据 ------------------------------------------------------------

    def peak_db(self) -> float:
        return self._peak

    def rms_db(self) -> float:
        return self._rms

    def hold_db(self) -> float:
        return self._hold

    def marker_db(self) -> float | None:
        return self._marker

    def set_static(self, peak_db: float, rms_db: float) -> None:
        """静态参考电平：立刻显示，不回落（它不是"此刻"，而是"这条轨道有多响"）。"""
        self._realtime = False
        self._timer.stop()
        self._peak = max(METER_FLOOR_DB, float(peak_db))
        self._rms = max(METER_FLOOR_DB, float(rms_db))
        self._hold = self._peak
        self._target_peak = self._peak
        self._target_rms = self._rms
        self.update()

    def push(self, peak_db: float, rms_db: float) -> None:
        """实时推入一个电平：立即起、慢慢落。"""
        self._realtime = True
        self._target_peak = max(METER_FLOOR_DB, float(peak_db))
        self._target_rms = max(METER_FLOOR_DB, float(rms_db))
        if self._target_peak >= self._peak:
            self._peak = self._target_peak
            self._hold = max(self._hold, self._target_peak)
        if self._target_rms >= self._rms:
            self._rms = self._target_rms
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def drop(self) -> None:
        """把目标打到表底（停止播放/静音时用），由回落动画负责落下去。"""
        self.push(METER_FLOOR_DB, METER_FLOOR_DB)

    def set_marker(self, db: float | None) -> None:
        self._marker = None if db is None else _clamp(float(db), METER_FLOOR_DB,
                                                     METER_CEIL_DB)
        self.update()

    def set_dimmed(self, value: bool) -> None:
        value = bool(value)
        if value != self._dimmed:
            self._dimmed = value
            self.update()

    def set_live(self, value: bool) -> None:
        """是否处于"实时"状态（画一个会亮的小点，与静态参考区分开）。"""
        value = bool(value)
        if value != self._live:
            self._live = value
            self.update()

    def _settled(self) -> bool:
        return (abs(self._peak - self._target_peak) < 0.05
                and abs(self._rms - self._target_rms) < 0.05)

    def _tick(self) -> None:
        """一次回落。测试里直接调它 —— 离屏环境没有事件循环，定时器不会自己跑。"""
        changed = False
        if self._peak > self._target_peak:
            self._peak = max(self._target_peak, self._peak - self.DECAY_DB)
            changed = True
        if self._rms > self._target_rms:
            self._rms = max(self._target_rms, self._rms - self.DECAY_DB)
            changed = True
        if self._hold > METER_FLOOR_DB:
            # 保持线落得比电平慢（否则它就没意义了），但离当前电平越远落得越快 ——
            # 固定步长会让停播后那条线拖着十几秒才归位，看着像卡住了。
            step = self.HOLD_DECAY_DB + self.HOLD_CATCHUP * max(0.0, self._hold - self._peak)
            self._hold = max(self._peak, self._hold - step)
            changed = True
        if changed:
            self.update()
        if self._settled() and self._hold <= self._peak + 1e-6:
            self._timer.stop()

    # -- 绘制 ------------------------------------------------------------

    def _y_for_db(self, db: float) -> float:
        r = QRectF(self.rect()).adjusted(0.0, 0.0, 0.0, 0.0)
        f = (db - METER_FLOOR_DB) / (METER_CEIL_DB - METER_FLOOR_DB)
        f = _clamp(f, 0.0, 1.0)
        return r.bottom() - f * r.height()

    @staticmethod
    def _zone_color(db: float) -> QColor:
        if db >= ZONE_RED_DB:
            return accents.red
        if db >= ZONE_YELLOW_DB:
            return accents.yellow
        return accents.green

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        dim = 0.34 if self._dimmed else 1.0

        # 槽
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.black(0.55))
        painter.drawRoundedRect(r, 2.5, 2.5)
        painter.setPen(QPen(theme.white(0.07), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(r, 2.5, 2.5)

        # 分段 LED：每 3px 一段、留 1px 缝 —— 连续色块看起来像进度条，
        # 分段才像表
        seg_h = 3.0
        pitch = 4.0
        n = max(1, int((r.height() - 3.0) // pitch))
        x0 = r.left() + 1.5
        w = r.width() - 3.0
        for i in range(n):
            y = r.bottom() - 2.0 - (i + 1) * pitch + 1.0
            db = METER_FLOOR_DB + (i + 0.5) / n * (METER_CEIL_DB - METER_FLOOR_DB)
            lit_rms = db <= self._rms + 1e-6
            lit_peak = db <= self._peak + 1e-6
            if lit_rms:
                c = self._zone_color(db)
                c.setAlphaF(min(1.0, 0.92 * dim))
            elif lit_peak:
                # 峰值高于 RMS 的那一小段用淡色 —— 一眼看出"瞬时冲到哪了"
                c = self._zone_color(db)
                c.setAlphaF(0.30 * dim)
            else:
                c = QColor(255, 255, 255, 16)
                c.setAlphaF(0.06 * dim)
            painter.setBrush(c)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(QRectF(x0, y, w, seg_h))

        # 峰值保持线：比峰值条更亮的一条，慢慢往下掉（硬件表的"peak hold"）
        if self._hold > METER_FLOOR_DB:
            y = self._y_for_db(self._hold)
            c = QColor(255, 255, 255, int(215 * dim))
            painter.setPen(QPen(c, 1.4))
            painter.drawLine(QPointF(r.left() + 1.0, y), QPointF(r.right() - 1.0, y))

        # 阈值标记（母带条用来画限幅阈值：一眼看出"还能往上推多少"）
        if self._marker is not None:
            y = self._y_for_db(self._marker)
            c = QColor(accents.orange)
            c.setAlphaF(0.85 * dim)
            pen = QPen(c, 1.0, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        # 实时指示灯
        if self._live:
            c = QColor(accents.red)
            c.setAlphaF(0.9)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawEllipse(QRectF(r.center().x() - 2.0, r.top() - 0.5, 4.0, 4.0))
        painter.end()


# ==========================================================================
# 通道条
# ==========================================================================


class ChannelStrip(GlassCard):
    """一条轨道的通道条。"""

    changed = Signal()
    editStarted = Signal(str)
    selectRequested = Signal(object)
    #: 请求试听这条通道（页面负责取片段并播放）
    auditionRequested = Signal(object)

    def __init__(self, track: Track, project: Project, index: int,
                 parent: QWidget | None = None):
        # 不用液态玻璃：通道条同屏有十几条，而一次液态折射合成是 20~50ms 的
        # numpy 运算（见 glass.py），十几条同屏就是几百毫秒，横向滚动时还会
        # 反复重建。通道条是**结构密集**的控件，用静态玻璃，把液态留给页面级面板。
        super().__init__(parent, radius=radii.card, liquid=False,
                         material=theme.black(0.30))
        self.track = track
        self.project = project
        self.index = index
        self._color = QColor(track.color)
        self.set_accent(self._color)
        self.setFixedWidth(STRIP_W)
        self.setMinimumHeight(self.minimum_height())
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # ---- 头部那颗图标：不只是装饰，点它就是试听这条轨道 -------------
        # "边调边听"是本软件的核心体验：推子推完、声像拧完，得马上听一耳朵
        # 才知道调对没有。所以把最容易点到的位置（头部图标）留给试听。
        self.btn_audition = GlassImageButton(
            track.icon, self, size=22, icon_size=16, style="plain",
            tooltip=f"试听「{track.name}」",
        )
        self.btn_audition.clicked.connect(lambda: self.auditionRequested.emit(self))

        # ---- 声像 ------------------------------------------------------
        self.pan = MixerKnob(self, -1.0, 1.0, track.pan, accent=accents.teal,
                             size=34, center_detent=True, edit_label="调整声像")
        self.pan.valueChanged.connect(self._on_pan)
        self.pan.editStarted.connect(self.editStarted)

        # ---- 发送 ------------------------------------------------------
        self.send_reverb = MixerKnob(self, 0.0, 1.0, track.send_reverb,
                                     accent=accents.purple, size=26,
                                     center_detent=False, reset_value=0.0,
                                     edit_label="调整混响发送")
        self.send_delay = MixerKnob(self, 0.0, 1.0, track.send_delay,
                                    accent=accents.blue_light, size=26,
                                    center_detent=False, reset_value=0.0,
                                    edit_label="调整延迟发送")
        self.send_reverb.valueChanged.connect(self._on_send_reverb)
        self.send_delay.valueChanged.connect(self._on_send_delay)
        self.send_reverb.editStarted.connect(self.editStarted)
        self.send_delay.editStarted.connect(self.editStarted)

        # ---- 推子 + 电平表 ---------------------------------------------
        self.fader = VerticalFader(self, minimum=FADER_MIN, maximum=FADER_MAX,
                                   value=track.volume, accent=self._color,
                                   caption="音量", unity=FADER_UNITY,
                                   reset_value=FADER_UNITY, edit_label="调整音量")
        self.fader.valueChanged.connect(self._on_volume)
        self.fader.editStarted.connect(self.editStarted)
        self.fader.editFinished.connect(self.changed)

        self.meter = VerticalMeter(self)

        # ---- 静音 / 独奏 -----------------------------------------------
        self.mute_dot = StateDot("M", accents.red, self, tooltip="静音（Mute）")
        self.solo_dot = StateDot("S", accents.yellow, self, tooltip="独奏（Solo）")
        self.mute_dot.toggled.connect(self._on_mute)
        self.solo_dot.toggled.connect(self._on_solo)
        self._sync_toggles()

        self._layout_children()

    # -- 布局 ------------------------------------------------------------

    @staticmethod
    def minimum_height() -> int:
        return HEADER_H + PAN_ROW_H + SEND_ROW_H + FADER_ROW_MIN_H + TOGGLE_ROW_H + READOUT_H + 6

    def _layout_children(self) -> None:
        """手写定位而不是 QVBoxLayout：推子行要吃掉"剩下的全部高度"，
        而头部与底部各行的位置必须精确，用布局反而要嵌三层才表达得清。"""
        w = self.width()
        y = HEADER_H

        self.btn_audition.move(8, 8)
        self.pan.move((w - self.pan.width()) // 2, y + 4)
        y += PAN_ROW_H

        knob = self.send_reverb.width()
        pair_w = knob * 2 + 8
        x0 = (w - pair_w) // 2
        self.send_reverb.move(x0, y + 2)
        self.send_delay.move(x0 + knob + 8, y + 2)
        y += SEND_ROW_H

        bottom_reserved = TOGGLE_ROW_H + READOUT_H + 6
        fader_h = max(FADER_ROW_MIN_H, self.height() - y - bottom_reserved)
        # 推子与表成对居中：混音台上"表就在推子右边"是肌肉记忆，不能拆散
        pair_w = FADER_W + 6 + METER_W
        fx = max(8, (w - pair_w) // 2)
        self.fader.setGeometry(fx, y, FADER_W, fader_h)
        self.meter.setGeometry(fx + FADER_W + 6, y + 6, METER_W, max(40, fader_h - 22))
        y += fader_h

        total = DOT_SIZE * 2 + 10
        dx = (w - total) // 2
        self.mute_dot.move(dx, y + 2)
        self.solo_dot.move(dx + DOT_SIZE + 10, y + 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_children()

    def mousePressEvent(self, event):
        # 点条体 = 选中这条通道（子控件会自己吃掉点击，不会冒泡到这里）
        self.selectRequested.emit(self)
        super().mousePressEvent(event)

    # -- 数据同步 --------------------------------------------------------

    def _on_volume(self, v: float) -> None:
        self.track.volume = float(v)
        self.changed.emit()
        self.update()

    def _on_pan(self, v: float) -> None:
        self.track.pan = float(v)
        self.changed.emit()
        self.update()

    def _on_send_reverb(self, v: float) -> None:
        self.track.send_reverb = float(v)
        self.changed.emit()

    def _on_send_delay(self, v: float) -> None:
        self.track.send_delay = float(v)
        self.changed.emit()

    def _on_mute(self, on: bool) -> None:
        # 撤销点必须在赋值**之前**压：push_undo 是当场对工程做 to_dict() 快照，
        # 压晚了快照里就是"静音之后"的状态，Ctrl+Z 撤了个寂寞。
        # StateDot.toggled 触发时工程还没被碰过，正是压栈的正确时机。
        self.editStarted.emit("静音")
        self.track.mute = bool(on)
        self._sync_toggles()
        self.changed.emit()

    def _on_solo(self, on: bool) -> None:
        self.editStarted.emit("独奏")        # 同上：先压撤销点，再改数据
        self.track.solo = bool(on)
        self._sync_toggles()
        self.changed.emit()

    def _sync_toggles(self) -> None:
        self.mute_dot.set_on(self.track.mute)
        self.solo_dot.set_on(self.track.solo)

    def refresh(self) -> None:
        """把控件从数据模型拉回同步（撤销/外部改动之后调用）。"""
        self.fader.set_value(self.track.volume)
        self.pan.set_value(self.track.pan)
        self.send_reverb.set_value(self.track.send_reverb)
        self.send_delay.set_value(self.track.send_delay)
        self._sync_toggles()
        self.update()

    def set_reference(self, peak_db: float, rms_db: float, *, audible: bool) -> None:
        """设置电平表的静态参考值；不可闻时落到表底并压暗。"""
        if not audible:
            self.meter.set_static(METER_FLOOR_DB, METER_FLOOR_DB)
            self.meter.set_dimmed(True)
        else:
            self.meter.set_static(peak_db, rms_db)
            self.meter.set_dimmed(False)

    def is_audible(self) -> bool:
        return self.project.is_audible(self.track)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        audible = self.is_audible()
        name_color = labels.label if audible else labels.quaternary

        # 左侧色条：轨道身份的第一识别信号
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color if audible else QColor(self._color).darker(190))
        painter.drawRoundedRect(QRectF(4.0, 8.0, 3.0, self.height() - 16.0), 1.5, 1.5)

        # 图标交给 btn_audition 去画（它还要承担点击试听），这里只画名字
        f = type_scale.caption1.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        fm = QFontMetricsF(f)
        name_w = self.width() - 40
        painter.setPen(QPen(name_color))
        painter.drawText(QRectF(35, 7, name_w, 16),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText(self.track.name, Qt.TextElideMode.ElideRight, name_w))

        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(labels.quaternary))
        kind = "鼓机" if self.track.kind == "drum" else "合成器"
        notes = len(self.track.notes)
        painter.drawText(QRectF(35, 24, name_w, 14),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         f"{kind} · {notes} 音符")

        # 分区标题：通道条上每个控件都得有名字，否则旋钮一串排下来没人知道哪个是哪个
        painter.setPen(QPen(labels.quaternary))
        painter.drawText(QRectF(0, self.pan.y() + self.pan.height() + 1, self.width(), 12),
                         Qt.AlignmentFlag.AlignCenter, "声像")

        send_y = HEADER_H + PAN_ROW_H
        for knob in (self.send_reverb, self.send_delay):
            painter.drawText(QRectF(knob.x() - 6, send_y + knob.height() + 1, knob.width() + 12, 12),
                             Qt.AlignmentFlag.AlignCenter,
                             "混响" if knob is self.send_reverb else "延迟")

        # 底部读数：线性值 + dB
        y = self.height() - READOUT_H
        v = self.track.volume
        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.secondary if audible else labels.quaternary))
        painter.drawText(QRectF(0, y, self.width(), READOUT_H),
                         Qt.AlignmentFlag.AlignCenter,
                         f"{v:.2f}  {_db_text(_gain_db(v))}dB")
        painter.end()


class MasterStrip(GlassCard):
    """母带通道条：总线音量、限幅阈值、混响空间、延迟反馈 + 实时电平表。"""

    changed = Signal()
    editStarted = Signal(str)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent, radius=radii.card, liquid=False,
                         material=theme.black(0.38))
        self.project = project
        self.set_accent(accents.orange)
        self.setFixedWidth(MASTER_W)
        self.setMinimumHeight(self.minimum_height())
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        m = project.master

        self.fader = VerticalFader(self, minimum=FADER_MIN, maximum=FADER_MAX,
                                   value=m.volume, accent=accents.orange,
                                   caption="总线", unity=FADER_UNITY,
                                   reset_value=FADER_UNITY, edit_label="调整总线音量")
        self.limiter = VerticalFader(self, minimum=-12.0, maximum=0.0,
                                     value=m.limiter_threshold, accent=accents.red,
                                     caption="限幅", unity=None,
                                     reset_value=-1.0, edit_label="调整限幅阈值")
        self.meter = VerticalMeter(self, width=20)

        reverb_size = float(m.reverb.params.get("size", 0.7))
        delay_fb = float(m.delay.params.get("feedback", 0.3))
        self.reverb = MixerKnob(self, 0.0, 1.0, _clamp(reverb_size, 0.0, 1.0),
                                accent=accents.purple, size=28, center_detent=False,
                                reset_value=0.0, edit_label="调整混响空间")
        self.delay = MixerKnob(self, 0.0, 0.9, _clamp(delay_fb, 0.0, 0.9),
                               accent=accents.teal, size=28, center_detent=False,
                               reset_value=0.0, edit_label="调整延迟反馈")

        self.fader.valueChanged.connect(self._on_volume)
        self.limiter.valueChanged.connect(self._on_limiter)
        self.reverb.valueChanged.connect(self._on_reverb)
        self.delay.valueChanged.connect(self._on_delay)
        for w in (self.fader, self.limiter, self.reverb, self.delay):
            w.editStarted.connect(self.editStarted)
        self.fader.editFinished.connect(self.changed)
        self.limiter.editFinished.connect(self.changed)

        self.meter.set_marker(m.limiter_threshold)

        self._layout_children()

    @staticmethod
    def minimum_height() -> int:
        return HEADER_H + 4 + FADER_ROW_MIN_H + SEND_ROW_H + READOUT_H * 3 + 6

    def _layout_children(self) -> None:
        w = self.width()
        y = HEADER_H + 4

        bottom_reserved = SEND_ROW_H + READOUT_H * 3 + 6
        fader_h = max(FADER_ROW_MIN_H, self.height() - y - bottom_reserved)
        self.fader.setGeometry(14, y, FADER_W, fader_h)
        self.limiter.setGeometry(14 + FADER_W + 6, y, FADER_W, fader_h)
        self.meter.setGeometry(14 + FADER_W * 2 + 16, y + 6, 20, max(40, fader_h - 22))
        y += fader_h

        knob = self.reverb.width()
        # 两个旋钮拉开一点：母带旋钮的标题是四字（"混响空间"/"延迟反馈"），
        # 贴着放会连成一串读不出来
        pair_w = knob * 2 + 26
        x0 = (w - pair_w) // 2
        self.reverb.move(x0, y + 2)
        self.delay.move(x0 + knob + 26, y + 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_children()

    # -- 数据同步 --------------------------------------------------------

    def _on_volume(self, v: float) -> None:
        self.project.master.volume = float(v)
        self.changed.emit()
        self.update()

    def _on_limiter(self, v: float) -> None:
        self.project.master.limiter_threshold = float(v)
        self.meter.set_marker(float(v))
        self.changed.emit()
        self.update()

    def _on_reverb(self, v: float) -> None:
        # 母带混响的 size 就是"空间大小"；写进 params 而不动 mix，
        # 因为总线混响的 mix 恒为 1.0（送多少由各轨的 send 决定）
        self.project.master.reverb.params["size"] = float(v)
        self.changed.emit()
        self.update()

    def _on_delay(self, v: float) -> None:
        self.project.master.delay.params["feedback"] = float(v)
        self.changed.emit()
        self.update()

    def refresh(self) -> None:
        m = self.project.master
        self.fader.set_value(m.volume)
        self.limiter.set_value(m.limiter_threshold)
        self.reverb.set_value(_clamp(float(m.reverb.params.get("size", 0.7)), 0.0, 1.0))
        self.delay.set_value(_clamp(float(m.delay.params.get("feedback", 0.3)), 0.0, 0.9))
        self.meter.set_marker(m.limiter_threshold)
        self.update()

    def set_static_level(self, peak_db: float, rms_db: float) -> None:
        """停止播放时显示最近一次整曲渲染的母带电平。"""
        self.meter.set_live(False)
        self.meter.set_static(peak_db, rms_db)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        pm = icons.pixmap("control_knobs", 22, opacity=0.95, dpr=self.devicePixelRatioF())
        painter.drawPixmap(12, 8, pm)

        f = type_scale.caption1.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        painter.drawText(QRectF(40, 7, self.width() - 50, 16),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "母带")
        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(labels.quaternary))
        painter.drawText(QRectF(40, 24, self.width() - 50, 14),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         "MASTER BUS")

        # 旋钮标题
        send_y = HEADER_H + 4 + max(FADER_ROW_MIN_H, self.height()
                                    - (HEADER_H + 4) - (SEND_ROW_H + READOUT_H * 3 + 6))
        for knob, name in ((self.reverb, "混响空间"), (self.delay, "延迟反馈")):
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(QRectF(knob.x() - 10, send_y + knob.height() + 1,
                                    knob.width() + 20, 12),
                             Qt.AlignmentFlag.AlignCenter, name)

        # 读数三行
        m = self.project.master
        rows = (
            f"总线 {m.volume:.2f} · {_db_text(_gain_db(m.volume))}dB",
            f"限幅 {m.limiter_threshold:+.1f} dBFS",
            f"空间 {float(m.reverb.params.get('size', 0.7)):.2f} · "
            f"反馈 {float(m.delay.params.get('feedback', 0.3)):.2f}",
        )
        y0 = self.height() - READOUT_H * 3
        painter.setFont(type_scale.caption2.font())
        for i, text in enumerate(rows):
            painter.setPen(QPen(labels.tertiary if i else labels.secondary))
            painter.drawText(QRectF(0, y0 + i * READOUT_H, self.width(), READOUT_H),
                             Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


# ==========================================================================
# 页面
# ==========================================================================


class MixerPage(QWidget):
    """混音台页面：一排通道条 + 最右侧母带条。"""

    #: 用户改了工程内容，需要重渲染（改动前已经 push_undo 过）
    edited = Signal()
    #: 请求把播放头移到某个时间（秒）—— 本页不产生，但契约要求有这个信号
    seekRequested = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._controller = None
        self._strips: list[ChannelStrip] = []
        self._strips_by_id: dict[str, ChannelStrip] = {}
        self._levels: dict[str, tuple[float, float]] = {}
        self._excerpts: dict[str, np.ndarray] = {}
        self._queue: list[str] = []
        #: 正在后台跑的作业：轨道 id → 作业句柄。有它才能"请它们停下"，
        #: 也才能在页面被丢弃时不留下没人认领的作业。
        self._jobs: dict[str, object] = {}
        #: 分析轮次代号。每开始新一轮就 +1，结果回来时对不上就丢掉 ——
        #: 用户在分析途中改了工程，回来的旧结果不许覆盖新的。
        #: （取消是快路径，代号是兜底：取消是协作式的，总有"已经算完、
        #: 正在回程"的那一瞬间。）
        self._round = 0
        #: 分析失败的留痕（渲染炸了、缓冲是空的…）。留着它是为了让 _update_status
        #: 每轮都能把故障播报出来 —— 只写一次 _status 的话，下一次进度刷新就把它盖掉了
        self._failures: list[str] = []
        self._analyzed_at = 0.0
        #: 上一次分析所依据的内容指纹。内容没变就不再重算 —— 参考电平只取决于
        #: 工程内容，同样的内容算第二遍不会有不同的结果（见 _maybe_reanalyze）。
        self._analyzed_signature = ""
        self._position = 0.0
        self._playing = False
        self._master: MasterStrip | None = None
        self._divider: GlassDivider | None = None
        self._selected: ChannelStrip | None = None

        # 分析调度定时器。**它不再是"每 tick 算一条"的工作循环** —— 有 controller
        # 时它只负责起跳一次（把整轮投给后台），然后自己停掉。没有 controller
        # （页面被单独使用、或控制器是旧版）时才退回老路：每 tick 同步算一条，
        # 两条之间让事件循环喘一口气 —— 那条路会卡界面，所以它只是兜底。
        self._analyze_timer = QTimer(self)
        self._analyze_timer.setInterval(ANALYSIS_GAP_MS)
        self._analyze_timer.timeout.connect(self._analyze_step)

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)

        # ---- 工具条 ----------------------------------------------------
        bar = GlassPanel(self, material=theme.black(0.20), radius=radii.control)
        bar.setFixedHeight(42)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        bl.setSpacing(5)

        def mk(icon_name, tip, slot):
            b = GlassImageButton(icon_name, bar, size=28, icon_size=16, tooltip=tip)
            b.clicked.connect(slot)
            bl.addWidget(b)
            return b

        self.btn_undo = mk("undo", "撤销 (Ctrl+Z)", self._undo)
        self.btn_redo = mk("redo", "重做 (Ctrl+Y)", self._redo)
        bl.addSpacing(6)
        self.btn_measure = mk("refresh", "重新测量各轨参考电平", self._remeasure)
        self.btn_clear = mk("speaker_mute", "清除所有静音与独奏", self._clear_mute_solo)
        bl.addStretch(1)
        self._status = QLabel("", bar)
        self._status.setFont(type_scale.caption1.font())
        self._status.setStyleSheet(
            f"color: rgba({labels.tertiary.red()},{labels.tertiary.green()},"
            f"{labels.tertiary.blue()},{labels.tertiary.alphaF():.3f});background:transparent;")
        bl.addWidget(self._status)
        self._bar = bar

        # ---- 通道条区（横向滚动）---------------------------------------
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:horizontal{background:transparent;height:9px;margin:0;}"
            "QScrollBar::handle:horizontal{background:rgba(255,255,255,45);border-radius:4px;"
            "min-width:40px;}"
            "QScrollBar::handle:hover{background:rgba(255,255,255,75);}"
            "QScrollBar::add-line,QScrollBar::sub-line{width:0;height:0;}"
            "QScrollBar::add-page,QScrollBar::sub-page{background:transparent;}"
        )

        self.host = QWidget()
        self.host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.row = QHBoxLayout(self.host)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(spacing.xs)
        self.scroll.setWidget(self.host)
        root.addWidget(bar)
        root.addWidget(self.scroll, 1)

        # ---- 提示条 ----------------------------------------------------
        hint = GlassCard(self, radius=radii.control)
        hint.setFixedHeight(30)
        hl = QHBoxLayout(hint)
        hl.setContentsMargins(12, 0, 12, 0)
        self._hint = QLabel(
            "点通道图标试听这条轨道 · 拖动推子调音量（Shift 精细 · 双击回 1.0）· "
            "旋钮上下拖动（双击归零）· 电平表为工程分析出的参考值，母带表为实时值", hint)
        self._hint.setFont(type_scale.caption1.font())
        self._hint.setStyleSheet(
            f"color: rgba({labels.tertiary.red()},{labels.tertiary.green()},"
            f"{labels.tertiary.blue()},{labels.tertiary.alphaF():.3f});background:transparent;")
        hl.addWidget(self._hint)
        hl.addStretch(1)
        self._count = QLabel("", hint)
        self._count.setFont(type_scale.caption1.font())
        self._count.setStyleSheet(
            f"color: rgba({labels.secondary.red()},{labels.secondary.green()},"
            f"{labels.secondary.blue()},{labels.secondary.alphaF():.3f});background:transparent;")
        hl.addWidget(self._count)
        self._hint_bar = hint

        root.addWidget(hint)

        self.rebuild()

    # -- 构建 ------------------------------------------------------------

    def rebuild(self) -> None:
        """工程**结构**变了：重建全部通道条（也重新测一次参考电平）。"""
        for strip in self._strips:
            self.row.removeWidget(strip)
            strip.setParent(None)
            strip.deleteLater()
        self._strips.clear()
        self._strips_by_id.clear()
        if self._master is not None:
            self.row.removeWidget(self._master)
            self._master.setParent(None)
            self._master.deleteLater()
            self._master = None
        if self._divider is not None:
            self.row.removeWidget(self._divider)
            self._divider.setParent(None)
            self._divider.deleteLater()
            self._divider = None

        for i, track in enumerate(self.project.tracks):
            strip = ChannelStrip(track, self.project, i, self.host)
            strip.changed.connect(self._on_strip_changed)
            strip.editStarted.connect(self._commit)
            strip.selectRequested.connect(self._select_strip)
            strip.auditionRequested.connect(self._audition_strip)
            self.row.addWidget(strip)
            self._strips.append(strip)
            self._strips_by_id[track.id] = strip

        # 母带条在最右，用一条竖分隔线跟分轨分开 —— 它是总线，不是"第 N+1 条轨道"
        self._divider = GlassDivider(self.host, vertical=True)
        self._divider.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.row.addWidget(self._divider)

        self._master = MasterStrip(self.project, self.host)
        self._master.changed.connect(self._on_strip_changed)
        self._master.editStarted.connect(self._commit)
        self.row.addWidget(self._master)

        self.host.setMinimumWidth(
            len(self.project.tracks) * (STRIP_W + spacing.xs) + MASTER_W + 8
        )
        self._levels.clear()
        self._excerpts.clear()
        self._update_count()
        self._select_strip(self._strips[0] if self._strips else None)
        self._schedule_analysis()

    def set_project(self, project: Project) -> None:
        self.project = project
        self.rebuild()

    def refresh_notes(self) -> None:
        """只是参数/音符变了：把控件与数据模型对齐，并按需重测参考电平。

        这里**不无条件重测**：用户每改一个音符，控制器都会（防抖后）重渲染一次，
        如果每次都跟着把 5 条轨道重新分析一遍，就等于把渲染开销翻倍。
        所以自动重测带最小间隔，用户想要立刻更新可以点工具条上的"重新测量"。
        """
        for strip in self._strips:
            strip.refresh()
            self._refresh_meter(strip)
        if self._master is not None:
            self._master.refresh()
        self._update_count()
        self._maybe_reanalyze()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        self._position = float(seconds)
        self._playing = bool(playing)
        if self._master is not None:
            self._master.meter.set_live(self._playing)

    # -- 与控制器对接 ----------------------------------------------------

    def bind(self, controller) -> None:
        self._controller = controller
        controller.transportChanged.connect(self._on_transport)
        try:
            controller.undoStateChanged.connect(self._on_undo_state)
        except Exception:
            pass
        try:
            controller.renderFinished.connect(self._on_render_finished)
        except Exception:
            pass
        # 控制器里可能已经有渲染结果（比如页面是后来才创建的）
        stats = getattr(controller, "stats", None)
        if (self._master is not None and stats is not None
                and getattr(stats, "peak_db", METER_FLOOR_DB) > METER_FLOOR_DB):
            self._master.set_static_level(stats.peak_db, stats.rms_db)

    def _on_undo_state(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_undo.set_dimmed(not can_undo)
        self.btn_redo.set_dimmed(not can_redo)

    def _commit(self, label: str) -> None:
        """改动**之前**压撤销点。一次拖动只在起点调用一次（见 VerticalFader）。"""
        ctrl = self._controller
        if ctrl is not None:
            ctrl.push_undo(label)

    def _undo(self) -> None:
        if self._controller is not None and self._controller.undo():
            self.rebuild()

    def _redo(self) -> None:
        if self._controller is not None and self._controller.redo():
            self.rebuild()

    def _on_transport(self, state) -> None:
        """播放线程给的只有总线电平 —— 分轨拿不到实时值，所以只喂母带表。"""
        master = getattr(self, "_master", None)
        if master is None:
            return
        self._playing = bool(getattr(state, "playing", False))
        master.meter.set_live(self._playing)
        if self._playing:
            master.meter.push(getattr(state, "peak_db", METER_FLOOR_DB),
                              getattr(state, "rms_db", METER_FLOOR_DB))
        else:
            stats = getattr(self._controller, "stats", None)
            if stats is not None and getattr(stats, "peak_db", METER_FLOOR_DB) > METER_FLOOR_DB:
                master.set_static_level(stats.peak_db, stats.rms_db)
            else:
                master.meter.drop()

    def _on_render_finished(self, stats) -> None:
        master = getattr(self, "_master", None)
        if master is not None and not self._playing:
            master.set_static_level(stats.peak_db, stats.rms_db)

    # -- 电平 ------------------------------------------------------------

    def analyze_track(self, track: Track) -> tuple[float, float]:
        """渲染一条轨道并算 (峰值 dB, 有效值 dB)。

        这是本页唯一的"重活"。**同步**做，调用它的地方只有两处：测试，
        以及"还没轮到它就点了试听"那条兜底路径。界面上的例行测量一律走后台
        （见 :meth:`_schedule_analysis`）。
        """
        peak_db, rms_db, _excerpt = self._analyze_full(track)
        return peak_db, rms_db

    def _analyze_full(self, track: Track) -> tuple[float, float, np.ndarray | None]:
        """同步渲染一次，同时产出 (峰值 dB, RMS dB, 试听片段)。

        合成一次渲染既给电平表、又留一段试听片段 —— 试听要是独立再渲染一遍，
        点一下图标就要等半秒才有声（见 :func:`_audition_strip`）。

        真正的计算在模块级 :func:`_measure_track`，后台线程用的是同一份 ——
        两边口径必须一致。这里只补上页面特有的那一半：失败要留痕。
        """
        peak_db, rms_db, excerpt, exc = _measure_track(self.project, track)
        if exc is not None:
            # 这里绝不能静默吞掉：渲染真坏掉时，界面上唯一的症状就是"这条轨道没声音"，
            # 用户分不清是自己编曲空了还是引擎炸了，也永远等不到提示。
            self._note_failure(track, exc)
            return METER_FLOOR_DB, METER_FLOOR_DB, None
        return peak_db, rms_db, excerpt

    def _note_failure(self, track: Track, exc: BaseException) -> None:
        """记下一次分析失败：既立刻写进状态栏，也留在 _failures 里反复播报。

        两处都要写，因为它们会被不同东西覆盖：``_status`` 会被用户的下一步操作
        或下一次分析进度改写，而 ``_failures`` 只在开始新一轮测量时才清空
        （见 :meth:`_schedule_analysis`）。
        """
        note = f"「{track.name}」参考电平分析失败：{type(exc).__name__}: {exc}"
        if note not in self._failures:          # 同一个故障反复测到就不要再堆一条
            self._failures.append(note)
        self._message(note)

    # -- 分析调度 --------------------------------------------------------

    @property
    def analyzing(self) -> bool:
        """还有参考电平没测完（排队中，或在后台跑）。"""
        return bool(self._queue)

    def _schedule_analysis(self) -> None:
        """开始新一轮参考电平测量。

        只做三件事：作废旧一轮、排好队列、让定时器起跳。真正"投给后台"发生在
        下一个 tick（见 :meth:`_analyze_step`）—— 因为 ``rebuild()`` 是在页面
        构造里调的，那时控制器可能还没 bind 上来（``main_window`` 与测试都是
        先构造页面再接线），当场投出去只能走同步兜底。等一跳既让 bind 先完成，
        又什么都没阻塞。
        """
        self._failures.clear()          # 新的一轮测量：上一轮的失败记录不再代表现状
        self._round += 1                # 从此上一轮的结果一律作废
        self._cancel_jobs()
        self._queue = [t.id for t in self.project.tracks]
        if not self._queue:
            self._analyze_timer.stop()
            self._update_status()
            return
        self._update_status()
        self._analyze_timer.start()

    def _cancel_jobs(self) -> None:
        """请还在后台跑的作业停下。

        取消是协作式的（它们在下一个音符边界收工），所以这里**不等**它们 ——
        结果回来时凭轮次代号作废即可。为什么要主动取消而不是任它们跑完：
        它们占着线程池的位置，而新一轮正等着人手。
        """
        for job in self._jobs.values():
            try:
                job.cancel()
            except Exception:
                pass
        self._jobs.clear()

    def _submit_round(self) -> bool:
        """把队列里还没投出去的轨道交给后台。返回"是否走了后台路径"。

        工程快照在 UI 线程只做一次 ``to_dict()``（示范曲 45KB，几毫秒），
        每条件业再在工作线程里各自还原一份（见 :func:`_analysis_job`）。
        后台绝不能读一个正被 GUI 线程改写的对象 —— 用户拖推子时改的正是
        ``volume``/``pan`` 这些字段。
        """
        ctrl = self._controller
        submit = getattr(ctrl, "submit_background", None) if ctrl is not None else None
        if not callable(submit):
            return False                # 没有后台设施：退回同步兜底
        snapshot = self.project.to_dict()
        round_id = self._round
        for tid in list(self._queue):
            if tid in self._jobs:
                continue                # 这条已经在后台跑了
            track = self.project.track_by_id(tid)
            name = track.name if track is not None else tid
            self._jobs[tid] = submit(
                functools.partial(_analysis_job, snapshot, tid),
                label=f"分析参考电平「{name}」",
                on_done=functools.partial(self._on_analysis_done, round_id, tid),
                on_error=functools.partial(self._on_analysis_error, round_id, tid),
            )
        return True

    def _maybe_reanalyze(self) -> None:
        """refresh_notes 之后的按需重测。

        两道闸门：
        1. **内容指纹**：跟上一轮测过的内容一模一样就不测。这一条最管用 ——
           它挡掉的正是本页最费的那部分开销（5 条轨道各渲染一遍）。
        2. 最小间隔（2 秒）：用户每改一个音符，控制器防抖后会重渲染一次、
           界面刷新信号跟着就来；每次都把全部轨道重新分析一遍等于把渲染开销
           翻倍。间隔挡住连点，又不至于让参考值明显过时。
        """
        if self._queue or self._analyze_timer.isActive():
            return                       # 已经在测了
        signature = project_signature(self.project)
        if signature and signature == self._analyzed_signature:
            return                       # 内容没变，参考电平不会变
        if time.perf_counter() - self._analyzed_at < ANALYSIS_MIN_GAP:
            return
        self._schedule_analysis()

    def _analyze_step(self) -> None:
        """定时器里跑的一步。

        有后台设施时，这一步只**把整轮投出去**，然后定时器就停 —— UI 线程
        一次都不会被 render_track 独占，表随结果回来逐条点亮。没有后台设施
        时才退回老路：同步算一条，两条之间让事件循环喘一口气（那条路 5 轨
        累计要僵住约 5.3s，所以它只是兜底）。
        """
        if not self._queue:
            self._analyze_timer.stop()
            return
        if self._submit_round():
            self._analyze_timer.stop()
            return
        self._analyze_one_sync()
        if not self._queue:
            self._analyze_timer.stop()
            self._analyzed_at = time.perf_counter()
            self._analyzed_signature = project_signature(self.project)
        self._update_status()

    def _analyze_one_sync(self) -> None:
        """同步算队列里的**一条**轨道（兜底路径与 :meth:`analyze_all_now` 用）。"""
        if not self._queue:
            return
        tid = self._queue.pop(0)
        track = self.project.track_by_id(tid)
        if track is None:
            return
        peak_db, rms_db, excerpt = self._analyze_full(track)
        self._levels[tid] = (peak_db, rms_db)
        if excerpt is not None:
            # 试听片段跟着分析一起留下：点图标就能立刻出声，不用等第二次渲染
            self._excerpts[tid] = excerpt
        strip = self._strips_by_id.get(tid)
        if strip is not None:
            self._refresh_meter(strip)

    def analyze_all_now(self) -> None:
        """同步把队列里的分析做完（测试与显式测量用）。

        这是**权威路径**：先作废并请停后台在跑的作业，再在调用线程里一次算完。
        为什么是"作废重算"而不是"等它们回来复用"：这个入口的语义是"现在算一遍"，
        在跑的作业可能基于更早的快照，复用它们等于把过时读数当成现状。
        它**不在界面路径上**（界面上的自动测量与"重新测量"按钮都走后台），
        所以这里的阻塞是可以接受的 —— 名字里的 "now" 就是这个意思。
        """
        self._analyze_timer.stop()
        self._round += 1                # 在跑的作业即使回得来，也不许落地
        self._cancel_jobs()
        while self._queue:
            self._analyze_one_sync()
        self._analyzed_at = time.perf_counter()
        self._analyzed_signature = project_signature(self.project)
        self._update_status()

    # -- 后台结果回到 UI 线程 --------------------------------------------

    def _on_analysis_done(self, round_id: int, tid: str, result) -> None:
        """一条轨道的后台结果回来了。**控件只在这里被碰。**

        这个方法一定跑在 UI 线程上（作业设施保证），所以在这里改 ``_levels``、
        刷新电平表、写状态栏都是安全的。
        """
        if round_id != self._round:
            return                      # 过期结果：中间换过工程/重开过一轮，不许覆盖新的
        self._jobs.pop(tid, None)
        if tid in self._queue:
            self._queue.remove(tid)
        peak_db, rms_db, excerpt, exc = result
        if exc is not None:
            track = self.project.track_by_id(tid)
            if track is not None:
                self._note_failure(track, exc)
        # 失败也要落一条记录（读数就是表底）。只写"没测到"的话，界面分不清
        # 这条轨道是失败了还是还没轮到它，而失败提示又会被后来的状态刷新盖掉。
        self._levels[tid] = (peak_db, rms_db)
        if excerpt is not None:
            self._excerpts[tid] = excerpt
        strip = self._strips_by_id.get(tid)
        if strip is not None:
            self._refresh_meter(strip)
        if not self._queue:
            self._analyzed_at = time.perf_counter()
            self._analyzed_signature = project_signature(self.project)
        self._update_status()

    def _on_analysis_error(self, round_id: int, tid: str, exc: BaseException) -> None:
        """作业本身炸了（不是"渲染失败"，而是作业函数抛了别的异常）。

        照旧按"这条轨道没测出来"处理：表落底 + 留下可见提示。静默丢一个异常
        是最坏的结果 —— 界面上唯一的症状就是"这条轨道没有读数"。
        """
        if round_id != self._round:
            return
        self._jobs.pop(tid, None)
        if tid in self._queue:
            self._queue.remove(tid)
        track = self.project.track_by_id(tid)
        if track is not None:
            self._note_failure(track, exc)
        self._levels[tid] = (METER_FLOOR_DB, METER_FLOOR_DB)
        strip = self._strips_by_id.get(tid)
        if strip is not None:
            self._refresh_meter(strip)
        if not self._queue:
            self._analyzed_at = time.perf_counter()
            self._analyzed_signature = project_signature(self.project)
        self._update_status()

    def _refresh_meter(self, strip: ChannelStrip) -> None:
        """把参考电平按当前音量/声像缩放后送到表上。

        这是"推子一动表就动"的关键：不重新渲染，只做几次浮点乘 ——
        因此拖动推子时电平表是实时的，而代价接近于零。
        """
        ref = self._levels.get(strip.track.id)
        if ref is None:
            # 还没分析到：先给表底，别显示一个假的中间值
            strip.set_reference(METER_FLOOR_DB, METER_FLOOR_DB,
                                audible=strip.is_audible())
            return
        audible = strip.is_audible()
        if not audible:
            strip.set_reference(METER_FLOOR_DB, METER_FLOOR_DB, audible=False)
            return
        gl, gr = dsp.equal_power_pan(strip.track.pan)
        gain = max(1e-6, float(strip.track.volume) * max(gl, gr))
        offset = dsp.gain_to_db(gain)
        peak = max(METER_FLOOR_DB, ref[0] + offset)
        rms = max(METER_FLOOR_DB, ref[1] + offset)
        strip.set_reference(peak, rms, audible=True)

    # -- 试听 ------------------------------------------------------------

    def excerpt(self, track: Track) -> np.ndarray | None:
        """取这条轨道的试听片段（分析时顺手留下的那一份）。"""
        return self._excerpts.get(track.id)

    def _audition_strip(self, strip: ChannelStrip) -> None:
        """点通道条头部图标 = 单独听这条轨道。

        这是混音时最常用的动作：拧完旋钮、推完推子，得马上确认"是不是我要的声音"。
        静音/独奏只影响混音结果，不影响试听 —— 你想听就说明你想听。
        """
        piece = self._excerpts.get(strip.track.id)
        if piece is None:
            # 还没分析到（比如刚加的新轨）：现渲染一次，慢一点但一定出声
            _p, _r, piece = self._analyze_full(strip.track)
            if piece is not None:
                self._excerpts[strip.track.id] = piece
        if piece is None or piece.size == 0:
            self._message(f"「{strip.track.name}」还没有可试听的内容")
            return
        try:
            from app.audio.player import audition

            player = audition()
            if player is None:
                self._message("音频设备不可用，无法试听")
                return
            player.play_note(piece)
            self._message(f"试听「{strip.track.name}」")
        except Exception:
            self._message("试听失败")

    def _remeasure(self) -> None:
        self._levels.clear()
        self._excerpts.clear()
        for strip in self._strips:
            strip.set_reference(METER_FLOOR_DB, METER_FLOOR_DB, audible=strip.is_audible())
        self._schedule_analysis()
        self._message("正在重新测量各轨参考电平…")

    def _clear_mute_solo(self) -> None:
        """清除所有静音与独奏。

        顺序上有一条硬约束：**先压撤销点，再清零**。``push_undo`` 是当场做
        ``project.to_dict()`` 快照，所以清零放在它前面的话，快照里已经是
        "清完"的样子 —— 用户按 Ctrl+Z 会发现什么都没变（旧实现就是这个 bug）。
        """
        changed = any(t.mute or t.solo for t in self.project.tracks)
        if not changed:
            self._message("没有静音或独奏需要清除")
            return
        self._commit("清除静音与独奏")
        for track in self.project.tracks:
            track.mute = False
            track.solo = False
        for strip in self._strips:
            strip.refresh()
            self._refresh_meter(strip)
        self._update_count()
        self.edited.emit()
        self._message("已清除全部静音与独奏")

    # -- 杂项 ------------------------------------------------------------

    def _on_strip_changed(self) -> None:
        """任何一条通道的参数变了。

        刷新**全部**通道的电平表而不只是变的那条：因为"独奏"是全局语义 ——
        开了某一条的独奏，其余通道立刻都不可闻，它们的表必须同时落到底。
        """
        for strip in self._strips:
            self._refresh_meter(strip)
        if self._master is not None:
            self._master.update()
        self._update_count()
        self.edited.emit()

    def _select_strip(self, strip) -> None:
        for s in self._strips:
            s.set_selected(s is strip)
        self._selected = strip

    def _message(self, text: str) -> None:
        self._status.setText(text)
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.statusMessage.emit(text, 2500)
            except Exception:
                pass

    def _update_count(self) -> None:
        notes = sum(len(t.notes) for t in self.project.tracks)
        self._count.setText(f"{len(self.project.tracks)} 条通道 · 母带 · 全曲 {notes} 音符")

    def _update_status(self) -> None:
        if self._queue:
            done = len(self._strips) - len(self._queue)
            self._status.setText(f"测量参考电平… {done}/{len(self._strips)}")
        elif self._failures:
            # 有失败就绝不说"已就绪"：那句无脑的成功提示会把故障彻底藏起来
            self._status.setText(
                f"{len(self._failures)} 条轨道参考电平分析失败（电平表按表底显示）："
                f"{self._failures[-1]}"
            )
        else:
            self._status.setText("参考电平已就绪")

    def showEvent(self, event):
        super().showEvent(event)
        # 页面第一次显示时如果还没有任何电平数据，补测一次
        if not self._levels and not self._queue:
            self._schedule_analysis()


__all__ = [
    "MixerPage", "ChannelStrip", "MasterStrip", "VerticalFader", "VerticalMeter",
    "MixerKnob", "StateDot", "METER_FLOOR_DB", "FADER_MAX", "STRIP_W", "MASTER_W",
]
