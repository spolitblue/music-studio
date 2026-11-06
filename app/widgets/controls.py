"""连续量控件：滑块与旋钮。

两者都遵循同一条交互原则：**按下即响应、拖动 1:1 跟随、松手带弹簧归位**。
滑块用绝对位置映射（点哪跳哪），旋钮用相对位移映射（拖动改值），
因为它们各自对应物理世界里的真实物件 —— 推子和旋钮的操作方式本就不同。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Property, Signal
from PySide6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QLinearGradient, QPainter, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from app import theme
from app.theme import accents, labels, materials, radii, type_scale
from app.widgets.glass import GlassStyle, paint_glass, paint_mini_specular


class GlassSlider(QWidget):
    """横向滑块。支持"点哪跳哪"（绝对定位），这是推子的直觉。"""

    valueChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None, minimum: float = 0.0,
                 maximum: float = 1.0, value: float = 0.5, *,
                 accent: QColor | None = None, height: int = 22,
                 center_origin: bool = False):
        super().__init__(parent)
        self._min = float(minimum)
        self._max = float(maximum)
        self._value = float(value)
        self._accent = accent or accents.blue
        self._center_origin = center_origin
        self._hover = False
        self._dragging = False

        self.setFixedHeight(height)
        self.setMinimumWidth(70)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- 值 --------------------------------------------------------------

    def value(self) -> float:
        return self._value

    def set_value(self, v: float, notify: bool = False) -> None:
        nv = max(self._min, min(self._max, float(v)))
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

    # -- 交互 ------------------------------------------------------------

    def _track_rect(self) -> QRectF:
        r = QRectF(self.rect()).adjusted(1.0, 0, -1.0, 0)
        return QRectF(r.x(), r.center().y() - 2.0, r.width(), 4.0)

    def _value_at(self, x: float) -> float:
        r = self._track_rect()
        f = 0.0 if r.width() <= 0 else (x - r.x()) / r.width()
        f = max(0.0, min(1.0, f))
        return self._min + f * (self._max - self._min)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.set_value(self._value_at(event.position().x()), notify=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.set_value(self._value_at(event.position().x()), notify=True)

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        step = (self._max - self._min) * 0.04
        self.set_value(self._value + (step if event.angleDelta().y() > 0 else -step), notify=True)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self._track_rect()
        f = self._frac()

        # 槽
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.black(0.32))
        painter.drawRoundedRect(r, 2.0, 2.0)
        painter.setBrush(theme.white(0.045))
        painter.drawRoundedRect(r.adjusted(0, 0.8, 0, 0), 2.0, 2.0)

        # 填充：居中模式从中间往两边长（声像滑块用）
        if self._center_origin:
            mid = r.center().x()
            hx = r.x() + f * r.width()
            fill = QRectF(min(mid, hx), r.y(), abs(hx - mid), r.height())
        else:
            fill = QRectF(r.x(), r.y(), r.width() * f, r.height())

        if fill.width() > 0.4:
            g = QLinearGradient(fill.left(), 0, fill.right(), 0)
            c0 = QColor(self._accent).lighter(112)
            c1 = QColor(self._accent)
            g.setColorAt(0.0, c0)
            g.setColorAt(1.0, c1)
            painter.setBrush(QBrush(g))
            painter.drawRoundedRect(fill, 2.0, 2.0)
            # 顶部高光
            hl = QColor(255, 255, 255, 90)
            painter.setBrush(hl)
            painter.drawRoundedRect(QRectF(fill.x(), fill.y() + 0.5, fill.width(), 1.0), 0.5, 0.5)

        # 手柄
        hx = r.x() + f * r.width()
        hr = 5.5 if (self._hover or self._dragging) else 4.8
        cy = self.rect().center().y() + 0.5
        # 阴影
        painter.setBrush(theme.black(0.35))
        painter.drawEllipse(QPointF(hx, cy + 0.8), hr, hr)
        painter.setBrush(QColor(250, 250, 252))
        painter.drawEllipse(QPointF(hx, cy), hr, hr)
        grad = QRadialGradient(QPointF(hx, cy - hr * 0.4), hr * 1.4)
        grad.setColorAt(0.0, QColor(255, 255, 255, 235))
        grad.setColorAt(1.0, QColor(210, 214, 222, 0))
        painter.setBrush(QBrush(grad))
        painter.drawEllipse(QPointF(hx, cy), hr, hr)
        painter.end()


class GlassKnob(QWidget):
    """旋钮。用**相对位移**改值（上下拖动），就像拧真实的旋钮。

    旋钮的指示弧从 -135° 到 +135° —— 这是硬件旋钮的标准行程，
    两端留出的空档让人一眼就知道"到底了"。
    """

    valueChanged = Signal(float)

    START_ANGLE = -225.0    # 度
    SWEEP = 270.0

    def __init__(self, parent: QWidget | None = None, minimum: float = -1.0,
                 maximum: float = 1.0, value: float = 0.0, *,
                 accent: QColor | None = None, size: int = 34,
                 center_detent: bool = True):
        super().__init__(parent)
        self._min = float(minimum)
        self._max = float(maximum)
        self._value = float(value)
        self._accent = accent or accents.blue
        self._center_detent = center_detent
        self._hover = False
        self._dragging = False
        self._drag_start_y = 0.0
        self._drag_start_value = 0.0

        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    # -- 值 --------------------------------------------------------------

    def value(self) -> float:
        return self._value

    def set_value(self, v: float, notify: bool = False) -> None:
        nv = max(self._min, min(self._max, float(v)))
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
        return 0.5 if span <= 0 else (self._value - self._min) / span

    # -- 交互 ------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start_y = event.globalPosition().y()
            self._drag_start_value = self._value

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        # 上下各 140px 走完整个行程 —— 既够精细又能快速到底
        dy = self._drag_start_y - event.globalPosition().y()
        span = self._max - self._min
        self.set_value(self._drag_start_value + (dy / 140.0) * span, notify=True)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        # 居中定位：松开时若很接近中点则吸附过去
        if self._center_detent and self._min < 0 < self._max:
            mid = (self._min + self._max) / 2.0
            if abs(self._value - mid) < span_epsilon(self._min, self._max):
                self.set_value(mid, notify=True)

    def mouseDoubleClickEvent(self, event):
        if self._center_detent and self._min < 0 < self._max:
            self.set_value(0.0, notify=True)
        else:
            self.set_value((self._min + self._max) / 2.0, notify=True)

    def wheelEvent(self, event):
        step = (self._max - self._min) * 0.05
        self.set_value(self._value + (step if event.angleDelta().y() > 0 else -step), notify=True)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(self.width(), self.height())

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        f = self._frac()

        # 底槽（指示弧的背景）
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(0, 0, 0, 110), 3.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(-self.START_ANGLE * -16), int(-self.SWEEP * 16))

        # 已填充弧
        pen = QPen(self._accent, 3.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(-self.START_ANGLE * -16), int(-self.SWEEP * f * 16))

        # 旋钮本体
        body = rect.adjusted(4.5, 4.5, -4.5, -4.5)
        paint_glass(painter, body, GlassStyle(
            material=materials.control_hover if (self._hover or self._dragging) else materials.control,
            radius=body.width() / 2.0, specular=0.8, border=1.0,
            inner_gradient=1.0, grain=0.5,
        ))

        # 指针
        angle = math.radians(self.START_ANGLE + self.SWEEP * f)
        cx, cy = body.center().x(), body.center().y()
        r_out = body.width() / 2.0 - 1.5
        r_in = r_out * 0.34
        painter.setPen(QPen(labels.label, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(
            QPointF(cx + r_in * math.cos(angle), cy + r_in * math.sin(angle)),
            QPointF(cx + r_out * math.cos(angle), cy + r_out * math.sin(angle)),
        )
        painter.end()


def span_epsilon(lo: float, hi: float) -> float:
    return max(1e-4, (hi - lo) * 0.045)


__all__ = ["GlassSlider", "GlassKnob", "span_epsilon"]
