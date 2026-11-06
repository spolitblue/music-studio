"""无边框窗口外壳 + 自定义标题栏。

为什么用无边框
--------------
DWM 的亚克力材质要作用在**客户区**上，系统标题栏会把它挡住并带来一条突兀的
白色描边。所以必须去掉系统边框、自己画标题栏，材质才能铺满整个窗口。

如何不牺牲原生手感
------------------
不用手写拖拽和缩放（那样会丢掉 Aero Snap 贴边和系统级缩放）。
Qt 6 提供 :meth:`QWindow.startSystemMove` / :meth:`startSystemResize`，
它们把交互交还给窗口管理器 —— 于是**贴边、双击最大化、多显示器缩放全部原生可用**。

最大化的处理
------------
最大化时窗口贴满屏幕，此时外圈的圆角与投影应当消失（否则屏幕上会出现
一圈透明缝）。:meth:`FramelessWindow.set_maximized` 统一处理这个状态切换。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from app import icons, theme
from app.platform import win_glass
from app.theme import accents, labels, materials, radii, spacing, type_scale
from app.widgets.glass import GlassIconButton, GlassSurface, paint_glass


class ResizeEdge(Enum):
    NONE = 0
    LEFT = 1
    RIGHT = 2
    TOP = 4
    BOTTOM = 8


# ==========================================================================
# 标题栏
# ==========================================================================


class TitleBar(QWidget):
    """自定义标题栏：左侧应用标识，中间可拖拽，右侧窗口按钮。

    高度刻意压到 44px —— iOS 的导航栏比例，既不浪费纵向空间，又给窗口按钮
    留够了 44×44 的可点区域（触控/鼠标都舒适）。
    """

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    HEIGHT = 44

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._title = "制乐 Music Studio"
        self._subtitle = ""
        self._hover_button: str | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(4)
        layout.addStretch(1)

        self.btn_min = GlassIconButton("－", self, size=30, tooltip="最小化")
        self.btn_max = GlassIconButton("□", self, size=30, tooltip="最大化")
        self.btn_close = GlassIconButton("✕", self, size=30, tooltip="关闭")
        self.btn_close.set_accent(accents.red)
        self.btn_close.set_style_kind("destructive")

        self.btn_min.clicked.connect(self.minimize_requested)
        self.btn_max.clicked.connect(self.maximize_requested)
        self.btn_close.clicked.connect(self.close_requested)

        for b in (self.btn_min, self.btn_max, self.btn_close):
            layout.addWidget(b)

        self._app_icon = icons.app_icon(64)

    # -- 标题 ------------------------------------------------------------

    def set_title(self, title: str, subtitle: str = "") -> None:
        self._title = title
        self._subtitle = subtitle
        self.update()

    def set_maximized(self, is_max: bool) -> None:
        self.btn_max.setText("❐" if is_max else "□")
        self.btn_max.setToolTip("向下还原" if is_max else "最大化")

    # -- 拖拽：交给窗口管理器，保住贴边与双击最大化 ----------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # 应用图标：玻璃底 + Twemoji 吉祥物 + 音符徽章
        s = 24
        pm = self._app_icon.pixmap(QSize(s, s))
        painter.drawPixmap(12, (self.height() - s) // 2, pm)

        # 标题
        x = 12 + s + 10
        f = type_scale.headline.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        painter.drawText(QRect(x, 0, self.width() - x - 140, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         self._title)

        if self._subtitle:
            from PySide6.QtGui import QFontMetricsF

            fm = QFontMetricsF(f)
            tx = x + fm.horizontalAdvance(self._title) + 10
            painter.setFont(type_scale.footnote.font())
            painter.setPen(QPen(labels.tertiary))
            painter.drawText(QRect(tx, 0, max(0, self.width() - tx - 140), self.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             self._subtitle)

        # 底部极淡分隔（滚动边缘效果：不做硬线，只轻轻压一道）
        g = QLinearGradient(0, self.height() - 1, self.width(), self.height() - 1)
        g.setColorAt(0.0, theme.white(0.0))
        g.setColorAt(0.5, theme.white(0.07))
        g.setColorAt(1.0, theme.white(0.0))
        painter.fillRect(0, self.height() - 1, self.width(), 1, QBrush(g))
        painter.end()


# ==========================================================================
# 无边框窗口
# ==========================================================================


class FramelessWindow(QWidget):
    """无边框 + 系统亚克力背景的窗口基类。

    子类只需往 :attr:`body` 里塞内容；外圈留白、圆角、材质、投影都由基类负责。
    """

    #: 外圈留白 —— 给投影与圆角留出绘制空间
    MARGIN = 8
    #: 边缘命中宽度（用于缩放）
    GRIP = 5

    def __init__(self, parent: QWidget | None = None, *, glass_mode: str = win_glass.GlassMode.ACRYLIC):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.setMinimumSize(940, 620)
        self.resize(1220, 800)

        self._glass_mode = glass_mode
        self._glass_ok = False
        self._glass_note = ""
        self._maximized = False
        self._resize_edge = ResizeEdge.NONE
        self._restore_geometry: QRect | None = None

        # 内容根节点
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)
        self._root.setSpacing(0)

        self.body = QWidget(self)
        self.body.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._root.addWidget(self.body)

        self.titlebar = TitleBar(self.body)
        self.titlebar.minimize_requested.connect(self.showMinimized)
        self.titlebar.maximize_requested.connect(self.toggle_maximized)
        self.titlebar.close_requested.connect(self.close)

    # -- 布局装配（子类调用）---------------------------------------------

    def assemble(self, content: QWidget, footer: QWidget | None = None) -> None:
        """把内容区与页脚（走带条）装进窗口。"""
        lay = QVBoxLayout(self.body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.titlebar)
        lay.addWidget(content, 1)
        if footer is not None:
            lay.addWidget(footer)

    # -- 材质 ------------------------------------------------------------

    @property
    def glass_active(self) -> bool:
        return self._glass_ok

    @property
    def glass_note(self) -> str:
        return self._glass_note

    def showEvent(self, event):
        super().showEvent(event)
        if not self._glass_ok:
            self._apply_glass()

    def _apply_glass(self) -> None:
        """在窗口真正创建之后才能挂系统材质（需要 HWND）。"""
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        # 染色值来自 theme.window_backdrop —— 这一层 win.grab() **拍不到**
        # （它在窗口背后由 DWM 合成），所以数值依据写在那个令牌的文档里：
        # 原来写死 a=168 相当于整窗盖 66% 近黑，桌面只透过 34%，
        # 就是用户看到的"蒙着一层黑雾"。现在是 62%。
        ok, note = win_glass.apply_glass(
            hwnd,
            mode=self._glass_mode,
            tint=theme.window_backdrop.tint,
            dark=True,
        )
        self._glass_ok = ok
        self._glass_note = note
        self.update()

    def set_glass_mode(self, mode: str) -> None:
        self._glass_mode = mode
        self._glass_ok = False
        self._apply_glass()

    # -- 最大化 ----------------------------------------------------------

    @property
    def is_maximized(self) -> bool:
        return self._maximized

    def toggle_maximized(self) -> None:
        self.set_maximized(not self._maximized)

    def set_maximized(self, value: bool) -> None:
        if value == self._maximized:
            return
        if value:
            self._restore_geometry = self.geometry()
            self._maximized = True
            self._root.setContentsMargins(0, 0, 0, 0)
            self.showMaximized()
        else:
            self._maximized = False
            self._root.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)
            self.showNormal()
            if self._restore_geometry is not None:
                self.setGeometry(self._restore_geometry)
        self.titlebar.set_maximized(self._maximized)
        self.update()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            is_max = bool(self.windowState() & Qt.WindowState.WindowMaximized)
            if is_max != self._maximized:
                self._maximized = is_max
                m = 0 if is_max else self.MARGIN
                self._root.setContentsMargins(m, m, m, m)
                self.titlebar.set_maximized(is_max)
                self.update()
        super().changeEvent(event)

    # -- 边缘缩放 --------------------------------------------------------

    def _edge_at(self, pos: QPoint) -> ResizeEdge:
        if self._maximized:
            return ResizeEdge.NONE
        m, g = self.MARGIN, self.GRIP
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        edge = ResizeEdge.NONE
        if x <= m + g:
            edge = ResizeEdge(edge.value | ResizeEdge.LEFT.value)
        if x >= w - m - g:
            edge = ResizeEdge(edge.value | ResizeEdge.RIGHT.value)
        if y <= m + g:
            edge = ResizeEdge(edge.value | ResizeEdge.TOP.value)
        if y >= h - m - g:
            edge = ResizeEdge(edge.value | ResizeEdge.BOTTOM.value)
        return edge

    @staticmethod
    def _cursor_for(edge: ResizeEdge):
        e = edge.value
        L, R, T, B = (ResizeEdge.LEFT.value, ResizeEdge.RIGHT.value,
                      ResizeEdge.TOP.value, ResizeEdge.BOTTOM.value)
        if e in (L | T, R | B):
            return Qt.CursorShape.SizeFDiagCursor
        if e in (R | T, L | B):
            return Qt.CursorShape.SizeBDiagCursor
        if e in (L, R):
            return Qt.CursorShape.SizeHorCursor
        if e in (T, B):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def mouseMoveEvent(self, event):
        edge = self._edge_at(event.position().toPoint())
        if edge != self._resize_edge:
            self._resize_edge = edge
            self.setCursor(self._cursor_for(edge))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._edge_at(event.position().toPoint())
            if edge != ResizeEdge.NONE:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(Qt.Edge(edge.value))
                    event.accept()
                    return
        super().mousePressEvent(event)

    def leaveEvent(self, event):
        self._resize_edge = ResizeEdge.NONE
        self.unsetCursor()
        super().leaveEvent(event)

    # -- 绘制：窗口玻璃 ------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        m = 0 if self._maximized else self.MARGIN
        rect = QRectF(self.rect()).adjusted(m, m, -m, -m)
        radius = 0.0 if self._maximized else radii.window
        shape = theme.squircle(rect, radius)

        painter.save()
        painter.setClipPath(shape)

        if self._glass_ok:
            # 系统亚克力在窗口背后做真实模糊，这里只叠一层极淡染色。
            # 染色压得很低 —— 用户要的是"背景透明"，桌面应该明显透上来，
            # 窗口自身只保留一层若有若无的玻璃壳。
            painter.setPen(Qt.PenStyle.NoPen)
            tint = QColor(materials.window_tint)
            tint.setAlphaF(0.12)
            painter.setBrush(tint)
            painter.drawRect(rect)
        # 不自绘底图、不加暗角 —— 背景保持真透明。
        # （早先这里画过一圈径向暗角来聚焦视线，但它会给整块窗口染上 alpha，
        #   桌面就透不过来了，与"背景透明"直接冲突，已去掉。）

        painter.restore()

        # ---- 外框：可见的圆角玻璃边 -------------------------------------
        # 背景透明之后窗口自身没有任何视觉边界，四角看起来就是方的。
        # 所以显式画一圈**圆角**轮廓：外层暗线把窗口从桌面里切出来，
        # 内层亮环给出玻璃厚度，顶部再补一道高光。
        if radius > 0:
            ow = theme.outline.width * 1.5
            shape_in = theme.squircle(rect.adjusted(0.4, 0.4, -0.4, -0.4), radius)

            dark = QColor(theme.outline.dark_outer)
            dark.setAlphaF(0.58)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(dark, ow * 2.2))
            painter.drawPath(theme.squircle(rect.adjusted(-0.8, -0.8, 0.8, 0.8), radius + 0.8))

            g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            g.setColorAt(0.0, theme.white(0.52))
            g.setColorAt(0.45, theme.white(0.24))
            g.setColorAt(1.0, theme.white(0.18))
            painter.setPen(QPen(QBrush(g), ow))
            painter.drawPath(shape_in)

            # 顶部高光（光线照到材质）
            painter.save()
            painter.setClipPath(shape)
            hg = QLinearGradient(rect.topLeft(), rect.topRight())
            hg.setColorAt(0.0, theme.white(0.0))
            hg.setColorAt(0.18, theme.white(0.46))
            hg.setColorAt(0.82, theme.white(0.38))
            hg.setColorAt(1.0, theme.white(0.0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(hg))
            painter.drawRect(QRectF(rect.x() + radius * 0.7, rect.y() + 0.6,
                                    max(0.0, rect.width() - radius * 1.4), 1.4))
            painter.restore()

        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 背景按窗口尺寸取，尺寸变了要重做（并让所有面板的液态缓存失效）
        self._invalidate_backdrop()

    def moveEvent(self, event):
        super().moveEvent(event)
        # **逐帧刷新，不再节流。**
        # 原先这里靠一个 80ms 单发定时器把刷新压到 ~12fps，因为一次全量
        # 重算 9 块面板的液态玻璃要上百毫秒。实时档下这个前提消失了 ——
        # 每帧的成本只有"重画染色与高光边"，没有任何像素运算，
        # 于是拖动时不再"每 80ms 卡一下"，而是跟着鼠标走。
        self._on_window_moved()

    def showEvent(self, event):
        super().showEvent(event)
        # 注意：这个方法不能只做桌面实拍 —— 系统材质也必须在窗口真正创建之后
        # 才能挂上（需要 HWND）。早先这里出现过两个 showEvent 定义，后者把前者
        # 覆盖掉，亚克力就悄无声息地失效了。
        if not self._glass_ok:
            self._apply_glass()
        # 首次显示后拍桌面。此时窗口已存在（有 HWND），才能临时透明化去拍。
        QTimer.singleShot(120, self.refresh_desktop_backdrop)

    def refresh_desktop_backdrop(self) -> None:
        """重新截取窗口背后的桌面，作为玻璃的折射源。

        窗口会闪一下（要先把自身透明化才能拍到背后的桌面），所以只在这个
        明确时机调用：启动、移动/缩放停手之后。
        """
        try:
            from app.widgets.ambient import capture_desktop

            if capture_desktop(self):
                self._invalidate_backdrop()
        except Exception:
            pass

    def _on_window_moved(self) -> None:
        """窗口移动时的高频刷新 —— **逐帧，不节流**。

        要点一：**不重新截图**。截图必须先把窗口透明化（否则拍到自己），
        每帧重拍会疯狂闪烁。而我们启动时截的那块桌面**比窗口大**，
        所以移动时只要平移采样窗口就够了 —— 省掉了截图，也省掉了重算模糊。

        要点二（本次改动的核心）：**把 80ms 的节流拿掉**。
        原先这里靠一个单发定时器把刷新压到 ~12fps，理由是一次全量重算
        9 块面板的液态玻璃要上百毫秒，"真按 60fps 走会把界面拖垮"。

        改成实时档之后这个前提消失了：坐在窗口背景上的面板（A 类）
        **一个像素都不算** —— 模糊由 DWM 在窗口背后完成，我们只画染色和
        高光边。于是逐帧刷新反而是最省的做法。参考实现（InfiniteGUI 的
        游戏内 HUD）的原话正是：

        > During animation, refresh rate is capped at the monitor refresh rate
        > for smooth output without wasteful work.

        而不是"节流到 12fps"。
        """
        try:
            from app.widgets.ambient import ambient

            geo = self.frameGeometry()
            ambient.set_window_position(geo.x(), geo.y())
            if ambient.using_desktop and not ambient.covers(geo):
                # 移出了截图范围 —— 只能重拍（会闪一下，但这种情况本来就少）
                ambient.invalidate_sampling()
                self.refresh_desktop_backdrop()
                return
            # 桌面内容没变，只是采样窗口平移了：预模糊结果照旧复用，
            # 降采样亮度图存的是截屏坐标，平移只是换个下标，也不用重算。
            self._invalidate_backdrop(full=False)
        except Exception:
            return

        # 停手 340ms 后重拍一次：既补上余量范围外的漂移，也让折射完全对齐
        if not hasattr(self, '_settle_timer'):
            self._settle_timer = QTimer(self)
            self._settle_timer.setSingleShot(True)
            self._settle_timer.setInterval(340)
            self._settle_timer.timeout.connect(self.refresh_desktop_backdrop)
        self._settle_timer.start()

    def _invalidate_backdrop(self, *, full: bool = True) -> None:
        """让所有玻璃面板在下次绘制时重新取背景。

        ``full=True``
            换壁纸 / 重拍桌面 / 窗口缩放 —— 采样图本身变了，全部作废。
        ``full=False``
            窗口**平移** —— 桌面内容没变，只是采样窗口移动了。
            预模糊结果与降采样亮度图都还能复用，只作废取样即可。
        """
        try:
            from app.widgets.ambient import ambient

            if full:
                ambient.invalidate()
            else:
                ambient.invalidate_sampling()
            for child in self.findChildren(QWidget):
                if hasattr(child, "invalidate_liquid"):
                    child.invalidate_liquid()
        except Exception:
            pass


__all__ = ["FramelessWindow", "TitleBar", "ResizeEdge"]
